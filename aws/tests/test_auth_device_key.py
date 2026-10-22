import importlib.util
import sys
import types
import unittest
from pathlib import Path


AUTH_PATH = (
    Path(__file__).resolve().parents[1]
    / "lambdas"
    / "auth_device_key"
    / "lambda_function.py"
)


class FakeDevicesTable:
    def __init__(self, items):
        self.items = {item["device_id"]: item for item in items}

    def get_item(self, Key):
        item = self.items.get(Key["device_id"])
        return {"Item": item} if item else {}


def load_auth(items):
    fake_table = FakeDevicesTable(items)
    boto3 = types.ModuleType("boto3")
    boto3.resource = lambda service: types.SimpleNamespace(
        Table=lambda name: fake_table
    )
    previous = sys.modules.get("boto3")
    sys.modules["boto3"] = boto3
    try:
        spec = importlib.util.spec_from_file_location(
            "auth_device_key", AUTH_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = previous
    return module


def event(device_id="dashboard-ljs", api_key="secret", location_id=None):
    payload = {
        "headers": {"x-device-id": device_id, "x-api-key": api_key},
        "queryStringParameters": {},
    }
    if location_id is not None:
        payload["queryStringParameters"]["location_id"] = location_id
    return payload


DASHBOARD = {
    "device_id": "dashboard-ljs",
    "api_key": "secret",
    "status": "active",
    "location_id": "ljs-louisville-001",
    "allowed_location_ids": [
        "ljs-louisville-001",
        "ljs-louisville-002",
        "ljs-lexington-001",
    ],
}


class AuthDeviceKeyTests(unittest.TestCase):
    def test_matching_home_location_is_allowed(self):
        auth = load_auth([DASHBOARD])
        result = auth.handler(event(location_id="ljs-louisville-001"), None)
        self.assertTrue(result["isAuthorized"])

    def test_sister_location_on_allowlist_is_allowed(self):
        auth = load_auth([DASHBOARD])
        for location_id in ("ljs-louisville-002", "ljs-lexington-001"):
            result = auth.handler(event(location_id=location_id), None)
            self.assertTrue(result["isAuthorized"], location_id)

    def test_unlisted_location_is_denied(self):
        auth = load_auth([DASHBOARD])
        result = auth.handler(event(location_id="qdoba-001"), None)
        self.assertFalse(result["isAuthorized"])

    def test_default_scope_is_the_device_location_only(self):
        item = {
            "device_id": "dashboard-ljs",
            "api_key": "secret",
            "status": "active",
            "location_id": "ljs-louisville-001",
        }
        auth = load_auth([item])
        self.assertTrue(
            auth.handler(
                event(location_id="ljs-louisville-001"), None
            )["isAuthorized"]
        )
        self.assertFalse(
            auth.handler(
                event(location_id="ljs-lexington-001"), None
            )["isAuthorized"]
        )

    def test_ingest_route_without_location_id_is_allowed(self):
        auth = load_auth([DASHBOARD])
        self.assertTrue(auth.handler(event(), None)["isAuthorized"])

    def test_wrong_key_or_inactive_device_is_denied(self):
        auth = load_auth([DASHBOARD])
        result = auth.handler(
            event(api_key="nope", location_id="ljs-louisville-001"), None
        )
        self.assertFalse(result["isAuthorized"])

        auth = load_auth([dict(DASHBOARD, status="revoked")])
        result = auth.handler(event(location_id="ljs-louisville-001"), None)
        self.assertFalse(result["isAuthorized"])


if __name__ == "__main__":
    unittest.main()
