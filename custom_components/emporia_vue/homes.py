"""Discovery helpers for Emporia's native home (site) groupings."""

from __future__ import annotations

from typing import Any

SITES_PATH = "v1/customers/sites"
DEVICES_PATH = "v1/customers/devices"


def parse_homes(
    payload: Any,
    devices: dict[int, Any],
    device_payload: Any = None,
) -> list[dict[str, Any]]:
    """Map Emporia sites to the numeric device GIDs used by usage requests."""
    if not isinstance(payload, dict) or not isinstance(payload.get("sites"), list):
        return []

    manufacturer_gids = {
        str(device.manufacturer_id): gid
        for gid, device in devices.items()
        if getattr(device, "manufacturer_id", None)
    }
    if isinstance(device_payload, dict) and isinstance(
        device_payload.get("devices"), list
    ):
        manufacturer_gids.update(
            {
                str(device["device_id"]): int(device["device_gid"])
                for device in device_payload["devices"]
                if isinstance(device, dict)
                and device.get("device_id") is not None
                and device.get("device_gid") is not None
            }
        )
    homes: list[dict[str, Any]] = []
    for site in payload["sites"]:
        if not isinstance(site, dict):
            continue
        site_gid = site.get("site_gid")
        device_ids = site.get("device_ids")
        if site_gid is None or not isinstance(device_ids, list):
            continue
        device_gids = list(
            dict.fromkeys(
                manufacturer_gids[str(device_id)]
                for device_id in device_ids
                if str(device_id) in manufacturer_gids
            )
        )
        if device_gids:
            homes.append(
                {
                    "site_gid": str(site_gid),
                    "name": str(site.get("display_name") or f"Emporia Home {site_gid}"),
                    "device_gids": device_gids,
                }
            )
    return homes


def _request_v1(vue: Any, path: str) -> Any:
    """Make an authenticated request to Emporia's bearer-token v1 API."""
    attempted_tokens: set[str] = set()
    response = None
    for token_name in ("access_token", "access_token", "id_token"):
        token = vue.auth.tokens.get(token_name)
        if not token or token in attempted_tokens:
            continue
        attempted_tokens.add(token)
        response = vue.auth.request(
            "get",
            path,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 401:
            break
        # PyEmVue refreshes its tokens when it sees a 401. Re-read the access
        # token on the next iteration before falling back to the ID token.
    if response is None:
        raise ValueError("No Emporia authentication token is available")
    response.raise_for_status()
    return response


def get_homes(vue: Any, devices: dict[int, Any]) -> list[dict[str, Any]]:
    """Fetch native Emporia homes using authoritative v1 device identifiers."""
    sites = _request_v1(vue, SITES_PATH).json()
    api_devices = _request_v1(vue, DEVICES_PATH).json()
    return parse_homes(sites, devices, api_devices)
