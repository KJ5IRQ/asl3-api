"""Fault-focused vNext tests. All AMI I/O is fake; no live node is touched."""
import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
from conftest import TEST_NODE, _StubConfig

from vnext.ledger import ControlOwner, Ledger
from vnext.models import DirectLink, NodeState, ProblemError, validate_node
from vnext.observation import Observer, parse_alinks, parse_snapshot
from vnext.service import Platform
from vnext.software import (
    APP_RPT_VERSION_ACTION,
    APP_RPT_VERSION_PATTERN,
    ASTERISK_VERSION_ACTION,
    parse_app_rpt_version,
    parse_asterisk_version,
)
from vnext.transport import AMITransport


def run(coro):
    return asyncio.run(coro)


def xstat(*, alinks="", count="0", conns=None, rx="0", tx="0", links=""):
    response = {
        "response": "Success",
        "node": TEST_NODE,
        "var": [
            f"RPT_RXKEYED={rx}",
            f"RPT_TXKEYED={tx}",
            f"RPT_NUMALINKS={count}",
            f"RPT_ALINKS={alinks}",
            f"RPT_LINKS={links}",
        ],
    }
    if conns:
        response["conn"] = [
            f"{name} 127.0.0.1 0 OUT 00:00:01 {status}"
            for name, status in conns
        ]
    return response


def sawstat(*entries):
    response = {"response": "Success", "node": TEST_NODE}
    if entries:
        response["conn"] = [
            f"{name} {1 if keyed else 0} 1 1"
            for name, keyed in entries
        ]
    return response


def state(*, links=None, traffic="CLEAR", complete=True):
    if not complete:
        return NodeState(
            node=TEST_NODE,
            observed_at="2026-09-19T00:00:00+00:00",
            connection_epoch="epoch",
            state_status="STATE_UNKNOWN",
            traffic_state="UNKNOWN",
            complete=False,
            reasons=["test unknown"],
        )
    return NodeState(
        node=TEST_NODE,
        observed_at="2026-09-19T00:00:00+00:00",
        connection_epoch="epoch",
        state_status="COMPLETE",
        traffic_state=traffic,
        complete=True,
        rx_keyed=traffic == "ACTIVE",
        tx_keyed=False,
        direct_links=links or [],
    )


def link(
    node="123",
    *,
    mode="T",
    keyed=False,
    connection_status="ESTABLISHED",
):
    try:
        allstar = validate_node(node)
    except ValueError:
        allstar = None
    return DirectLink(
        node=node,
        allstar_node=allstar,
        mode=mode,
        keyed=keyed,
        connection_status=connection_status,
    )


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "000000",
        "0123",
        "１２３",
        "١٢٣",
        "1\n",
        "+1",
        "1234567",
        "",
        "1 2",
    ],
)
def test_canonical_node_validation_rejects_shorthand_and_unicode(value):
    with pytest.raises(ValueError):
        validate_node(value)


@pytest.mark.parametrize("value", ["1", "123", "999999"])
def test_private_and_static_node_syntax_is_valid(value):
    assert validate_node(value) == value


def test_native_snapshot_combines_xstat_sawstat_and_foreign_links():
    xs = xstat(
        alinks="1,123TK",
        count="1",
        conns=[
            ("123", "ESTABLISHED"),
            ("3123456", "ESTABLISHED"),
            ("PHONE-P", "ESTABLISHED"),
        ],
        links="T123,R3123456",
    )
    saw = sawstat(
        ("123", True),
        ("3123456", False),
        ("PHONE-P", False),
    )
    result = parse_snapshot(xs, saw, TEST_NODE, "epoch")
    assert result.complete
    assert result.traffic_state == "ACTIVE"
    by_name = {item.node: item for item in result.direct_links}
    assert by_name["123"].allstar_node == "123"
    assert by_name["123"].mode == "T"
    assert by_name["3123456"].allstar_node is None
    assert by_name["3123456"].mode == "UNKNOWN"
    assert by_name["PHONE-P"].allstar_node is None


def test_local_monitor_and_non_allstar_names_do_not_poison_observation():
    parsed = parse_alinks("1,ABC-CLIENTLU", "1")
    assert parsed["ABC-CLIENT"]["mode"] == "L"


