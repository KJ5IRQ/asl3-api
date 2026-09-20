"""Contract tests for the withdrawn `time` and `version` announcements.

These run HTTP against a real Platform with a real SQLite ledger and a
recording transport, so "no AMI bytes" and "no admitted operation" are observed
rather than inferred from a status code. A status-only test would still pass if
the refusal happened after admission.

`time` and `version` map to `status 2` / `status 3`, which app_rpt delivers as
link telemetry text. Whether anything is spoken is decided by the receiving
node, so the API cannot honestly advertise them. The mappings were correct; the
product promise was not.
"""

import pytest
from conftest import TEST_API_KEY, TEST_NODE
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from slowapi import Limiter
from slowapi.util import get_remote_address
from test_vnext import FakeObserver, FakeTransport, TestConfig, state

from vnext.api import install_api
from vnext.models import (
    SUPPORTED_ANNOUNCEMENTS,
    WITHDRAWN_ANNOUNCEMENTS,
    AnnouncementRequest,
    ProblemError,
    command_for,
)
from vnext.service import Platform

WITHDRAWN = list(WITHDRAWN_ANNOUNCEMENTS)
SUPPORTED = list(SUPPORTED_ANNOUNCEMENTS)


class ApiConfig(TestConfig):
    """Real platform config plus a control credential for the HTTP layer."""

    def get(self, key, default=None):
        if key == "api.credentials":
            return [
                {
                    "name": "operator",
                    "key": TEST_API_KEY,
                    "authority": ["observe", "control"],
                }
            ]
        return super().get(key, default)


class FakeCache:
    size = 0
    last_updated = None

    def lookup(self, node):
        return {"node": node, "callsign": None, "description": None, "location": None}


@pytest.fixture
def live(tmp_path):
    """HTTP client over a real Platform, real ledger, recording transport."""
    transport = FakeTransport()
    config = ApiConfig(tmp_path)
    platform = Platform(config, transport=transport, observer=FakeObserver([state()]))
    platform.start()

    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.state.platform = platform
    install_api(app, config, FakeCache(), limiter)
    try:
        yield TestClient(app), platform, transport
    finally:
        platform.ledger.db.close()


def auth():
    return {"X-API-Key": TEST_API_KEY}


def ledger_count(platform):
    row = platform.ledger.db.execute("SELECT COUNT(*) FROM operations").fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Supported kinds still work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", SUPPORTED)
def test_supported_announcement_is_accepted(live, kind):
    client, platform, _ = live
    response = client.post("/v1/announcements", headers=auth(), json={"kind": kind})
    assert response.status_code == 202
    assert response.json()["kind"] == "announce"
    assert ledger_count(platform) == 1


def test_supported_set_is_exactly_identify_and_status():
    assert SUPPORTED_ANNOUNCEMENTS == ("identify", "status")


# ---------------------------------------------------------------------------
# Withdrawn kinds cannot cross the admission or dispatch boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", WITHDRAWN)
def test_withdrawn_announcement_is_refused(live, kind):
    client, _, _ = live
    response = client.post("/v1/announcements", headers=auth(), json={"kind": kind})
    assert response.status_code == 422
    assert response.status_code != 202
    body = response.json()
    assert body["code"] == "UNSUPPORTED_ANNOUNCEMENT"
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("kind", WITHDRAWN)
def test_withdrawn_announcement_writes_no_ledger_row(live, kind):
    """No accepted operation exists, so nothing can later be dispatched."""
    client, platform, _ = live
    client.post("/v1/announcements", headers=auth(), json={"kind": kind})
    assert ledger_count(platform) == 0
    assert platform.ledger.list() == []


@pytest.mark.parametrize("kind", WITHDRAWN)
def test_withdrawn_announcement_sends_no_ami_bytes(live, kind):
    """The transport is the only path to AMI; it must never be called."""
    client, _, transport = live
    client.post("/v1/announcements", headers=auth(), json={"kind": kind})
    assert transport.commands == []


@pytest.mark.parametrize("kind", WITHDRAWN)
def test_withdrawn_announcement_returns_no_operation_id(live, kind):
    client, _, _ = live
    body = client.post("/v1/announcements", headers=auth(), json={"kind": kind}).json()
    assert "id" not in body
    assert "Location" not in body


