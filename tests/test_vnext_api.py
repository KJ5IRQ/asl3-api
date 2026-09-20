"""HTTP-contract tests for the v1 API. No AMI or live node access occurs."""

from types import SimpleNamespace

from conftest import TEST_API_KEY, TEST_NODE, _StubConfig
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.util import get_remote_address

from vnext import software
from vnext.api import install_api
from vnext.models import Operation, now


class ApiConfig(_StubConfig):
    def __init__(self, credentials=None):
        self._credentials = credentials

    def get(self, key, default=None):
        if key == "api.credentials" and self._credentials is not None:
            return self._credentials
        return default


class FakePolicy:
    def characteristics(self):
        return {
            "link_node": {"active": "deny", "unknown": "deny"},
            "announce": {"active": "deny", "unknown": "deny"},
            "unlink_node": {"active": "allow", "unknown": "allow"},
            "unlink_all": {"active": "allow", "unknown": "allow"},
        }


class FakeLedger:
    def __init__(self):
        self.operations = {}

    def get(self, operation_id):
        return self.operations.get(operation_id)

    def list(self, limit=100, offset=0):
        values = list(self.operations.values())
        return values[offset : offset + limit]


class FakeRuntime:
    def __init__(self):
        self.healthy = True
        self.owner = SimpleNamespace(fd=1)
        self.closing = False
        self.transport = SimpleNamespace(contract_version="fake/1")
        self.policy = FakePolicy()
        self.ledger = FakeLedger()
        self.admissions = []
        self.software = software.undetected()
        self.software_probes = 0

    async def software_evidence(self):
        self.software_probes += 1
        return self.software

    def require_ledger(self):
        return self.ledger

    def admit(self, kind, body, credential, key=None):
        self.admissions.append((kind, body, credential, key))
        timestamp = now()
        operation = Operation(
            id=f"op-{len(self.admissions)}",
            node=TEST_NODE,
            kind=kind,
            request=body,
            credential=credential,
            created_at=timestamp,
            updated_at=timestamp,
            dispatch_status="QUEUED",
            effect_status="PENDING",
            terminal=False,
        )
        self.ledger.operations[operation.id] = operation
        return operation


class FakeCache:
    size = 0
    last_updated = None

    def lookup(self, node):
        return {
            "node": node,
            "callsign": None,
            "description": None,
            "location": None,
        }


def build_client(config=None):
    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    runtime = FakeRuntime()
    app.state.platform = runtime
    install_api(app, config or ApiConfig(), FakeCache(), limiter)
    return TestClient(app), runtime


def test_v1_requires_header_authentication():
    client, _ = build_client()
    response = client.get("/v1/capabilities")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_control_returns_202_operation_and_location():
    client, runtime = build_client()
    response = client.post(
        "/v1/announcements",
        headers={
            "X-API-Key": TEST_API_KEY,
            "Idempotency-Key": "logical-operation-1",
        },
        json={"kind": "time"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "announce"
    assert body["dispatch_status"] == "QUEUED"
    assert response.headers["location"] == f"/v1/operations/{body['id']}"
    assert runtime.admissions == [
        (
            "announce",
            {"kind": "time"},
            "legacy",
            "logical-operation-1",
        )
    ]


def test_observe_only_credential_cannot_control():
    config = ApiConfig(
        [
            {"name": "reader", "key": "read-key", "authority": ["observe"]},
            {"name": "operator", "key": "control-key", "authority": ["control"]},
        ]
    )
    client, _ = build_client(config)
    assert client.get(
        "/v1/capabilities",
        headers={"X-API-Key": "read-key"},
    ).status_code == 200

    denied = client.post(
        "/v1/announcements",
        headers={"X-API-Key": "read-key"},
        json={"kind": "time"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "AUTHORITY_DENIED"

    allowed = client.get(
        "/v1/capabilities",
        headers={"X-API-Key": "control-key"},
    )
    assert allowed.status_code == 200


def test_v1_events_never_accept_query_string_secret():
    client, _ = build_client()
    response = client.get(f"/v1/events?api_key={TEST_API_KEY}")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_operation_lookup_uses_same_durable_resource():
    client, _ = build_client()
    created = client.post(
        "/v1/announcements",
        headers={"X-API-Key": TEST_API_KEY},
        json={"kind": "version"},
    )
    operation_id = created.json()["id"]
    fetched = client.get(
        f"/v1/operations/{operation_id}",
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == operation_id


def capabilities(client):
    return client.get(
        "/v1/capabilities",
        headers={"X-API-Key": TEST_API_KEY},
    )


def test_capabilities_report_detected_backend_software_versions():
    client, runtime = build_client()
    runtime.software = software.evidence(
        {"response": "Success", "asteriskversion": "22.5.2"},
        {"response": "Success", "output": "app_rpt version: 1.2.3"},
    )
    response = capabilities(client)
    assert response.status_code == 200
    data = response.json()

    assert data["backend_software"]["asterisk"] == {
        "version": "22.5.2",
        "detected": True,
        "source": "ami:CoreSettings/AsteriskVersion",
    }
    assert data["backend_software"]["app_rpt"] == {
        "version": "1.2.3",
        "detected": True,
        "source": "ami:Command/rpt show version",
    }
    # The flat fields are a mirror of the app_rpt entry, so they cannot drift.
    assert (
        data["backend_software_version"]
        == data["backend_software"]["app_rpt"]["version"]
    )
    assert (
        data["backend_software_version_detected"]
        is data["backend_software"]["app_rpt"]["detected"]
    )
    # Contract versions stay distinct from software versions.
    assert data["result_contract_version"] == "1.0"
    assert data["backend_contract_version"] == "fake/1"
    assert runtime.software_probes == 1
    assert runtime.admissions == []


def test_capabilities_report_unknown_versions_without_inventing_one():
    client, runtime = build_client()
    data = capabilities(client).json()

    assert data["backend_software_version"] is None
    assert data["backend_software_version_detected"] is False
    for component in ("asterisk", "app_rpt"):
        entry = data["backend_software"][component]
        assert entry["version"] is None
        assert entry["detected"] is False
        assert entry["source"]
    assert data["backend_software"]["method"]
    assert runtime.admissions == []