@pytest.mark.parametrize(
    "alinks,count",
    [
        ("1,000000RU", "1"),
        ("1,123TX", "1"),
        ("1,123TU,", "1"),
        ("2,123TU,123TK", "2"),
        ("", "1"),
        ("0", "0"),
        ("", "00"),
    ],
)
def test_malformed_or_sentinel_alinks_becomes_unknown(alinks, count):
    xs = xstat(
        alinks=alinks,
        count=count,
        conns=[("123", "ESTABLISHED")] if count not in {"0", "00"} else None,
    )
    result = parse_snapshot(
        xs,
        sawstat(("123", False)),
        TEST_NODE,
        "epoch",
    )
    assert not result.complete
    assert result.traffic_state == "UNKNOWN"
    assert result.direct_links is None


def test_topology_change_between_xstat_and_sawstat_is_unknown():
    xs = xstat(
        alinks="1,123TU",
        count="1",
        conns=[("123", "ESTABLISHED")],
    )
    result = parse_snapshot(
        xs,
        sawstat(("124", False)),
        TEST_NODE,
        "epoch",
    )
    assert result.state_status == "STATE_UNKNOWN"


def test_truncated_rpt_links_is_unknown():
    xs = xstat(links="R000000")
    result = parse_snapshot(
        xs,
        sawstat(),
        TEST_NODE,
        "epoch",
    )
    assert not result.complete


def test_stale_observation_generation_is_unknown():
    async def scenario():
        transport = AsyncMock()
        observer = Observer(TEST_NODE, transport)

        async def changed(node):
            observer.invalidate()
            return xstat(), sawstat(), "old-epoch"

        transport.observe.side_effect = changed
        result = await observer.snapshot()
        assert result.reasons == ["STALE_CONNECTION_EPOCH"]
        assert result.traffic_state == "UNKNOWN"

    run(scenario())


class TestConfig(_StubConfig):
    __test__ = False

    def __init__(self, path):
        self.settings = {
            "operations.state_directory": str(path),
            "operations.lock_directory": str(path),
            "operations.effect_timeout": 0.001,
            "operations.dispatch_timeout": 1,
            "timeouts.observation_seconds": 1,
            "timeouts.ami_seconds": 1,
        }

    def get(self, key, default=None):
        return self.settings.get(key, default)


class FakeObserver:
    def __init__(self, states):
        self.states = list(states)
        self.last = self.states[-1] if self.states else state()
        self.invalidated = False

    async def snapshot(self):
        if self.states:
            self.last = self.states.pop(0)
        return self.last

    def invalidate(self):
        self.invalidated = True


class FakeTransport:
    contract_version = "fake/1"

    def __init__(self):
        self.commands = []
        self.result = {"response": "Success"}
        self.fail_before = False
        self.fail_after = False
        self.after_barrier = None
        self.version_responses = (
            {"response": "Success", "asteriskversion": "22.5.2"},
            {"response": "Success", "output": "app_rpt version: 1.2.3"},
        )
        self.version_error = None
        self.version_probes = 0

    async def dispatch(self, command, before_write):
        if self.fail_before:
            raise ConnectionError("offline before write")
        before_write()
        if self.after_barrier is not None:
            self.after_barrier()
        self.commands.append(command)
        if self.fail_after:
            raise ConnectionError("lost response after possible dispatch")
        return self.result

    async def software_versions(self):
        self.version_probes += 1
        if self.version_error is not None:
            raise self.version_error
        return self.version_responses


def runtime(tmp_path, states=None):
    transport = FakeTransport()
    observer = FakeObserver(states or [state()])
    platform = Platform(
        TestConfig(tmp_path),
        transport=transport,
        observer=observer,
    )
    platform.start()
    return platform, transport, observer


async def settle(platform):
    tasks = list(platform.tasks)
    if tasks:
        await asyncio.gather(*tasks)


def test_second_control_owner_is_rejected(tmp_path):
    first = ControlOwner(tmp_path / "node.lock")
    try:
        with pytest.raises(BlockingIOError):
            ControlOwner(tmp_path / "node.lock")
    finally:
        first.close()
    second = ControlOwner(tmp_path / "node.lock")
    second.close()


