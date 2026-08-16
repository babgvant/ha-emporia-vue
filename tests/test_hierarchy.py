"""Tests for Emporia monitor selection and entity naming."""

from dataclasses import dataclass, field

from custom_components.emporia_vue.hierarchy import (
    aggregate_root_gids,
    apply_usage_hierarchy,
    channel_name,
    device_identifier,
    merge_devices,
    monitor_options,
    selected_device_gids,
)


@dataclass
class Channel:
    channel_num: str
    name: str | None = None


@dataclass
class Device:
    device_gid: int
    device_name: str
    parent_device_gid: int = 0
    display_name: str = ""
    channels: list[Channel] = field(default_factory=list)


@dataclass
class UsageChannel:
    nested_devices: dict[int, "UsageDevice"] = field(default_factory=dict)


@dataclass
class UsageDevice:
    channels: dict[str, UsageChannel] = field(default_factory=dict)


def device_map() -> dict[int, Device]:
    """Create three roots and one nested monitor."""
    return merge_devices(
        [
            Device(1, "Monitor A"),
            Device(2, "Monitor A2", parent_device_gid=1),
            Device(3, "Monitor B"),
            Device(4, "Monitor C"),
        ]
    )


def test_selected_monitor_filters_other_roots_and_includes_descendants() -> None:
    """A selected root includes its nested tree only."""
    assert selected_device_gids(device_map(), ["1"]) == {1, 2}


def test_usage_data_supplies_missing_combined_monitor_relationships() -> None:
    """Nested usage identifies children absent from device metadata."""
    devices = merge_devices(
        [Device(1, "Main"), Device(2, "Subpanel"), Device(3, "Other")]
    )
    usage = {
        1: UsageDevice(
            channels={
                "1": UsageChannel(nested_devices={2: UsageDevice()})
            }
        ),
        3: UsageDevice(),
    }
    apply_usage_hierarchy(devices, usage)
    assert devices[2].parent_device_gid == 1
    assert selected_device_gids(devices, ["1"]) == {1, 2}
    assert monitor_options(devices) == {"1": "Main", "3": "Other"}


def test_configured_root_expands_usage_discovered_descendants() -> None:
    """A root-only usage response expands the stored root selection."""
    devices = merge_devices(
        [Device(1, "Main"), Device(2, "South"), Device(3, "North")]
    )
    usage = {
        1: UsageDevice(
            channels={
                "1": UsageChannel(
                    nested_devices={
                        2: UsageDevice(
                            channels={
                                "2": UsageChannel(
                                    nested_devices={3: UsageDevice()}
                                )
                            }
                        )
                    }
                )
            }
        )
    }
    apply_usage_hierarchy(devices, usage)
    assert selected_device_gids(devices, ["1"]) == {1, 2, 3}


def test_polling_gids_come_only_from_selected_tree() -> None:
    """The runtime usage request cannot include excluded root GIDs."""
    devices = device_map()
    included = selected_device_gids(devices, ["1"])
    polling_gids = [str(gid) for gid in devices if gid in included]
    assert polling_gids == ["1", "2"]


def test_virtual_home_uses_only_selected_roots() -> None:
    """Nested sub-panels remain visible without being added to the home total."""
    devices = device_map()
    assert aggregate_root_gids(devices, ["1", "2", "4"]) == [1, 4]


def test_new_selected_root_joins_virtual_home() -> None:
    """Reconfiguration can add a new root to the stable virtual aggregate."""
    devices = device_map()
    assert aggregate_root_gids(devices, ["1"]) == [1]
    assert aggregate_root_gids(devices, ["1", "3"]) == [1, 3]


def test_visible_monitor_can_be_excluded_from_virtual_home() -> None:
    """Polling selection and virtual aggregation are independent."""
    devices = device_map()
    assert selected_device_gids(devices, ["1", "3"]) == {1, 2, 3}
    assert aggregate_root_gids(devices, ["1"]) == [1]


def test_missing_selection_preserves_all_devices() -> None:
    """Legacy entries without a filter retain the old behavior."""
    assert selected_device_gids(device_map(), None) == {1, 2, 3, 4}


def test_multiple_selected_monitors() -> None:
    """More than one root can be selected."""
    assert selected_device_gids(device_map(), ["1", "4"]) == {1, 2, 4}


def test_only_top_level_monitors_are_options() -> None:
    """Children are included implicitly and are not separately selectable."""
    assert monitor_options(device_map()) == {
        "1": "Monitor A",
        "3": "Monitor B",
        "4": "Monitor C",
    }


def test_duplicate_and_unnamed_ct_names_fall_back_to_circuit_number() -> None:
    """Repeated monitor names do not create indistinguishable entities."""
    device = Device(1, "Load Center North")
    assert channel_name(device, Channel("4")) == "Circuit 4"
    assert channel_name(device, Channel("5", "Load Center North")) == "Circuit 5"
    assert channel_name(device, Channel("6", "Kitchen")) == "Kitchen"


def test_all_channels_share_one_physical_device_identifier() -> None:
    """Channel numbers do not create additional HA device identifiers."""
    device = Device(1, "Load Center North")
    assert {
        device_identifier(device)
        for _channel in (Channel("1,2,3"), Channel("4"), Channel("5"))
    } == {"1"}


def test_aggregate_channels_are_named_intentionally() -> None:
    """Main stays unprefixed while directional and balance data is explicit."""
    device = Device(1, "Load Center North")
    assert channel_name(device, Channel("1,2,3")) is None
    assert channel_name(device, Channel("MainsFromGrid")) == "Mains From Grid"
    assert channel_name(device, Channel("MainsToGrid")) == "Mains To Grid"
    assert channel_name(device, Channel("Balance")) == "Balance"
