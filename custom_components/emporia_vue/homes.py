"""Discovery helpers for Emporia's native home (site) groupings."""

from __future__ import annotations

from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
import requests

API_ROOT = "https://api.emporiaenergy.com"
AWS_REGION = "us-east-2"
AWS_SERVICE = "execute-api"
IDENTITY_POOL_ID = "us-east-2:4078e9e8-ab5b-4075-a42d-2bd65ac37ccd"
USER_POOL_ID = "us-east-2_ghlOXVLi1"
COGNITO_PROVIDER = f"cognito-idp.{AWS_REGION}.amazonaws.com/{USER_POOL_ID}"
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


def _get_aws_credentials(vue: Any) -> Credentials:
    """Exchange the Cognito ID token for credentials used by the v1 API."""
    id_token = vue.auth.tokens.get("id_token")
    if not id_token:
        raise ValueError("No Emporia ID token is available")

    client = boto3.client("cognito-identity", region_name=AWS_REGION)
    logins = {COGNITO_PROVIDER: id_token}
    identity_id = client.get_id(
        IdentityPoolId=IDENTITY_POOL_ID,
        Logins=logins,
    )["IdentityId"]
    raw_credentials = client.get_credentials_for_identity(
        IdentityId=identity_id,
        Logins=logins,
    )["Credentials"]
    return Credentials(
        raw_credentials["AccessKeyId"],
        raw_credentials["SecretKey"],
        raw_credentials["SessionToken"],
    )


def _request_v1(vue: Any, path: str, credentials: Credentials) -> Any:
    """Make an AWS SigV4-authenticated request to Emporia's v1 API."""
    url = f"{API_ROOT}/{path}"
    aws_request = AWSRequest(method="GET", url=url)
    SigV4Auth(credentials, AWS_SERVICE, AWS_REGION).add_auth(aws_request)
    response = requests.get(
        url,
        headers=dict(aws_request.headers.items()),
        timeout=(
            getattr(vue, "connect_timeout", 6.03),
            getattr(vue, "read_timeout", 10.03),
        ),
    )
    response.raise_for_status()
    return response


def get_homes(vue: Any, devices: dict[int, Any]) -> list[dict[str, Any]]:
    """Fetch native Emporia homes using authoritative v1 device identifiers."""
    credentials = _get_aws_credentials(vue)
    sites = _request_v1(vue, SITES_PATH, credentials).json()
    api_devices = _request_v1(vue, DEVICES_PATH, credentials).json()
    return parse_homes(sites, devices, api_devices)