def test_restart_recovery_never_redispatches_idempotent_operation(tmp_path):
    ledger = Ledger(tmp_path / "ops.db", TEST_NODE)
    operation, created = ledger.admit(
        "announce",
        {"kind": "time"},
        "operator",
        "same",
    )
    assert created
    ledger.start_dispatch(operation.id)
    ledger.close()

    recovered = Ledger(tmp_path / "ops.db", TEST_NODE)
    recovered.recover()
    result = recovered.get(operation.id)
    assert result.dispatch_status == "OUTCOME_UNKNOWN"
    assert result.effect_status == "UNKNOWN"
    assert result.terminal
    same = recovered.find_idempotent(
        "announce",
        {"kind": "time"},
        "operator",
        "same",
    )
    assert same.id == operation.id
    with pytest.raises(ProblemError) as exc:
        recovered.find_idempotent(
            "announce",
            {"kind": "version"},
            "operator",
            "same",
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    recovered.close()


def test_queued_restart_is_known_not_dispatched(tmp_path):
    ledger = Ledger(tmp_path / "ops.db", TEST_NODE)
    operation, _ = ledger.admit(
        "unlink_all",
        {},
        "operator",
        None,
    )
    ledger.close()
    recovered = Ledger(tmp_path / "ops.db", TEST_NODE)
    recovered.recover()
    result = recovered.get(operation.id)
    assert result.dispatch_status == "NOT_DISPATCHED"
    assert result.effect_status == "NOT_ATTEMPTED"
    recovered.close()


def test_active_policy_denies_link_and_announcement_without_dispatch(tmp_path):
    async def scenario():
        active = state(traffic="ACTIVE")
        platform, transport, _ = runtime(
            tmp_path,
            [active, active],
        )
        try:
            first = platform.admit(
                "link_node",
                {"node": "123", "mode": "transceive"},
                "operator",
            )
            await settle(platform)
            result = platform.ledger.get(first.id)
            assert result.semantic_error == "POLICY_DENIED_ACTIVE_TRAFFIC"
            assert result.dispatch_status == "NOT_DISPATCHED"
            assert transport.commands == []

            second = platform.admit(
                "announce",
                {"kind": "time"},
                "operator",
            )
            await settle(platform)
            result = platform.ledger.get(second.id)
            assert result.semantic_error == "POLICY_DENIED_ACTIVE_TRAFFIC"
            assert transport.commands == []
        finally:
            await platform.close()

    run(scenario())


def test_unknown_policy_denies_link_but_allows_exact_unlink(tmp_path):
    async def scenario():
        unknown_state = state(complete=False)
        platform, transport, _ = runtime(
            tmp_path,
            [unknown_state, unknown_state],
        )
        try:
            denied = platform.admit(
                "link_node",
                {"node": "123", "mode": "monitor"},
                "operator",
            )
            await settle(platform)
            assert (
                platform.ledger.get(denied.id).semantic_error
                == "POLICY_DENIED_STATE_UNKNOWN"
            )
            assert transport.commands == []

            allowed = platform.admit(
                "unlink_node",
                {"node": "123"},
                "operator",
            )
            await settle(platform)
            result = platform.ledger.get(allowed.id)
            assert result.dispatch_status == "ACKNOWLEDGED"
            assert result.effect_status == "UNKNOWN"
            assert transport.commands == [
                f"rpt cmd {TEST_NODE} ilink 11 123"
            ]
        finally:
            await platform.close()

    run(scenario())


def test_exact_unlink_including_permanent_uses_ilink_11(tmp_path):
    async def scenario():
        before = state(links=[link("123")])
        after = state()
        platform, transport, _ = runtime(
            tmp_path,
            [before, after],
        )
        try:
            operation = platform.admit(
                "unlink_node",
                {"node": "123"},
                "operator",
            )
            await settle(platform)
            result = platform.ledger.get(operation.id)
            assert result.dispatch_status == "ACKNOWLEDGED"
            assert result.effect_status == "OBSERVED_SATISFIED"
            assert transport.commands == [f"rpt cmd {TEST_NODE} ilink 11 123"]
        finally:
            await platform.close()

    run(scenario())


def test_failure_before_dispatch_barrier_is_known_not_dispatched(tmp_path):
    async def scenario():
        platform, transport, _ = runtime(tmp_path, [state()])
        transport.fail_before = True
        try:
            operation = platform.admit(
                "announce",
                {"kind": "time"},
                "operator",
            )
            await settle(platform)
            result = platform.ledger.get(operation.id)
            assert result.dispatch_status == "NOT_DISPATCHED"
            assert result.effect_status == "NOT_ATTEMPTED"
            assert result.terminal
            assert transport.commands == []
        finally:
            await platform.close()

    run(scenario())


def test_lost_response_after_dispatch_is_outcome_unknown_and_never_replayed(tmp_path):
    async def scenario():
        platform, transport, _ = runtime(tmp_path, [state()])
        transport.fail_after = True
        try:
            operation = platform.admit(
                "announce",
                {"kind": "time"},
                "operator",
                "one-shot",
            )
            await settle(platform)
            result = platform.ledger.get(operation.id)
            assert result.dispatch_status == "OUTCOME_UNKNOWN"
            assert result.effect_status == "UNKNOWN"
            assert result.terminal
            assert transport.commands == [f"rpt cmd {TEST_NODE} status 2"]

            retry = platform.admit(
                "announce",
                {"kind": "time"},
                "operator",
                "one-shot",
            )
            assert retry.id == operation.id
            await settle(platform)
            assert transport.commands == [f"rpt cmd {TEST_NODE} status 2"]
        finally:
            await platform.close()

    run(scenario())


def test_dispatch_barrier_persistence_failure_prevents_control_write(tmp_path, monkeypatch):
    async def scenario():
        platform, transport, _ = runtime(tmp_path, [state()])

        def fail_barrier(operation_id):
            raise __import__("sqlite3").OperationalError("simulated barrier failure")

        monkeypatch.setattr(platform.ledger, "start_dispatch", fail_barrier)
        try:
            operation = platform.admit(
                "announce",
                {"kind": "time"},
                "operator",
            )
            await settle(platform)
            result = platform.ledger.get(operation.id)
            assert result.dispatch_status == "NOT_DISPATCHED"
            assert result.effect_status == "NOT_ATTEMPTED"
            assert transport.commands == []
        finally:
            await platform.close()

    run(scenario())


def test_post_dispatch_persistence_failure_fail_closes_until_restart(tmp_path, monkeypatch):
    async def scenario():
        platform, transport, _ = runtime(tmp_path, [state()])
        original_update = platform.ledger.update

        def fail_result_update(operation_id, **changes):
            current = platform.ledger.get(operation_id)
            if current.dispatch_status == "DISPATCH_STARTED":
                raise __import__("sqlite3").OperationalError(
                    "simulated result persistence failure"
                )
            return original_update(operation_id, **changes)

        monkeypatch.setattr(platform.ledger, "update", fail_result_update)
        try:
            platform.admit(
                "announce",
                {"kind": "time"},
                "operator",
                "persist-fail",
            )
            await settle(platform)
            assert transport.commands == [f"rpt cmd {TEST_NODE} status 2"]
            assert platform.healthy is False

            with pytest.raises(ProblemError) as exc:
                platform.require_ledger()
            assert exc.value.code == "LEDGER_UNAVAILABLE"

            with pytest.raises(ProblemError) as exc:
                platform.admit(
                    "announce",
                    {"kind": "time"},
                    "operator",
                    "persist-fail",
                )
            assert exc.value.code == "LEDGER_UNAVAILABLE"
        finally:
            monkeypatch.setattr(platform.ledger, "update", original_update)
            await platform.close()

    run(scenario())


# ---------------------------------------------------------------------------
# Backend software version evidence (read-only AMI reads only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "22.5.2",
        "20.7.0",
        "22.5.2+asl3-3.6.3",
        "20.7.0-1",
        "18.9.0_rc1",
    ],
)
def test_asterisk_version_is_reported_when_core_settings_is_well_formed(value):
    assert (
        parse_asterisk_version(
            {"response": "Success", "asteriskversion": value}
        )
        == value
    )


