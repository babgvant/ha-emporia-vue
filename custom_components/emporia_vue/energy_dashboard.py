"""Add Emporia branch-circuit statistics to Home Assistant Energy."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
import logging
import re
from typing import Any

from pyemvue.enums import Scale
import voluptuous as vol

from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .hierarchy import channel_name

SERVICE_ADD_TO_ENERGY_DASHBOARD = "add_to_energy_dashboard"
ATTR_DEVICE_ID = "device_id"
DATA_ENERGY_UPDATE_LOCK = "energy_dashboard_update_lock"
ENERGY_NAMES_STORAGE_VERSION = 1

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


def energy_dashboard_monitor_roles(
    devices: Mapping[int, Any], selected_gids: Iterable[int]
) -> tuple[set[int], set[int]]:
    """Return branch monitors and nested devices represented as one circuit.

    Selected monitors and nested subpanels contribute branch circuits. A nested
    EVSE contributes its main total as one discrete circuit instead.
    """
    requested = {int(gid) for gid in selected_gids if int(gid) in devices}

    def has_requested_ancestor(gid: int) -> bool:
        seen = {gid}
        parent_gid = devices[gid].parent_device_gid
        while parent_gid in devices and parent_gid not in seen:
            if parent_gid in requested:
                return True
            seen.add(parent_gid)
            parent_gid = devices[parent_gid].parent_device_gid
        return False

    # A selected ancestor takes precedence. This also handles existing config
    # entries that stored both a parent and its nested monitor.
    roots = {gid for gid in requested if not has_requested_ancestor(gid)}
    branch_monitors = set(roots)
    discrete_monitors: set[int] = set()
    pending = list(roots)
    while pending:
        parent_gid = pending.pop()
        for gid, device in devices.items():
            if device.parent_device_gid != parent_gid:
                continue
            has_branch_circuits = any(
                is_consumptive_circuit(channel)
                for channel in getattr(device, "channels", [])
            )
            if getattr(device, "ev_charger", None) or not has_branch_circuits:
                discrete_monitors.add(gid)
            elif gid not in branch_monitors:
                branch_monitors.add(gid)
                pending.append(gid)
    return branch_monitors, discrete_monitors


def circuit_energy_unique_id(device_gid: int, channel_num: str) -> str:
    """Return the stable daily cumulative-energy entity unique ID."""
    return (
        f"sensor.emporia_vue.{Scale.DAY.value}."
        f"{device_gid}-{channel_num}"
    )


def registered_energy_entity_id(
    entries_by_unique_id: Mapping[str, Any], unique_id: str
) -> str | None:
    """Return an enabled registered entity without requiring a published state."""
    if (
        (entry := entries_by_unique_id.get(unique_id)) is None
        or entry.disabled_by is not None
    ):
        return None
    return entry.entity_id


def parse_circuit_energy_unique_id(unique_id: str) -> tuple[int, str] | None:
    """Parse an actual registered daily circuit unique ID."""
    match = re.fullmatch(
        rf"sensor\.emporia_vue\.{re.escape(Scale.DAY.value)}\.(\d+)-(.+)",
        unique_id,
    )
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def circuit_energy_name(device: Any, channel: Any) -> str:
    """Keep circuit labels distinct when HA groups entities under one monitor."""
    monitor_name = (
        device.display_name or device.device_name or f"Emporia monitor {device.device_gid}"
    )
    prefix = channel_name(device, channel)
    return f"{monitor_name} {prefix}" if prefix else monitor_name


def merge_device_consumption(
    existing: Iterable[dict[str, Any]], statistic_ids: Iterable[str],
    names: Mapping[str, str] | None = None,
    generated_names: dict[str, str | None] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Sync generated labels and record ownership in generated_names.

    A string is the last label we wrote; None records a manual override. Legacy
    labels are adopted only if absent or equal to the current Emporia label.
    """
    names = names or {}
    if generated_names is None:
        generated_names = {}
    result = [dict(item) for item in existing]
    requested = list(statistic_ids)
    requested_ids = set(requested)
    for item in result:
        statistic_id = item.get("stat_consumption")
        if statistic_id not in requested_ids or not names.get(statistic_id):
            continue
        current_name = item.get("name")
        if statistic_id in generated_names:
            previous_name = generated_names[statistic_id]
            managed = not current_name or (
                previous_name is not None and current_name == previous_name
            )
        else:
            managed = not current_name or current_name == names[statistic_id]
        if managed:
            item["name"] = names[statistic_id]
            generated_names[statistic_id] = names[statistic_id]
        else:
            # Keep this override even if a future Emporia rename matches it.
            generated_names[statistic_id] = None
    configured = {item.get("stat_consumption") for item in result}
    added = already_configured = 0
    for statistic_id in requested:
        if statistic_id in configured:
            already_configured += 1
            continue
        item = {"stat_consumption": statistic_id}
        if names.get(statistic_id):
            item["name"] = names[statistic_id]
            generated_names[statistic_id] = names[statistic_id]
        result.append(item)
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


