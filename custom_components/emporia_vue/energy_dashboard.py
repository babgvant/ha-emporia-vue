"""Add Emporia branch-circuit statistics to Home Assistant Energy."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_STATE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from .const import DOMAIN

SERVICE_ADD_TO_ENERGY_DASHBOARD = "add_to_energy_dashboard"
ATTR_DEVICE_ID = "device_id"
DATA_ENERGY_UPDATE_LOCK = "energy_dashboard_update_lock"

_LOGGER = logging.getLogger(__name__)
_AGGREGATE_CHANNELS = {
    "1,2,3",
    "Balance",
    "MainsFromGrid",
    "MainsToGrid",
    "TotalUsage",
}
_NON_CONSUMPTION_TYPES = ("solar", "generation", "bidirectional")


def is_consumptive_circuit(channel: Any) -> bool:
    """Return whether an Emporia channel is a branch consumption circuit."""
    channel_num = str(channel.channel_num)
    if channel_num in _AGGREGATE_CHANNELS or not channel_num.isdigit():
        return False
    if getattr(channel, "channel_type_gid", None) == 13:
        return False
    channel_type = str(getattr(channel, "type", "") or "").casefold()
    return not any(marker in channel_type for marker in _NON_CONSUMPTION_TYPES)


def descendant_gids(devices: Mapping[int, Any], root_gid: int) -> set[int]:
    """Return one monitor and all of its nested/combined descendants."""
    included = {root_gid}
    changed = True
    while changed:
        changed = False
        for gid, device in devices.items():
            if gid not in included and device.parent_device_gid in included:
                included.add(gid)
                changed = True
    return included


def circuit_energy_unique_id(device_gid: int, channel_num: str) -> str:
    """Return the stable daily cumulative-energy entity unique ID."""
    return f"sensor.emporia_vue.day.{device_gid}-{channel_num}"


def merge_device_consumption(
    existing: Iterable[dict[str, Any]], statistic_ids: Iterable[str]
) -> tuple[list[dict[str, Any]], int, int]:
    """Append missing statistics without changing existing Energy entries."""
    result = list(existing)
    configured = {item.get("stat_consumption") for item in result}
    added = already_configured = 0
    for statistic_id in statistic_ids:
        if statistic_id in configured:
            already_configured += 1
            continue
        result.append({"stat_consumption": statistic_id})
        configured.add(statistic_id)
        added += 1
    return result, added, already_configured


def _device_gid(device: dr.DeviceEntry) -> int:
    """Extract the Emporia GID from a registry device."""
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            try:
                return int(identifier)
            except ValueError as err:
                raise HomeAssistantError(
                    f"Emporia device has an invalid identifier: {identifier}"
                ) from err
    raise HomeAssistantError("The selected device is not an Emporia Vue monitor")


def _is_energy_state_compatible(hass: HomeAssistant, entity_id: str) -> bool:
    """Verify the entity can produce the long-term sum Energy consumes."""
    state = hass.states.get(entity_id)
    if state is None:
        return False
    return (
        state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.ENERGY
        and state.attributes.get(ATTR_STATE_CLASS)
        in (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING)
        and state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) in set(UnitOfEnergy)
    )


async def async_setup_energy_dashboard_service(hass: HomeAssistant) -> None:
    """Register the device-targeted Energy Dashboard action."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_TO_ENERGY_DASHBOARD):
        return
    update_lock = hass.data[DOMAIN].setdefault(DATA_ENERGY_UPDATE_LOCK, asyncio.Lock())

    async def async_add_to_energy_dashboard(
        call: ServiceCall,
    ) -> dict[str, int] | None:
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        device = device_registry.async_get(call.data[ATTR_DEVICE_ID])
        if device is None:
            raise HomeAssistantError("The selected Home Assistant device was not found")

        root_gid = _device_gid(device)
        entry_ids = device.config_entries & set(hass.data.get(DOMAIN, {}))
        if not entry_ids:
            raise HomeAssistantError("The selected Emporia Vue device is not loaded")
        entry_id = next(iter(entry_ids))
        devices = hass.data[DOMAIN][entry_id]["device_information"]
        if root_gid not in devices:
            raise HomeAssistantError(
                "The selected monitor is not part of this config entry"
            )

        selected_gids = descendant_gids(devices, root_gid)
        entries_by_unique_id = {
            entry.unique_id: entry
            for entry in entity_registry.entities.values()
            if entry.config_entry_id == entry_id and entry.platform == DOMAIN
        }
        statistic_ids: list[str] = []
        skipped_aggregate = skipped_incompatible = 0
        for gid in sorted(selected_gids):
            for channel in devices[gid].channels:
                if not is_consumptive_circuit(channel):
                    skipped_aggregate += 1
                    continue
                unique_id = circuit_energy_unique_id(gid, channel.channel_num)
                if (
                    (entry := entries_by_unique_id.get(unique_id)) is None
                    or not _is_energy_state_compatible(hass, entry.entity_id)
                ):
                    skipped_incompatible += 1
                    continue
                statistic_ids.append(entry.entity_id)

        async with update_lock:
            manager = await async_get_manager(hass)
            preferences = manager.data or manager.default_preferences()
            merged, added, already_configured = merge_device_consumption(
                preferences.get("device_consumption", []), statistic_ids
            )
            if added:
                # EnergyManager applies a partial update, preserving all other prefs.
                await manager.async_update({"device_consumption": merged})

        result = {
            "added": added,
            "already_configured": already_configured,
            "skipped_aggregate": skipped_aggregate,
            "skipped_incompatible": skipped_incompatible,
        }
        _LOGGER.info(
            "Energy Dashboard update for %s: %s",
            device.name_by_user or device.name or root_gid,
            result,
        )
        return result if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_TO_ENERGY_DASHBOARD,
        async_add_to_energy_dashboard,
        schema=vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