@pytest.mark.parametrize(
    "response",
    [
        None,
        "22.5.2",
        {},
        {"response": "Error", "asteriskversion": "22.5.2"},
        {"response": "Success"},
        {"response": "Success", "asteriskversion": ""},
        {"response": "Success", "asteriskversion": " 22.5.2"},
        {"response": "Success", "asteriskversion": "22.5.2 "},
        {"response": "Success", "asteriskversion": "22 5 2"},
        {"response": "Success", "asteriskversion": "22.5.2\n"},
        {"response": "Success", "asteriskversion": "unknown"},
        {"response": "Success", "asteriskversion": ["22.5.2", "20.7.0"]},
        {"response": "Success", "asteriskversion": 22},
        {"response": "Success", "asteriskversion": "2" * 65},
    ],
)
def test_malformed_or_missing_asterisk_version_is_unknown(response):
    assert parse_asterisk_version(response) is None


@pytest.mark.parametrize(
    "output",
    ["app_rpt version: 1.2.3", "app_rpt version: 0.0.0"],
)
def test_app_rpt_version_is_reported_from_its_own_read_only_command(output):
    assert (
        parse_app_rpt_version({"response": "Success", "output": output})
        == output.rsplit(" ", 1)[1]
    )


def test_app_rpt_version_accepts_single_repeated_output_header():
    """AMI repeats the Output header; one line is still one line."""
    parsed = parse_app_rpt_version(
        {"response": "Success", "output": ["app_rpt version: 3.6.3"]}
    )
    assert parsed == "3.6.3"


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"response": "Error", "output": "app_rpt version: 1.2.3"},
        {"response": "Success"},
        {"response": "Success", "output": ""},
        {"response": "Success", "output": []},
        {"response": "Success", "output": "No such command 'rpt show version'"},
        {"response": "Success", "output": "Usage: rpt show version"},
        {"response": "Success", "output": "app_rpt version: 1.2"},
        {"response": "Success", "output": "app_rpt version: 1.2.3 (extra)"},
        {"response": "Success", "output": "version 1.2.3"},
        {
            "response": "Success",
            "output": ["app_rpt version: 1.2.3", "app_rpt version: 2.0.0"],
        },
        {
            "response": "Success",
            "output": ["app_rpt version: 1.2.3", "extra line"],
        },
    ],
)
def test_malformed_or_refused_app_rpt_version_is_unknown(response):
    assert parse_app_rpt_version(response) is None