async def async_setup_energy_dashboard_service(hass: HomeAssistant) -> None:
    """Register the device-targeted Energy Dashboard action."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_TO_ENERGY_DASHBOARD):
        return
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

        result = await async_add_circuits_to_energy_dashboard(
            hass, entry_id, {root_gid}
        )
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


async def async_add_circuits_to_energy_dashboard(
    hass: HomeAssistant, entry_id: str, selected_gids: Iterable[int]
) -> dict[str, int]:
    """Add eligible circuits for selected monitor trees to Energy preferences."""
    devices = hass.data[DOMAIN][entry_id]["device_information"]
    entity_registry = er.async_get(hass)
    registered_entries = [
        entry
        for entry in entity_registry.entities.values()
        if entry.config_entry_id == entry_id and entry.platform == DOMAIN
    ]
    selected_gids, nested_circuit_gids = energy_dashboard_monitor_roles(
        devices, selected_gids
    )
    selected_channel_gids = {
        int(channel.device_gid)
        for gid in selected_gids
        if gid in devices
        for channel in devices[gid].channels
    }
    allowed_entity_gids = (
        selected_gids | nested_circuit_gids | selected_channel_gids
    )
    channel_lookup = {
        (int(channel.device_gid), str(channel.channel_num)): channel
        for device in devices.values()
        for channel in device.channels
    }
    channel_devices = {
        (int(channel.device_gid), str(channel.channel_num)): device
        for device in devices.values()
        for channel in device.channels
    }
    statistic_ids: list[str] = []
    names: dict[str, str] = {}
    skipped_aggregate = skipped_incompatible = 0
    registered_daily_entities = 0
    for entry in registered_entries:
        if (parsed := parse_circuit_energy_unique_id(entry.unique_id)) is None:
            continue
        registered_daily_entities += 1
        gid, channel_num = parsed
        if gid not in allowed_entity_gids:
            continue
        channel = channel_lookup.get((gid, channel_num))
        if channel is None or entry.disabled_by is not None:
            skipped_incompatible += 1
            continue
        is_nested_monitor_total = (
            gid in nested_circuit_gids and channel_num == "1,2,3"
        )
        if gid in nested_circuit_gids and not is_nested_monitor_total:
            skipped_aggregate += 1
            continue
        if not is_nested_monitor_total and not is_consumptive_circuit(channel):
            skipped_aggregate += 1
            continue
        statistic_ids.append(entry.entity_id)
        names[entry.entity_id] = circuit_energy_name(
            channel_devices[(gid, channel_num)], channel
        )

    update_lock = hass.data[DOMAIN].setdefault(
        DATA_ENERGY_UPDATE_LOCK, asyncio.Lock()
    )
    async with update_lock:
        # Keep ownership outside Energy preferences, whose schema only accepts
        # HA's own fields. Stable unique IDs survive entity_id changes.
        store = Store(hass, ENERGY_NAMES_STORAGE_VERSION, f"{DOMAIN}.energy_names.{entry_id}")
        stored_names = await store.async_load() or {}
        generated_names = {
            entry.entity_id: stored_names[entry.unique_id]
            for entry in registered_entries
            if entry.unique_id in stored_names
        }
        manager = await async_get_manager(hass)
        preferences = manager.data or manager.default_preferences()
        existing = preferences.get("device_consumption", [])
        merged, added, already_configured = merge_device_consumption(
            existing, statistic_ids, names, generated_names
        )
        if merged != existing:
            # EnergyManager applies a partial update, preserving all other prefs.
            await manager.async_update({"device_consumption": merged})
        updated_names = {
            **stored_names,
            **{
                entry.unique_id: generated_names[entry.entity_id]
                for entry in registered_entries
                if entry.entity_id in names
            },
        }
        if updated_names != stored_names:
            await store.async_save(updated_names)

    return {
        "added": added,
        "already_configured": already_configured,
        "skipped_aggregate": skipped_aggregate,
        "skipped_incompatible": skipped_incompatible,
        "selected_monitors": len(selected_gids),
        "nested_monitor_circuits": len(nested_circuit_gids),
        "registered_daily_entities": registered_daily_entities,
    }
