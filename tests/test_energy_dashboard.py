"""Tests for Energy Dashboard bulk configuration helpers."""

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.emporia_vue.energy_dashboard import (
    circuit_energy_unique_id,
    circuit_energy_name,
    async_add_circuits_to_energy_dashboard,
    descendant_gids,
    energy_dashboard_monitor_roles,
    is_consumptive_circuit,
    merge_device_consumption,
    parse_circuit_energy_unique_id,
    registered_energy_entity_id,
)


@dataclass
class Channel:
    channel_num: str
    channel_type_gid: int = 1
    type: str = "Consumption"


@dataclass
class Device:
    parent_device_gid: int = 0
    ev_charger: bool = False
    channels: list[Channel] = field(default_factory=list)


def test_basic_bulk_add_and_existing_configuration() -> None:
    existing = [{"stat_consumption": "sensor.dryer"}]
    merged, added, already = merge_device_consumption(
        existing, ["sensor.hvac", "sensor.dryer", "sensor.kitchen"]
    )
    assert merged == [
        {"stat_consumption": "sensor.dryer"},
        {"stat_consumption": "sensor.hvac"},
        {"stat_consumption": "sensor.kitchen"},
    ]
    assert (added, already) == (2, 1)


def test_idempotency() -> None:
    first, _, _ = merge_device_consumption([], ["sensor.hvac", "sensor.dryer"])
    second, added, already = merge_device_consumption(
        first, ["sensor.hvac", "sensor.dryer"]
    )
    assert second == first
    assert (added, already) == (0, 2)


def test_energy_names_include_circuit_and_monitor() -> None:
    device = SimpleNamespace(device_gid=1, display_name="HS2", device_name="Vue")
    assert circuit_energy_name(device, SimpleNamespace(channel_num="4", name="Kitchen")) == "HS2 Kitchen"
    assert circuit_energy_name(device, SimpleNamespace(channel_num="5", name="HS2")) == "HS2 Circuit 5"
    assert circuit_energy_name(device, SimpleNamespace(channel_num="6", name=None)) == "HS2 Circuit 6"
    assert circuit_energy_name(device, SimpleNamespace(channel_num="1,2,3", name="Main")) == "HS2"


def test_rerun_repairs_missing_names_and_preserves_custom_settings() -> None:
    existing = [
        {"stat_consumption": "sensor.kitchen", "stat_rate": "sensor.kitchen_power"},
        {"stat_consumption": "sensor.dryer", "name": "My dryer"},
        {"stat_consumption": "sensor.other"},
    ]
    names = {"sensor.kitchen": "HS2 Kitchen", "sensor.dryer": "HS2 Dryer"}
    merged, added, already = merge_device_consumption(existing, names, names)
    assert merged == [
        {"stat_consumption": "sensor.kitchen", "stat_rate": "sensor.kitchen_power", "name": "HS2 Kitchen"},
        existing[1],
        existing[2],
    ]
    assert "name" not in existing[0]
    assert (added, already) == (0, 2)
    assert merge_device_consumption(merged, names, names) == (merged, 0, 2)


def test_new_circuits_save_names_without_duplicates() -> None:
    names = {"sensor.kitchen": "HS2 Kitchen", "sensor.dryer": "HS2 Dryer"}
    merged, added, already = merge_device_consumption([], names, names)
    assert merged == [
        {"stat_consumption": "sensor.kitchen", "name": "HS2 Kitchen"},
        {"stat_consumption": "sensor.dryer", "name": "HS2 Dryer"},
    ]
    assert (added, already) == (2, 0)


def test_generated_names_follow_renames_but_manual_names_stay_manual() -> None:
    generated = {}
    names = {"sensor.garage_10": "Garage Circuit 10"}
    merged, _, _ = merge_device_consumption([], names, names, generated)
    names["sensor.garage_10"] = "Garage Freezer"
    merged, added, already = merge_device_consumption(merged, names, names, generated)
    assert merged[0]["name"] == "Garage Freezer"
    assert generated == names
    assert (added, already) == (0, 1)

    merged[0]["name"] = "My freezer"
    merged, _, _ = merge_device_consumption(merged, names, names, generated)
    assert merged[0]["name"] == "My freezer"
    assert generated == {"sensor.garage_10": None}
    # Matching Emporia later must not silently reclaim a manual label.
    names["sensor.garage_10"] = "My freezer"
    merged, _, _ = merge_device_consumption(merged, names, names, generated)
    names["sensor.garage_10"] = "Garage Refrigerator"
    merged, _, _ = merge_device_consumption(merged, names, names, generated)
    assert merged[0]["name"] == "My freezer"


