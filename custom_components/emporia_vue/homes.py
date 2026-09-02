"""Discovery helpers for Emporia's native home (site) groupings."""

from __future__ import annotations

from typing import Any

import requests

API_ROOT = "https://api.emporiaenergy.com"
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
    """Make a Cognito-authenticated request to Emporia's v1 API."""
    id_token = vue.auth.tokens.get("id_token")
    access_token = vue.auth.tokens.get("access_token")
    if not id_token and not access_token:
        raise ValueError("No Emporia authentication token is available")
    url = f"{API_ROOT}/{path}"
    candidates = (
        ("raw ID token", id_token),
        ("raw access token", access_token),
        ("Bearer ID token", f"Bearer {id_token}" if id_token else None),
        (
            "Bearer access token",
            f"Bearer {access_token}" if access_token else None,
        ),
    )
    attempted_values: set[str] = set()
    response = None
    attempted_schemes: list[str] = []
    for scheme, token_value in candidates:
        if not token_value or token_value in attempted_values:
            continue
        attempted_values.add(token_value)
        attempted_schemes.append(scheme)
        response = requests.get(
            url,
            headers={"Authorization": token_value},
            timeout=(
                getattr(vue, "connect_timeout", 6.03),
                getattr(vue, "read_timeout", 10.03),
            ),
        )
        if response.status_code not in (401, 403):
            break
    if response is None:
        raise ValueError("No usable Emporia authentication token is available")
    if response.status_code in (401, 403):
        detail = response.text.strip().replace("\n", " ")[:300]
        raise PermissionError(
            f"Emporia home API rejected {', '.join(attempted_schemes)} "
            f"with HTTP {response.status_code}: {detail or '<empty response>'}"
        )
    response.raise_for_status()
    return response


def get_homes(vue: Any, devices: dict[int, Any]) -> list[dict[str, Any]]:
    """Fetch native Emporia homes using authoritative v1 device identifiers."""
    sites = _request_v1(vue, SITES_PATH).json()
    api_devices = _request_v1(vue, DEVICES_PATH).json()
    return parse_homes(sites, devices, api_devices)
