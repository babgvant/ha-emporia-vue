"""Tests for native Emporia home discovery."""

from dataclasses import dataclass

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


def test_get_homes_uses_bearer_access_token() -> None:
    """The v1 sites API receives the authorization scheme used by the web app."""
    class Auth:
        tokens = {"access_token": "access-token", "id_token": "id-token"}

        def request(self, method: str, path: str, **kwargs) -> Response:
            assert method == "get"
            assert path in ("v1/customers/sites", "v1/customers/devices")
            assert kwargs["headers"] == {"Authorization": "Bearer access-token"}
            payload = {"sites": []} if path.endswith("sites") else {"devices": []}
            return Response(200, payload)

    class Vue:
        auth = Auth()

    assert get_homes(Vue(), {}) == []


def test_get_homes_rebuilds_header_after_token_refresh() -> None:
    """A 401 retry uses the access token refreshed by PyEmVue."""
    class Auth:
        def __init__(self) -> None:
            self.tokens = {"access_token": "old-token", "id_token": "id-token"}
            self.calls = 0

        def request(self, method: str, path: str, **kwargs) -> Response:
            self.calls += 1
            if self.calls == 1:
                assert kwargs["headers"]["Authorization"] == "Bearer old-token"
                self.tokens["access_token"] = "new-token"
                return Response(401, {})
            assert kwargs["headers"]["Authorization"] == "Bearer new-token"
            payload = {"sites": []} if path.endswith("sites") else {"devices": []}
            return Response(200, payload)

    class Vue:
        auth = Auth()

    assert get_homes(Vue(), {}) == []
    assert Vue.auth.calls == 3


def test_get_homes_falls_back_to_bearer_id_token() -> None:
    """Cognito deployments that authorize with the ID token are supported."""
    class Auth:
        tokens = {"access_token": "access-token", "id_token": "id-token"}
        calls = 0

        def request(self, method: str, path: str, **kwargs) -> Response:
            self.calls += 1
            token = kwargs["headers"]["Authorization"]
            payload = {"sites": []} if path.endswith("sites") else {"devices": []}
            return Response(200 if token == "Bearer id-token" else 403, payload)

    class Vue:
        auth = Auth()

    assert get_homes(Vue(), {}) == []
    assert Vue.auth.calls == 4