def test_legacy_names_are_adopted_conservatively_and_can_be_reset() -> None:
    names = {"sensor.10": "Garage Freezer", "sensor.11": "Garage Lights"}
    generated = {}
    existing = [
        {"stat_consumption": "sensor.10", "name": "Garage Freezer"},
        {"stat_consumption": "sensor.11", "name": "Old or manual label"},
    ]
    merged, _, _ = merge_device_consumption(existing, names, names, generated)
    assert merged == existing
    assert generated == {"sensor.10": "Garage Freezer", "sensor.11": None}
    merged[1]["name"] = ""
    merged, _, _ = merge_device_consumption(merged, names, names, generated)
    assert merged[1]["name"] == "Garage Lights"
    assert generated == names


def test_unselected_circuits_keep_their_names_and_tracking() -> None:
    existing = [{"stat_consumption": "sensor.other", "name": "Original"}]
    generated = {"sensor.other": "Original"}
    merged, _, _ = merge_device_consumption(
        existing, [], {"sensor.other": "Changed"}, generated
    )
    assert merged == existing
    assert generated == {"sensor.other": "Original"}


def test_bulk_add_persists_name_repairs_without_new_circuits() -> None:
    """A rerun must save repairs even when the added count is zero."""
    async def run() -> None:
        channel = SimpleNamespace(
            device_gid=1, channel_num="4", name="Kitchen", type="Consumption"
        )
        device = SimpleNamespace(
            device_gid=1, display_name="HS2", device_name="Vue",
            parent_device_gid=0, channels=[channel],
        )
        entry = SimpleNamespace(
            config_entry_id="entry", platform="emporia_vue", disabled_by=None,
            unique_id=circuit_energy_unique_id(1, "4"), entity_id="sensor.kitchen",
        )
        existing = [{"stat_consumption": "sensor.kitchen"}]
        manager = SimpleNamespace(
            data={"device_consumption": existing}, async_update=AsyncMock()
        )
        hass = SimpleNamespace(data={
            "emporia_vue": {"entry": {"device_information": {1: device}}}
        })
        persisted = {}

        async def save_names(data):
            persisted.clear()
            persisted.update(data)

        # A fresh Store is constructed on every call, as after a HA restart.
        store = SimpleNamespace(
            async_load=AsyncMock(side_effect=lambda: dict(persisted)),
            async_save=AsyncMock(side_effect=save_names),
        )
        with (
            patch("custom_components.emporia_vue.energy_dashboard.Store",
                  return_value=store),
            patch("custom_components.emporia_vue.energy_dashboard.er.async_get",
                  return_value=SimpleNamespace(entities={entry.entity_id: entry})),
            patch("custom_components.emporia_vue.energy_dashboard.async_get_manager",
                  new=AsyncMock(return_value=manager)),
        ):
            result = await async_add_circuits_to_energy_dashboard(hass, "entry", [1])
            assert result["added"] == 0
            assert result["already_configured"] == 1
            update = {"device_consumption": [
                {"stat_consumption": "sensor.kitchen", "name": "HS2 Kitchen"}
            ]}
            manager.async_update.assert_awaited_once_with(update)
            assert existing == [{"stat_consumption": "sensor.kitchen"}]
            manager.data = update
            manager.async_update.reset_mock()
            await async_add_circuits_to_energy_dashboard(hass, "entry", [1])
            manager.async_update.assert_not_awaited()
            assert persisted == {entry.unique_id: "HS2 Kitchen"}
            # Both an Emporia rename and HA entity_id rename survive reloads.
            channel.name = "Freezer"
            entry.entity_id = "sensor.renamed_kitchen"
            manager.data["device_consumption"][0]["stat_consumption"] = entry.entity_id
            await async_add_circuits_to_energy_dashboard(hass, "entry", [1])
            manager.async_update.assert_awaited_once_with({"device_consumption": [
                {"stat_consumption": entry.entity_id, "name": "HS2 Freezer"}
            ]})
            assert persisted == {entry.unique_id: "HS2 Freezer"}
            manager.data = {"device_consumption": [
                {"stat_consumption": entry.entity_id, "name": "Custom freezer"}
            ]}
            manager.async_update.reset_mock()
            channel.name = "Refrigerator"
            await async_add_circuits_to_energy_dashboard(hass, "entry", [1])
            manager.async_update.assert_not_awaited()
            assert persisted == {entry.unique_id: None}

    asyncio.run(run())