def test_version_probe_uses_two_read_only_actions_in_one_session(
    tmp_path,
    monkeypatch,
):
    """The probe is read-only: no node argument, no dispatch barrier."""
    actions = []
    responses = [
        {"response": "Success", "asteriskversion": "22.5.2"},
        {"response": "Success", "output": "app_rpt version: 1.2.3"},
    ]

    class Session:
        async def action(self, fields, *, before_write=None):
            actions.append((dict(fields), before_write))
            return responses.pop(0)

    @contextlib.asynccontextmanager
    async def fake_session(self):
        assert isinstance(self, AMITransport)
        yield Session()

    monkeypatch.setattr(AMITransport, "session", fake_session)
    transport = AMITransport(TestConfig(tmp_path))

    asterisk, app_rpt = run(transport.software_versions())

    assert actions == [
        ({**ASTERISK_VERSION_ACTION}, None),
        ({**APP_RPT_VERSION_ACTION}, None),
    ]
    assert parse_asterisk_version(asterisk) == "22.5.2"
    assert parse_app_rpt_version(app_rpt) == "1.2.3"
    # The app_rpt read is a constant read-only CLI read: it cannot name a node.
    assert TEST_NODE not in APP_RPT_VERSION_ACTION["Command"]
    assert APP_RPT_VERSION_PATTERN.fullmatch(
        f"app_rpt version: {parse_app_rpt_version(app_rpt)}"
    )


def test_version_probe_is_bounded_by_the_ami_timeout(tmp_path, monkeypatch):
    class SlowSession:
        async def action(self, fields, *, before_write=None):
            await asyncio.sleep(30)

    @contextlib.asynccontextmanager
    async def fake_session(self):
        yield SlowSession()

    monkeypatch.setattr(AMITransport, "session", fake_session)
    transport = AMITransport(TestConfig(tmp_path))
    assert transport.timeout == 1

    with pytest.raises(asyncio.TimeoutError):
        run(transport.software_versions())


def test_software_evidence_reports_detected_versions(tmp_path):
    async def scenario():
        platform, transport, _ = runtime(tmp_path)
        try:
            evidence = await platform.software_evidence()
            assert evidence["asterisk"]["version"] == "22.5.2"
            assert evidence["asterisk"]["detected"] is True
            assert evidence["app_rpt"]["version"] == "1.2.3"
            assert evidence["app_rpt"]["detected"] is True
            assert transport.version_probes == 1
            assert transport.commands == []
        finally:
            await platform.close()

    run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("ami unavailable"),
        asyncio.TimeoutError("ami probe timed out"),
        ConnectionResetError("ami peer vanished"),
    ],
)
def test_software_evidence_is_unknown_when_the_probe_fails(tmp_path, failure):
    async def scenario():
        platform, transport, _ = runtime(tmp_path)
        transport.version_error = failure
        try:
            evidence = await platform.software_evidence()
            assert evidence["asterisk"]["version"] is None
            assert evidence["asterisk"]["detected"] is False
            assert evidence["app_rpt"]["version"] is None
            assert evidence["app_rpt"]["detected"] is False
            assert transport.commands == []
        finally:
            await platform.close()

    run(scenario())


