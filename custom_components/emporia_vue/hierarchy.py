"""Helpers for selecting and presenting the Emporia device hierarchy."""

from collections.abc import Iterable
from typing import Any


def merge_devices(devices: Iterable[Any]) -> dict[int, Any]:
    """Merge PyEmVue's duplicate device records while retaining all channels."""
    merged: dict[int, Any] = {}
    for device in devices:
        if device.device_gid not in merged:
            merged[device.device_gid] = device
        else:
            known = {channel.channel_num for channel in merged[device.device_gid].channels}
            merged[device.device_gid].channels.extend(
                channel for channel in device.channels if channel.channel_num not in known
            )
    return merged


def top_level_devices(devices: dict[int, Any]) -> list[Any]:
    """Return selectable roots; tolerate missing or stale parent references."""
    gids = set(devices)
    return [
        device
        for device in devices.values()
        if not device.parent_device_gid or device.parent_device_gid not in gids
    ]


def selected_device_gids(
    devices: dict[int, Any], selected_roots: Iterable[str] | None
) -> set[int]:
    """Expand selected roots to their complete descendant trees.

    ``None`` deliberately means no filter for backward compatibility. An empty
    collection is a real selection and therefore includes no devices.
    """
    if selected_roots is None:
        return set(devices)
    included = {int(gid) for gid in selected_roots if int(gid) in devices}
    changed = True
    while changed:
        changed = False
        for gid, device in devices.items():
            if gid not in included and device.parent_device_gid in included:
                included.add(gid)
                changed = True
    return included


def monitor_options(devices: dict[int, Any]) -> dict[str, str]:
    """Build stable-GID/human-name options for top-level monitors."""
    options: dict[str, str] = {}
    for device in sorted(
        top_level_devices(devices),
        key=lambda item: ((item.display_name or item.device_name or "").casefold(), item.device_gid),
    ):
        name = device.display_name or device.device_name or f"Emporia monitor {device.device_gid}"
        if name in options.values():
            name = f"{name} ({device.device_gid})"
        options[str(device.device_gid)] = name
    return options


def device_identifier(device: Any) -> str:
    """Return the one HA device-registry identifier for physical hardware."""
    return str(device.device_gid)


def aggregate_root_gids(
    devices: dict[int, Any], selected_roots: Iterable[str]
) -> list[int]:
    """Return selected aggregate sources without double-counting descendants."""
    selected = {int(gid) for gid in selected_roots if int(gid) in devices}
    return [
        gid
        for gid in devices
        if gid in selected and devices[gid].parent_device_gid not in selected
    ]


def channel_name(device: Any, channel: Any) -> str | None:
    """Return an entity-name prefix for a channel, or None for main usage."""
    number = str(channel.channel_num)
    name = (channel.name or "").strip()
    device_names = {value for value in (device.device_name, device.display_name) if value}
    if number == "1,2,3":
        return None
    if number.isdigit():
        return name if name and name not in device_names else f"Circuit {number}"
    special = {
        "MainsFromGrid": "Mains From Grid",
        "MainsToGrid": "Mains To Grid",
        "Balance": "Balance",
        "TotalUsage": "Total Usage",
    }
    return name if name and name not in device_names else special.get(number, number)
