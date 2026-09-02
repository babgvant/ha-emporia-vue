"""Tests for native Emporia home discovery."""

from dataclasses import dataclass

from botocore.credentials import Credentials

from custom_components.emporia_vue import homes
from custom_components.emporia_vue.homes import get_homes, parse_homes


@dataclass
class Device:
    manufacturer_id: str


class Response:
    """Minimal requests response used by the home API tests."""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        """Stand in for requests.Response.raise_for_status."""
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self) -> dict:
        """Return the response payload."""
        return self._payload


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


def test_parse_homes_uses_authoritative_v1_device_mapping() -> None:
    """Modern device IDs map to usage GIDs without relying on legacy models."""
    sites = {
        "sites": [
            {"site_gid": 42, "display_name": "Lake House", "device_ids": ["A"]}
        ]
    }
    api_devices = {"devices": [{"device_id": "A", "device_gid": 101}]}
    assert parse_homes(sites, {}, api_devices)[0]["device_gids"] == [101]


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


def test_get_homes_uses_aws_signed_requests(monkeypatch) -> None:
    """Home requests use temporary AWS credentials and SigV4 signatures."""
    class Auth:
        tokens = {"id_token": "id-token"}

    class Vue:
        auth = Auth()

    monkeypatch.setattr(
        homes,
        "_get_aws_credentials",
        lambda vue: Credentials("access", "secret", "session"),
    )

    def get(url: str, **kwargs) -> Response:
        assert url in (
            "https://api.emporiaenergy.com/v1/customers/sites",
            "https://api.emporiaenergy.com/v1/customers/devices",
        )
        assert kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert kwargs["headers"]["X-Amz-Security-Token"] == "session"
        assert kwargs["timeout"] == (6.03, 10.03)
        payload = {"sites": []} if url.endswith("sites") else {"devices": []}
        return Response(200, payload)

    monkeypatch.setattr(homes.requests, "get", get)
    assert get_homes(Vue(), {}) == []
