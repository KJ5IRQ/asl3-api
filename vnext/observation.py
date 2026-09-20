"""Strict native app_rpt observations.

XStat supplies direct connection identity/status and channel variables.
SawStat supplies per-adjacent-link keyed state. RPT_ALINKS supplies mode/keyed
evidence for links app_rpt includes in that variable. Any contradictory sample
becomes STATE_UNKNOWN rather than being coerced to false.
"""
import asyncio
import re
from collections.abc import Mapping
from uuid import uuid4

from .models import NodeState, now, validate_node
from .transport import ProtocolError

SAFE_LINK_NAME = re.compile(r"^[^\s,\x00-\x1f\x7f]{1,64}$", re.ASCII)
XSTAT_CONN = re.compile(
    r"^(\S+)\s+(\S+)\s+(\d+)\s+(IN|OUT)\s+"
    r"(\d{2,}:[0-5]\d:[0-5]\d)\s+(ESTABLISHED|CONNECTING)\s*$",
    re.ASCII,
)
SAWSTAT_CONN = re.compile(r"^(\S+)\s+([01])\s+(-?\d+)\s+(-?\d+)\s*$", re.ASCII)


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or SAFE_LINK_NAME.fullmatch(name) is None:
        raise ValueError("Malformed adjacent-link identifier")
    if name == "000000":
        raise ValueError("Truncation sentinel in adjacent-link evidence")
    return name


def _values(message: Mapping, key: str) -> list[str]:
    value = message.get(key)
    if value is None:
        return []
    result = value if isinstance(value, list) else [value]
    if not all(isinstance(item, str) for item in result):
        raise ValueError(f"Malformed {key} field")
    return result


def _scalar(message: Mapping, key: str) -> str:
    values = _values(message, key)
    if len(values) != 1:
        raise ValueError(f"Missing or repeated {key} field")
    return values[0]


def parse_alinks(value: str, count: str) -> dict[str, dict]:
    """Parse RPT_ALINKS without assuming every link is an AllStar node number."""
    if not isinstance(value, str) or not isinstance(count, str):
        raise TypeError("Missing ALINKS evidence")
    if re.fullmatch(r"0|[1-9][0-9]*", count, flags=re.ASCII) is None:
        raise ValueError("Malformed ALINKS count")
    expected = int(count)
    if expected == 0:
        if value != "":
            raise ValueError("ALINKS count/value mismatch")
        return {}

    tokens = value.split(",")
    if tokens[0] != count or len(tokens) != expected + 1:
        raise ValueError("Truncated or inconsistent ALINKS")

    result: dict[str, dict] = {}
    for token in tokens[1:]:
        if len(token) < 3:
            raise ValueError("Malformed ALINKS entry")
        name, mode, keyed = token[:-2], token[-2], token[-1]
        _safe_name(name)
        if mode not in {"T", "R", "L", "C"} or keyed not in {"K", "U"}:
            raise ValueError("Malformed ALINKS mode/key state")
        if name in result:
            raise ValueError("Duplicate ALINKS entry")
        result[name] = {"mode": mode, "keyed": keyed == "K"}
    return result


def _parse_xstat(response: Mapping, node: str) -> tuple[dict, dict]:
    fields = {str(k).lower(): v for k, v in response.items()}
    if _scalar(fields, "response") != "Success" or _scalar(fields, "node") != node:
        raise ValueError("XStat failed or node mismatch")

    variables = {}
    for row in _values(fields, "var"):
        key, separator, value = row.partition("=")
        if not separator or not key or key in variables:
            raise ValueError("Malformed or duplicate XStat variable")
        variables[key] = value

    for key in ("RPT_RXKEYED", "RPT_TXKEYED"):
        if variables.get(key) not in {"0", "1"}:
            raise ValueError(f"Missing or malformed {key}")

    # app_rpt emits 000000 when link-list evidence is truncated.
    if "000000" in str(variables.get("RPT_LINKS", "")):
        raise ValueError("Truncated RPT_LINKS")

    connections: dict[str, str] = {}
    for row in _values(fields, "conn"):
        match = XSTAT_CONN.fullmatch(row)
        if match is None:
            raise ValueError("Malformed XStat Conn")
        name = _safe_name(match.group(1))
        if name in connections:
            raise ValueError("Duplicate XStat Conn")
        connections[name] = match.group(6)

    alinks = parse_alinks(
        variables.get("RPT_ALINKS"),
        variables.get("RPT_NUMALINKS"),
    )
    if not set(alinks).issubset(connections):
        raise ValueError("ALINKS names are not a subset of XStat direct connections")
    return variables, {"connections": connections, "alinks": alinks}


