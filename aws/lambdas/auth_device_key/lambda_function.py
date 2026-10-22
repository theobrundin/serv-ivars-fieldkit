import hmac
import os

import boto3

dynamodb = boto3.resource("dynamodb")
devices = dynamodb.Table(os.environ.get("DEVICES_TABLE", "Devices"))


def _get_header(event, name):
    """Case-insensitive header lookup. HTTP API v2 lowercases headers,
    but be defensive."""
    headers = event.get("headers") or {}
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def _requested_location_id(event):
    """Pull location_id (or the legacy locationId) from the query string, if
    the route has one. Returns None for routes with no such param (e.g. the
    device ingest routes), which is what makes this check a no-op for them."""
    qs = event.get("queryStringParameters") or {}
    return qs.get("location_id") or qs.get("locationId")


def _allowed_location_ids(item):
    """Locations this device key may read. Defaults to the device's own
    location_id. A dashboard key can list sister sites in
    allowed_location_ids (list, string-set, or comma-separated string)
    without becoming a fleet-wide key."""
    raw = item.get("allowed_location_ids")
    allowed = set()
    if isinstance(raw, (list, set, tuple)):
        allowed = {str(value).strip() for value in raw if str(value).strip()}
    elif isinstance(raw, str) and raw.strip():
        allowed = {part.strip() for part in raw.split(",") if part.strip()}
    if allowed:
        return allowed
    device_location_id = item.get("location_id", "")
    return {device_location_id} if device_location_id else set()


def handler(event, context):
    device_id = _get_header(event, "x-device-id") or ""
    api_key = _get_header(event, "x-api-key") or ""

    if not device_id or not api_key:
        return {"isAuthorized": False}

    try:
        result = devices.get_item(Key={"device_id": device_id})
        item = result.get("Item")

        if not item or item.get("status") != "active":
            return {"isAuthorized": False}

        stored_key = str(item.get("api_key", ""))

        # Constant-time comparison — prevents timing attacks.
        if not hmac.compare_digest(stored_key, api_key):
            return {"isAuthorized": False}

        device_location_id = item.get("location_id", "")

        # If a dashboard read route asks for a location, the authenticated
        # key must explicitly be allowed to read it. Ingest routes have no
        # location query parameter and keep their existing behavior.
        requested_location_id = _requested_location_id(event)
        if (
            requested_location_id
            and requested_location_id not in _allowed_location_ids(item)
        ):
            print(
                f"AUTH DENY: device {device_id} (location={device_location_id}) "
                f"requested location={requested_location_id}"
            )
            return {"isAuthorized": False}

        return {
            "isAuthorized": True,
            "context": {
                "deviceId": device_id,
                "locationId": device_location_id,
            },
        }

    except Exception as exc:
        print(f"AUTH ERROR: {exc}")
        return {"isAuthorized": False}
