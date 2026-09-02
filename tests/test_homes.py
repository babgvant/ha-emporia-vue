"""Tests for native Emporia home discovery."""

from dataclasses import dataclass

from custom_components.emporia_vue.homes import parse_homes


@dataclass
class Device:
    manufacturer_id: str


def test_parse_homes_preserves_names_and_maps_device_ids() -> None:
    """Native site membership is converted to IDs used by usage polling."""
    payload = {
        "sites": [
            {
                "site_gid": 42,
                "display_name": "Lake House",
                "device_ids": ["MONITOR-A", "CHARGER", "MONITOR-B"],
            }
        ]
    }

    assert parse_homes(
        payload,
        {101: Device("MONITOR-A"), 102: Device("MONITOR-B")},
    ) == [
        {
            "site_gid": "42",
            "name": "Lake House",
            "device_gids": [101, 102],
        }
    ]


def test_parse_homes_uses_stable_fallback_name() -> None:
    """A missing display name does not prevent native home support."""
    payload = {
        "sites": [
            {"site_gid": 42, "display_name": "", "device_ids": ["MONITOR-A"]}
        ]
    }
    assert parse_homes(payload, {101: Device("MONITOR-A")})[0]["name"] == (
        "Emporia Home 42"
    )


def test_parse_homes_ignores_invalid_or_empty_responses() -> None:
    """Accounts without native homes remain backward compatible."""
    assert parse_homes(None, {}) == []
    assert parse_homes({}, {}) == []
    assert parse_homes({"sites": []}, {}) == []