def test_unrelated_energy_configuration_is_not_part_of_partial_update() -> None:
    preferences = {
        "energy_sources": [{"type": "solar", "stat_energy_from": "sensor.solar"}],
        "device_consumption": [{"stat_consumption": "sensor.other"}],
        "future_setting": {"keep": True},
    }
    merged, _, _ = merge_device_consumption(
        preferences["device_consumption"], ["sensor.hvac"]
    )
    update = {"device_consumption": merged}
    assert update == {
        "device_consumption": [
            {"stat_consumption": "sensor.other"},
            {"stat_consumption": "sensor.hvac"},
        ]
    }
    assert preferences["energy_sources"][0]["stat_energy_from"] == "sensor.solar"
    assert preferences["future_setting"] == {"keep": True}


def test_power_and_aggregate_channels_are_ignored_by_metadata() -> None:
    assert circuit_energy_unique_id(1, "4") == "sensor.emporia_vue.1D.1-4"
    assert circuit_energy_unique_id(1, "4") != "sensor.emporia_vue.instant.1-4"
    assert is_consumptive_circuit(Channel("4"))
    assert not is_consumptive_circuit(Channel("1,2,3"))
    assert not is_consumptive_circuit(Channel("Balance"))
    assert not is_consumptive_circuit(Channel("MainsFromGrid"))
    assert not is_consumptive_circuit(Channel("4", channel_type_gid=13))
    assert not is_consumptive_circuit(Channel("5", type="Bidirectional"))


def test_registered_energy_entity_does_not_require_live_state() -> None:
    """Setup-time bulk add uses the registry before states are published."""
    unique_id = circuit_energy_unique_id(1, "4")
    entries = {
        unique_id: SimpleNamespace(
            entity_id="sensor.hvac_energy_today", disabled_by=None
        )
    }
    assert (
        registered_energy_entity_id(entries, unique_id)
        == "sensor.hvac_energy_today"
    )


def test_registered_child_monitor_circuit_id_is_discovered() -> None:
    """Daily entities retain the child monitor GID in their unique ID."""
    assert parse_circuit_energy_unique_id(
        "sensor.emporia_vue.1D.200-7"
    ) == (200, "7")
    assert parse_circuit_energy_unique_id(
        "sensor.emporia_vue.1MON.200-7"
    ) is None


def test_combined_monitor_tree_excludes_unrelated_roots() -> None:
    devices = {
        1: Device(),
        2: Device(parent_device_gid=1),
        3: Device(parent_device_gid=2),
        4: Device(),
    }
    assert descendant_gids(devices, 1) == {1, 2, 3}


def test_parent_selection_treats_nested_evse_as_one_circuit() -> None:
    devices = {
        1: Device(),
        2: Device(parent_device_gid=1, ev_charger=True),
        3: Device(parent_device_gid=2),
        4: Device(),
    }
    explicit, nested_circuits = energy_dashboard_monitor_roles(devices, [1])
    assert explicit == {1}
    assert nested_circuits == {2}


def test_nested_subpanel_exposes_its_circuits_recursively() -> None:
    devices = {
        1: Device(channels=[Channel("1")]),
        2: Device(parent_device_gid=1, channels=[Channel("1")]),
        3: Device(parent_device_gid=2, channels=[Channel("1")]),
    }
    branch_monitors, discrete_circuits = energy_dashboard_monitor_roles(devices, [1])
    assert branch_monitors == {1, 2, 3}
    assert discrete_circuits == set()


def test_nested_main_only_device_is_discrete_without_charger_metadata() -> None:
    """Telemetry shape still identifies an EVSE if merged metadata is absent."""
    devices = {
        1: Device(channels=[Channel("1")]),
        2: Device(parent_device_gid=1, channels=[Channel("1,2,3")]),
    }
    branch_monitors, discrete_circuits = energy_dashboard_monitor_roles(devices, [1])
    assert branch_monitors == {1}
    assert discrete_circuits == {2}


def test_parent_selection_wins_over_stored_nested_selection() -> None:
    """Legacy parent-and-child selections still add the child as one circuit."""
    devices = {
        1: Device(),
        2: Device(parent_device_gid=1, ev_charger=True),
        3: Device(parent_device_gid=2),
    }
    explicit, nested_circuits = energy_dashboard_monitor_roles(devices, [1, 2])
    assert explicit == {1}
    assert nested_circuits == {2}