def test_software_evidence_keeps_one_unknown_while_reporting_the_other(tmp_path):
    async def scenario():
        platform, transport, _ = runtime(tmp_path)
        transport.version_responses = (
            {"response": "Success", "asteriskversion": "22.5.2"},
            {"response": "Error", "output": "No such command"},
        )
        try:
            evidence = await platform.software_evidence()
            assert evidence["asterisk"]["detected"] is True
            assert evidence["app_rpt"]["version"] is None
            assert evidence["app_rpt"]["detected"] is False
            assert evidence["app_rpt"]["source"] == (
                "ami:Command/rpt show version"
            )
        finally:
            await platform.close()

    run(scenario())


async def _read_ami_frame(reader):
    raw = await reader.readuntil(b"\r\n\r\n")
    frame = {}
    for line in raw[:-4].decode("utf-8").split("\r\n"):
        key, _, value = line.partition(":")
        frame[key.strip()] = value.strip()
    return frame


def _ami_wire_server(requests, *, refuse_command=False):
    """Minimal AMI speaker: banner, login, CoreSettings, Command."""

    async def handler(reader, writer):
        writer.write(b"Asterisk Call Manager/14.0.0\r\n")
        await writer.drain()
        while True:
            try:
                request = await _read_ami_frame(reader)
            except (asyncio.IncompleteReadError, ConnectionError):
                writer.close()
                return
            requests.append(request)
            action = request.get("Action")
            if action == "Login":
                body = "Response: Success\r\nMessage: Authentication accepted\r\n"
            elif action == "CoreSettings":
                body = (
                    "Response: Success\r\n"
                    "AMIversion: 14.0.0\r\n"
                    "AsteriskVersion: 22.5.2+asl3-3.6.3\r\n"
                    "SystemName: asl01\r\n"
                )
            elif refuse_command:
                body = "Response: Error\r\nMessage: Permission denied\r\n"
            else:
                body = (
                    "Response: Success\r\n"
                    "Message: Command output follows\r\n"
                    "Output: app_rpt version: 1.2.3\r\n"
                )
            writer.write(
                f"{body}ActionID: {request.get('ActionID', '')}\r\n\r\n".encode()
            )
            await writer.drain()

    return handler


def test_version_probe_speaks_the_real_ami_wire_protocol(tmp_path):
    """Exercise the probe over real sockets and real AMI framing."""
    requests = []

    async def scenario():
        server = await asyncio.start_server(
            _ami_wire_server(requests),
            "127.0.0.1",
            0,
        )
        config = TestConfig(tmp_path)
        config.ami_port = server.sockets[0].getsockname()[1]
        try:
            transport = AMITransport(config)
            asterisk, app_rpt = await transport.software_versions()
        finally:
            server.close()
            await server.wait_closed()

        assert parse_asterisk_version(asterisk) == "22.5.2+asl3-3.6.3"
        assert parse_app_rpt_version(app_rpt) == "1.2.3"

        assert [request["Action"] for request in requests] == [
            "Login",
            "CoreSettings",
            "Command",
        ]
        assert requests[-1]["Command"] == "rpt show version"
        # Read-only: the probe never issued a node-targeted control command.
        assert not any(
            "rpt cmd" in request.get("Command", "") for request in requests
        )
        assert all("Node" not in request for request in requests)

    run(scenario())


def test_version_probe_reports_unknown_when_ami_refuses_the_read(tmp_path):
    """A refused CLI read degrades app_rpt to unknown, not to a version."""
    requests = []

    async def scenario():
        server = await asyncio.start_server(
            _ami_wire_server(requests, refuse_command=True),
            "127.0.0.1",
            0,
        )
        config = TestConfig(tmp_path)
        config.ami_port = server.sockets[0].getsockname()[1]
        try:
            transport = AMITransport(config)
            asterisk, app_rpt = await transport.software_versions()
        finally:
            server.close()
            await server.wait_closed()

        assert parse_asterisk_version(asterisk) == "22.5.2+asl3-3.6.3"
        assert app_rpt["response"] == "Error"
        assert parse_app_rpt_version(app_rpt) is None

    run(scenario())
