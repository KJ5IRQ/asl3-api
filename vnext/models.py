"""Public v1 types. Unknown evidence is never represented as false."""
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NODE_PATTERN = r"^[1-9][0-9]{0,5}$"
Node = Annotated[str, StringConstraints(strict=True, pattern=NODE_PATTERN)]


def validate_node(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(NODE_PATTERN, value, flags=re.ASCII) is None:
        raise ValueError("Node must be 1-6 ASCII digits with no leading zero")
    return value


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: Node
    mode: Literal["transceive", "monitor"] = "transceive"


class AnnouncementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["identify", "time", "status", "version"]


class DirectLink(BaseModel):
    """One real adjacent app_rpt connection.

    `allstar_node` is populated only when the identifier is a canonical v1
    AllStar target. Foreign/direct-client links remain observable but cannot be
    smuggled into the v1 control target namespace.
    """

    node: str = Field(min_length=1, max_length=64)
    allstar_node: Node | None = None
    mode: Literal["T", "R", "L", "C", "UNKNOWN"]
    keyed: bool
    connection_status: Literal["ESTABLISHED", "CONNECTING"]


class NodeState(BaseModel):
    node: Node
    observed_at: str
    connection_epoch: str | None = None
    state_status: Literal["COMPLETE", "STATE_UNKNOWN"]
    traffic_state: Literal["ACTIVE", "CLEAR", "UNKNOWN"]
    complete: bool
    rx_keyed: bool | None = None
    tx_keyed: bool | None = None
    direct_links: list[DirectLink] | None = None
    reasons: list[str] = Field(default_factory=list)
    source: str = "app_rpt/RptStatus/XStat+SawStat"


class Operation(BaseModel):
    id: str
    node: Node
    kind: Literal["link_node", "unlink_node", "unlink_all", "announce"]
    request: dict
    credential: str
    created_at: str
    updated_at: str
    dispatch_status: Literal[
        "QUEUED",
        "DISPATCH_STARTED",
        "ACKNOWLEDGED",
        "REJECTED",
        "NOT_DISPATCHED",
        "OUTCOME_UNKNOWN",
    ]
    effect_status: Literal[
        "PENDING",
        "NOT_ATTEMPTED",
        "OBSERVED_SATISFIED",
        "OBSERVED_PARTIAL",
        "OBSERVED_UNSATISFIED",
        "NOT_APPLICABLE",
        "UNKNOWN",
    ]
    terminal: bool
    semantic_error: str | None = None
    evidence: NodeState | None = None


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
    instance: str


class ProblemError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)


def command_for(node: str, kind: str, request: dict) -> str:
    """Map the small semantic v1 surface to verified app_rpt commands."""
    validate_node(node)
    if kind == "link_node":
        body = LinkRequest(**request)
        command = f"ilink {2 if body.mode == 'monitor' else 3} {body.node}"
    elif kind == "unlink_node":
        # ilink 11 removes the exact target even when it is a permanent link.
        command = f"ilink 11 {validate_node(request['node'])}"
    elif kind == "unlink_all" and not request:
        command = "ilink 6"
    elif kind == "announce":
        body = AnnouncementRequest(**request)
        command = {
            "identify": "status 1",
            "time": "status 2",
            "status": "ilink 5",
            "version": "status 3",
        }[body.kind]
    else:
        raise ValueError("Unsupported operation")
    return f"rpt cmd {node} {command}"