@pytest.mark.parametrize("kind", WITHDRAWN)
def test_withdrawn_announcement_never_reserves_the_control_slot(live, kind):
    """A refused request must not leave the node marked busy."""
    client, platform, _ = live
    client.post("/v1/announcements", headers=auth(), json={"kind": kind})
    assert platform.busy_operation_id is None
    # A supported announcement immediately afterwards must still be admissible.
    assert (
        client.post(
            "/v1/announcements", headers=auth(), json={"kind": "identify"}
        ).status_code
        == 202
    )


# ---------------------------------------------------------------------------
# Legacy compatibility routes
#
# These live on the asl_agent application, not on the bare v1 router, so they
# need the real app with a real Platform attached.
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy(tmp_path):
    import asl_agent

    transport = FakeTransport()
    config = ApiConfig(tmp_path)
    platform = Platform(config, transport=transport, observer=FakeObserver([state()]))
    platform.start()

    previous = getattr(asl_agent.app.state, "platform", None)
    asl_agent.app.state.platform = platform
    try:
        yield TestClient(asl_agent.app), platform, transport
    finally:
        asl_agent.app.state.platform = previous
        platform.ledger.db.close()


@pytest.mark.parametrize("path", ["/cop/time", "/cop/version"])
def test_legacy_route_still_exists(legacy, path):
    """Retained, not silently removed, so callers get a reason not a 404."""
    client, _, _ = legacy
    assert client.post(path, headers=auth()).status_code != 404


@pytest.mark.parametrize("path", ["/cop/time", "/cop/version"])
def test_legacy_route_refuses_explicitly(legacy, path):
    client, _, _ = legacy
    response = client.post(path, headers=auth())
    assert response.status_code == 422
    assert response.status_code != 202
    assert response.json()["code"] == "UNSUPPORTED_ANNOUNCEMENT"


@pytest.mark.parametrize("path", ["/cop/time", "/cop/version"])
def test_legacy_route_admits_nothing_and_dispatches_nothing(legacy, path):
    client, platform, transport = legacy
    client.post(path, headers=auth())
    assert ledger_count(platform) == 0
    assert transport.commands == []
    assert platform.busy_operation_id is None


@pytest.mark.parametrize("path", ["/cop/identify", "/cop/status"])
def test_supported_legacy_routes_still_admit(legacy, path):
    """The withdrawal must not damage the announcements that do work."""
    client, platform, _ = legacy
    response = client.post(path, headers=auth())
    assert response.status_code == 202
    assert ledger_count(platform) == 1


# ---------------------------------------------------------------------------
# Capabilities advertise exactly the supported set
# ---------------------------------------------------------------------------


def test_v1_capabilities_advertise_only_supported(live):
    client, _, _ = live
    features = client.get("/v1/capabilities", headers=auth()).json()["features"]
    assert features["announcements"] == ["identify", "status"]
    for kind in WITHDRAWN:
        assert kind not in features["announcements"]


def test_openapi_schema_offers_only_supported_kinds(live):
    """OpenAPI is the published contract; it must not offer withdrawn values."""
    client, _, _ = live
    schema = client.get("/openapi.json").json()
    request = schema["components"]["schemas"]["AnnouncementRequest"]
    assert request["properties"]["kind"]["enum"] == ["identify", "status"]


# ---------------------------------------------------------------------------
# Last-gate enforcement, independent of the HTTP schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", WITHDRAWN)
def test_command_for_refuses_withdrawn_kind(kind):
    """command_for() is crossed by every dispatch path, including legacy."""
    with pytest.raises(ProblemError) as exc:
        command_for(TEST_NODE, "announce", {"kind": kind})
    assert exc.value.code == "UNSUPPORTED_ANNOUNCEMENT"
    assert exc.value.status == 422


@pytest.mark.parametrize("kind", SUPPORTED)
def test_command_for_maps_supported_kind(kind):
    command = command_for(TEST_NODE, "announce", {"kind": kind})
    expected = {"identify": "status 1", "status": "ilink 5"}[kind]
    assert command == f"rpt cmd {TEST_NODE} {expected}"


def test_withdrawn_commands_are_unreachable_from_the_mapping():
    """status 2 and status 3 must not be producible by any supported kind."""
    produced = {
        command_for(TEST_NODE, "announce", {"kind": kind}) for kind in SUPPORTED
    }
    assert f"rpt cmd {TEST_NODE} status 2" not in produced
    assert f"rpt cmd {TEST_NODE} status 3" not in produced


