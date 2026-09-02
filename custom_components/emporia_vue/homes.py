"""Discovery helpers for Emporia's native home (site) groupings."""

from __future__ import annotations

from typing import Any

SITES_PATH = "v1/customers/sites"


def parse_homes(payload: Any, devices: dict[int, Any]) -> list[dict[str, Any]]:
    """Map Emporia sites to the numeric device GIDs used by usage requests."""
    if not isinstance(payload, dict) or not isinstance(payload.get("sites"), list):
        return []

    manufacturer_gids = {
        str(device.manufacturer_id): gid
        for gid, device in devices.items()
        if getattr(device, "manufacturer_id", None)
    }
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


def get_homes(vue: Any, devices: dict[int, Any]) -> list[dict[str, Any]]:
    """Fetch native Emporia homes using PyEmVue's authenticated session."""
    attempted_tokens: set[str] = set()
    response = None
    for token_name in ("access_token", "access_token", "id_token"):
        token = vue.auth.tokens.get(token_name)
        if not token or token in attempted_tokens:
            continue
        attempted_tokens.add(token)
        response = vue.auth.request(
            "get",
            SITES_PATH,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 401:
            break
        # PyEmVue refreshes its tokens when it sees a 401. Re-read the access
        # token on the next iteration before falling back to the ID token.
    if response is None:
        raise ValueError("No Emporia authentication token is available")
    response.raise_for_status()
    return parse_homes(response.json(), devices)