def _parse_sawstat(response: Mapping, node: str) -> dict[str, bool]:
    fields = {str(k).lower(): v for k, v in response.items()}
    if _scalar(fields, "response") != "Success" or _scalar(fields, "node") != node:
        raise ValueError("SawStat failed or node mismatch")

    keyed: dict[str, bool] = {}
    for row in _values(fields, "conn"):
        match = SAWSTAT_CONN.fullmatch(row)
        if match is None:
            raise ValueError("Malformed SawStat Conn")
        name = _safe_name(match.group(1))
        if name in keyed:
            raise ValueError("Duplicate SawStat Conn")
        keyed[name] = match.group(2) == "1"
    return keyed


def unknown(node: str, epoch: str | None, reason: str) -> NodeState:
    return NodeState(
        node=node,
        observed_at=now(),
        connection_epoch=epoch,
        state_status="STATE_UNKNOWN",
        traffic_state="UNKNOWN",
        complete=False,
        reasons=[reason],
    )


def parse_snapshot(xstat, sawstat, node: str, epoch: str) -> NodeState:
    """Combine two same-session native observations conservatively."""
    try:
        if not isinstance(xstat, Mapping) or not isinstance(sawstat, Mapping):
            raise TypeError("Malformed RptStatus response")
        variables, xdata = _parse_xstat(xstat, node)
        saw_keyed = _parse_sawstat(sawstat, node)
        connections = xdata["connections"]
        alinks = xdata["alinks"]

        # XStat and SawStat are separate actions. A set mismatch means topology
        # changed between them, so this sample cannot support a control decision.
        if set(connections) != set(saw_keyed):
            raise ValueError("Direct-link set changed during observation")

        links = []
        for name, status in connections.items():
            alink = alinks.get(name)
            keyed = saw_keyed[name]
            if alink is not None and alink["keyed"] != keyed:
                raise ValueError("Keyed state changed during observation")
            try:
                allstar = validate_node(name)
            except ValueError:
                allstar = None
            links.append(
                {
                    "node": name,
                    "allstar_node": allstar,
                    "mode": alink["mode"] if alink is not None else "UNKNOWN",
                    "keyed": keyed,
                    "connection_status": status,
                }
            )

        rx_keyed = variables["RPT_RXKEYED"] == "1"
        tx_keyed = variables["RPT_TXKEYED"] == "1"
        active = rx_keyed or tx_keyed or any(link["keyed"] for link in links)
        return NodeState(
            node=node,
            observed_at=now(),
            connection_epoch=epoch,
            state_status="COMPLETE",
            traffic_state="ACTIVE" if active else "CLEAR",
            complete=True,
            rx_keyed=rx_keyed,
            tx_keyed=tx_keyed,
            direct_links=links,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return unknown(node, epoch, str(exc))


class Observer:
    def __init__(self, node, transport, timeout=5):
        self.node = node
        self.transport = transport
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.generation = 0

    def invalidate(self):
        """Invalidate an in-flight sample after lifecycle/connection changes."""
        self.generation += 1

    async def snapshot(self) -> NodeState:
        async with self.lock:
            generation = self.generation
            try:
                xstat, sawstat, epoch = await asyncio.wait_for(
                    self.transport.observe(self.node),
                    self.timeout,
                )
                if generation != self.generation:
                    return unknown(self.node, epoch, "STALE_CONNECTION_EPOCH")
                return parse_snapshot(xstat, sawstat, self.node, epoch)
            except (
                OSError,
                asyncio.TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ProtocolError,
                ValueError,
            ):
                return unknown(self.node, uuid4().hex, "AMI_OBSERVATION_UNAVAILABLE")