@pytest.mark.parametrize("kind", WITHDRAWN)
def test_request_model_rejects_withdrawn_kind(kind):
    with pytest.raises(ValidationError):
        AnnouncementRequest(kind=kind)


# ---------------------------------------------------------------------------
# Error semantics: a withdrawn kind is distinguishable from a bogus one
# ---------------------------------------------------------------------------


def test_unknown_kind_is_invalid_request_not_unsupported_announcement(live):
    """A client must be able to tell "removed" apart from "nonsense"."""
    client, _, _ = live
    body = client.post("/v1/announcements", headers=auth(), json={"kind": "wat"}).json()
    assert body["code"] == "INVALID_REQUEST"


def test_withdrawn_kind_with_extra_field_still_reports_the_withdrawal(live):
    client, _, _ = live
    body = client.post(
        "/v1/announcements", headers=auth(), json={"kind": "time", "extra": 1}
    ).json()
    assert body["code"] == "UNSUPPORTED_ANNOUNCEMENT"


# ---------------------------------------------------------------------------
# Authentication still precedes the refusal on the legacy routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/cop/time", "/cop/version"])
def test_legacy_refusal_does_not_bypass_authentication(legacy, path):
    """The refusal must not become an unauthenticated oracle."""
    client, _, transport = legacy
    response = client.post(path, headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert transport.commands == []


# ---------------------------------------------------------------------------
# Operations admitted before the withdrawal
#
# The hub ran with `time` supported, so a durable ledger can already hold one.
# Upgrading must neither re-dispatch it nor make it unreadable.
# ---------------------------------------------------------------------------


def seed_withdrawn_operation(tmp_path, dispatch_status, kind="time"):
    from vnext.ledger import Ledger
    from vnext.models import Operation, now

    ledger = Ledger(tmp_path / "operations.sqlite3", TEST_NODE)
    stamp = now()
    operation = Operation(
        id=f"legacy-{kind}",
        node=TEST_NODE,
        kind="announce",
        request={"kind": kind},
        credential="operator",
        created_at=stamp,
        updated_at=stamp,
        dispatch_status=dispatch_status,
        effect_status="PENDING",
        terminal=False,
    )
    ledger.db.execute(
        "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)",
        (
            operation.id,
            TEST_NODE,
            operation.credential,
            None,
            Ledger.canonical("announce", operation.request),
            operation.model_dump_json(),
        ),
    )
    ledger.db.commit()
    return ledger, operation


@pytest.mark.parametrize("kind", WITHDRAWN)
@pytest.mark.parametrize(
    "dispatch_status,expected",
    [
        ("QUEUED", "NOT_DISPATCHED"),
        ("DISPATCH_STARTED", "OUTCOME_UNKNOWN"),
    ],
)
def test_preexisting_withdrawn_operation_terminalizes_without_redispatch(
    tmp_path, kind, dispatch_status, expected
):
    ledger, seeded = seed_withdrawn_operation(tmp_path, dispatch_status, kind)
    try:
        ledger.recover()
        recovered = ledger.get(seeded.id)
        assert recovered.terminal
        assert recovered.dispatch_status == expected
        # The historical request stays readable and is never re-derived.
        assert recovered.request == {"kind": kind}
    finally:
        ledger.db.close()


@pytest.mark.parametrize("kind", WITHDRAWN)
def test_preexisting_withdrawn_operation_remains_readable_over_http(tmp_path, kind):
    """Withdrawal narrows what can be requested, not what was recorded."""
    ledger, seeded = seed_withdrawn_operation(tmp_path, "QUEUED", kind)
    ledger.recover()
    ledger.db.close()

    transport = FakeTransport()
    config = ApiConfig(tmp_path)
    platform = Platform(config, transport=transport, observer=FakeObserver([state()]))
    platform.start()
    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.state.platform = platform
    install_api(app, config, FakeCache(), limiter)
    try:
        client = TestClient(app)
        response = client.get(f"/v1/operations/{seeded.id}", headers=auth())
        assert response.status_code == 200
        assert response.json()["request"] == {"kind": kind}
        assert transport.commands == []
    finally:
        platform.ledger.db.close()
