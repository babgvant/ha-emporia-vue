"""Tests for Energy Dashboard bulk configuration helpers."""

from dataclasses import dataclass
from types import SimpleNamespace

from custom_components.emporia_vue.energy_dashboard import (
    circuit_energy_unique_id,
    descendant_gids,
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
