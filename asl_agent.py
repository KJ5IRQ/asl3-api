"""ASL3-API - REST API for AllStar Link node control."""
import asyncio
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from ami_client import CommandRejected, ami_client
from ami_event_listener import AMIEventListener
from config import config
from event_handler import EventHandler
from node_cache import node_cache

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.log_level),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global event infrastructure
# ---------------------------------------------------------------------------

event_handler = EventHandler(ami_client)
ami_event_listener = AMIEventListener(ami_client)
_monitoring_task: Optional[asyncio.Task] = None

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect to AMI on startup; disconnect cleanly on shutdown."""
    global _monitoring_task

    logger.info("ASL3-API starting...")
    try:
        config.validate()
    except ValueError as e:
        logger.critical(str(e))
        raise

    await ami_client.connect()
    await event_handler.start()
    await node_cache.start()
    await ami_event_listener.start()

    if config.webhooks_enabled:
        _monitoring_task = asyncio.create_task(event_handler.monitoring_loop())
        logger.info("Webhook monitoring loop started")

    logger.info(
        f"ASL3-API ready — node {config.node_number} ({config.node_callsign})"
        f" on {config.api_host}:{config.api_port}"
    )

    yield

    logger.info("ASL3-API shutting down...")
    if _monitoring_task:
        _monitoring_task.cancel()
        try:
            await _monitoring_task
        except asyncio.CancelledError:
            pass

    await ami_event_listener.stop()
    await event_handler.stop()
    await node_cache.stop()
    await ami_client.disconnect()
    logger.info("ASL3-API stopped")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ASL3-API",
    description="REST API for AllStar Link node monitoring, control, and live event streaming.",
    version="1.4.2",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


async def verify_api_key(x_api_key: str = Header(...)):
    """Validate the X-API-Key header on every protected endpoint."""
    if not config.api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key not configured on server",
        )
    if x_api_key != config.api_key:
        logger.warning("Rejected request with invalid API key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return x_api_key


async def verify_api_key_query(api_key: str = Query(..., alias="api_key")):
    """
    Query-parameter API key validation for SSE endpoints.

    The browser EventSource API does not support custom headers, so the
    /events endpoint accepts ?api_key= as an alternative to X-API-Key.
    Both are validated against the same configured secret.
    """
    if not config.api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key not configured on server",
        )
    if api_key != config.api_key:
        logger.warning("Rejected SSE request with invalid api_key query param")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return api_key


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


def audit_log(command: str, details: str = ""):
    """Append a timestamped entry to the audit log file."""
    entry = (
        f"{datetime.now(timezone.utc).isoformat()} | {command} | {details}\n"
    )
    try:
        with open(config.audit_file, "a") as f:
            f.write(entry)
    except Exception as e:
        logger.error(f"Audit log write failed: {e}")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ConnectRequest(BaseModel):
    node: str = Field(..., description="Node number to connect to")
    monitor_only: bool = Field(
        False, description="If true, connect in monitor-only (RX) mode"
    )

    @field_validator("node")
    @classmethod
    def node_must_be_numeric(cls, v: str) -> str:
        if not re.fullmatch(r"\d+", v):
            raise ValueError("Node number must contain only digits")
        return v


class DisconnectRequest(BaseModel):
    node: str = Field(..., description="Node number to disconnect")

    @field_validator("node")
    @classmethod
    def node_must_be_numeric(cls, v: str) -> str:
        if not re.fullmatch(r"\d+", v):
            raise ValueError("Node number must contain only digits")
        return v


class DTMFRequest(BaseModel):
    sequence: str = Field(..., description="DTMF sequence to send (0-9, *, #)")
    confirmed: bool = Field(
        False,
        description="Must be true to execute — prevents accidental DTMF sends",
    )

    @field_validator("sequence")
    @classmethod
    def sequence_must_be_valid_dtmf(cls, v: str) -> str:
        if not v:
            raise ValueError("DTMF sequence cannot be empty")
        if not re.fullmatch(r"[0-9*#]+", v):
            raise ValueError("DTMF sequence may only contain digits, *, and #")
        return v


class MacroRequest(BaseModel):
    macro_number: str = Field(
        ..., description="Macro number as defined in rpt.conf"
    )

    @field_validator("macro_number")
    @classmethod
    def macro_must_be_numeric(cls, v: str) -> str:
        if not re.fullmatch(r"\d+", v):
            raise ValueError("Macro number must contain only digits")
        return v


# ---------------------------------------------------------------------------
# Routes — Health
# ---------------------------------------------------------------------------


@app.get("/ping", tags=["Health"])
async def ping():
    """
    Lightweight health check. No authentication required.

    Actively verifies the AMI connection is alive on every call.
    Use this to confirm the API is reachable and Asterisk is responding
    before polling /status or /nodes.
    """
    ami_ok = await ami_client.check_ami_health()
    return {
        "service": "ASL3-API",
        "node": config.node_number,
        "callsign": config.node_callsign,
        "ami_connected": ami_ok,
        "sse_clients": ami_event_listener.subscriber_count,
    }


@app.get("/version", tags=["Health"])
async def version():
    """
    Return version information. No authentication required.

    Useful for verifying which version is deployed, especially when
    running multiple nodes with different ASL3-API versions.
    """
    return {
        "version": app.version,
        "python": sys.version.split()[0],
        "node": config.node_number,
        "callsign": config.node_callsign,
        "node_cache_size": node_cache.size,
        "node_cache_last_updated": node_cache.last_updated,
        "sse_clients": ami_event_listener.subscriber_count,
        "events_enabled": config.events_enabled,
    }


# ---------------------------------------------------------------------------
# Routes — Node
# ---------------------------------------------------------------------------


@app.get("/status", dependencies=[Depends(verify_api_key)], tags=["Node"])
async def get_status(raw: bool = False):
    """Return node statistics: uptime, keyup count, TX time, DTMF stats.

    Add ?raw=true to include the unparsed rpt stats output for debugging.
    """
    try:
        stats = await ami_client.get_node_stats(include_raw=raw)
        audit_log("status")
        return stats
    except Exception as e:
        logger.error(f"/status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/nodes", dependencies=[Depends(verify_api_key)], tags=["Node"])
async def get_nodes(enrich: bool = False):
    """
    Return the list of nodes currently connected to this node.

    Add ?enrich=true to include callsign, location, and description
    for each connected node from the AllStar node database.

    All response fields are always present. Fields without data are null,
    never absent.
    """
    try:
        nodes = await ami_client.get_connected_nodes()
        if enrich:
            node_cache.enrich_node_list(nodes)
        else:
            # Guarantee consistent schema even without enrichment
            for n in nodes:
                n.setdefault("callsign", None)
                n.setdefault("description", None)
                n.setdefault("location", None)
        audit_log("nodes", f"{len(nodes)} connected")
        return {"connected_nodes": nodes, "count": len(nodes)}
    except Exception as e:
        logger.error(f"/nodes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/variables", dependencies=[Depends(verify_api_key)], tags=["Node"])
async def get_variables():
    """
    Return live app_rpt node variables.

    Includes keyed state (rxkeyed), transmitter state (txkeyed),
    link counts, autopatch state, and more. Sourced directly from
    Asterisk via AMI — no caching.

    All fields are always present in the response. Fields that could
    not be read from AMI are null, never absent.

    Key fields:
      rxkeyed           bool or null  - RF receiver is keyed (signal present on input)
      txkeyed           bool or null  - Transmitter is currently active
      ext_txkeyed       bool or null  - External TX keyed
      num_links         int  or null  - Number of connected links
      links             str  or null  - Raw link list string from app_rpt
      num_active_links  int  or null  - Number of adjacent active links
      active_links      str  or null  - Raw adjacent link list with mode/keyed state
      autopatch_up      bool or null  - Autopatch is currently active
    """
    try:
        variables = await ami_client.get_node_variables()
        return variables
    except Exception as e:
        logger.error(f"/variables error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/capabilities", dependencies=[Depends(verify_api_key)], tags=["Node"])
async def get_capabilities():
    """
    Return the static capabilities of this node and API instance.

    Provides machine-readable metadata about what this API supports,
    what the configured node is, and what optional features are active.
    Intended for MCP tool descriptions and client auto-configuration.

    This endpoint does not query AMI — it reads config only and is safe
    to call frequently.
    """
    return {
        "node": config.node_number,
        "callsign": config.node_callsign,
        "api_version": app.version,
        "features": {
            "sse_events": config.events_enabled,
            "webhooks": config.webhooks_enabled,
            "node_cache": True,
            "node_enrichment": True,
            "dtmf": False,
            "macros": False,
            "announcements": ["identify", "time", "status", "version"],
        },
        "unavailable": {
            "dtmf": (
                "Disabled since 1.4.2 pending safety redesign. The prior "
                "implementation issued an invalid app_rpt command and never "
                "executed. POST /dtmf returns 503."
            ),
            "macros": (
                "Disabled since 1.4.2 pending safety redesign. The prior "
                "implementation issued a command app_rpt ignored and never "
                "executed. POST /macro returns 503."
            ),
        },
        "endpoints": {
            "events_stream": "/events?api_key=YOUR_KEY" if config.events_enabled else None,
            "rest_docs": "/docs",
            "redoc": "/redoc",
        },
        "event_types": [
            "node.rxkeyed",
            "node.txkeyed",
            "node.variables.snapshot",
            "link.connected",
            "link.disconnected",
            "health.ami",
        ] if config.events_enabled else [],
        "notes": {
            "connect_timeout_seconds": config.connect_timeout,
            "disconnect_timeout_seconds": config.disconnect_timeout,
            "node_cache_refresh_seconds": 900,
            "rate_limit_per_minute": config.rate_limit,
        },
    }


@app.get("/lookup/{node_number}", dependencies=[Depends(verify_api_key)], tags=["Node"])
async def lookup_node(node_number: str):
    """
    Look up a node's callsign, location, and description from the AllStar
    node database. Served from the local cache — no external HTTP call.

    All fields are always present. Fields not in the database are null.

    The cache is refreshed every 15 minutes from allmondb.allstarlink.org.
    """
    if not re.fullmatch(r"\d+", node_number):
        raise HTTPException(status_code=400, detail="Node number must contain only digits")
    return node_cache.lookup(node_number)


# ---------------------------------------------------------------------------
# Routes — SSE Event Stream
# ---------------------------------------------------------------------------


@app.get("/events", tags=["Events"])
async def event_stream(
    request: Request,
    api_key: str = Depends(verify_api_key_query),
):
    """
    Server-Sent Events stream of live node state.

    Connect with EventSource in a browser:
        const es = new EventSource('/events?api_key=YOUR_KEY');
        es.addEventListener('node.rxkeyed', e => console.log(JSON.parse(e.data)));

    Or with curl:
        curl -N 'http://node:8073/events?api_key=YOUR_KEY'

    Events emitted (all include timestamp, node, callsign fields):

      node.rxkeyed
        rxkeyed: bool  — RF receiver keyed state changed
        node_number: str

      node.txkeyed
        txkeyed: bool  — Transmitter keyed state changed
        node_number: str

      node.variables.snapshot
        variables: object  — Full variable state snapshot (every 10s)

      link.connected
        connected_node: str  — Remote node just connected
        mode: str            — T=transceive, R=receive-only

      link.disconnected
        disconnected_node: str  — Remote node just disconnected

      health.ami
        connected: bool  — AMI connection state changed

    Keepalive comments are sent every 15 seconds to prevent proxy/browser
    timeout on idle connections.

    NOTE: If you are running nginx in front of this API, add
    proxy_set_header X-Accel-Buffering no; to your location block,
    or events will be buffered and not delivered in real time.
    """
    if not config.events_enabled:
        raise HTTPException(
            status_code=503,
            detail="SSE events are disabled. Set events.enabled: true in config.yaml.",
        )

    queue = ami_event_listener.subscribe()
    audit_log("events/connect", f"clients={ami_event_listener.subscriber_count}")

    async def generator() -> AsyncIterator[str]:
        # Send immediate variable snapshot on connect so client has initial state
        try:
            variables = await ami_client.get_node_variables()
            snapshot = {
                "type": "node.variables.snapshot",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "node": config.node_number,
                "callsign": config.node_callsign,
                "variables": variables,
            }
            yield f"event: node.variables.snapshot\ndata: {json.dumps(snapshot)}\n\n"
        except Exception as e:
            logger.warning(f"Initial snapshot failed: {e}")

        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    event_type = event.get("type", "message")
                    yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive -- prevents nginx/browser from closing idle connections
                    yield ": keepalive\n\n"
        finally:
            ami_event_listener.unsubscribe(queue)
            audit_log("events/disconnect", f"clients={ami_event_listener.subscriber_count}")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Routes — Control
# ---------------------------------------------------------------------------


@app.post("/connect", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def connect_node(request: Request, body: ConnectRequest):
    """
    Connect to a remote AllStar node.

    Set monitor_only=true for receive-only (RX) mode.
    Connection verification polls every second up to connect_timeout seconds.
    """
    try:
        mode = "monitor" if body.monitor_only else "transceive"
        result = await ami_client.connect_node(body.node, body.monitor_only)
        audit_log("connect", f"node={body.node} mode={mode}")

        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error"))

        return {
            "success": True,
            "message": f"Connected to node {body.node} in {mode} mode",
            "node": body.node,
            "mode": mode,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/connect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/disconnect", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def disconnect_node(request: Request, body: DisconnectRequest):
    """
    Disconnect from a specific remote node.

    Disconnection verification polls every second up to disconnect_timeout seconds.
    """
    try:
        result = await ami_client.disconnect_node(body.node)
        audit_log("disconnect", f"node={body.node}")

        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error"))

        return {
            "success": True,
            "message": f"Disconnected from node {body.node}",
            "node": body.node,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/disconnect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/disconnect-all", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def disconnect_all(request: Request):
    """Drop all active node connections."""
    try:
        await ami_client.disconnect_all()
        audit_log("disconnect-all")
        return {"success": True, "message": "All node connections dropped"}
    except Exception as e:
        logger.error(f"/disconnect-all error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _capability_disabled(capability: str, detail: str, attempt: str) -> HTTPException:
    """
    Build the 503 returned by a capability withdrawn pending safety redesign.

    Records the refused attempt in the audit log. The audit entry is explicitly
    marked as rejected so the log never implies the node acted on the request.
    """
    audit_log(f"{capability}/rejected", f"{attempt} reason=capability_disabled")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "capability_disabled",
            "capability": capability,
            "message": detail,
            "executed": False,
            "since_version": "1.4.2",
        },
    )


@app.post("/dtmf", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def send_dtmf(request: Request, body: DTMFRequest):
    """
    Disabled since 1.4.2. Always returns 503 and sends nothing to the node.

    This endpoint never worked. It issued 'rpt cmd <node> senddigits <seq>',
    and 'senddigits' is not an app_rpt function — app_rpt answered "Unknown
    action name senddigits." while this API reported success and wrote an
    audit entry claiming the digits were sent.

    It is not being repaired in place. The working equivalent, 'rpt fun',
    injects digits into the node's own DTMF function decoder, so its effect is
    whatever that node's rpt.conf [functions] stanza defines — potentially
    including link control and control-operator state changes. That needs the
    policy gating designed in the architecture review before it is exposed.
    """
    raise _capability_disabled(
        "dtmf",
        "DTMF sending is disabled pending a safety redesign. The previous "
        "implementation issued an invalid app_rpt command and never executed, "
        "despite reporting success. No digits were sent.",
        f"sequence={body.sequence}",
    )


@app.post("/macro", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def execute_macro(request: Request, body: MacroRequest):
    """
    Disabled since 1.4.2. Always returns 503 and sends nothing to the node.

    This endpoint never worked. It issued 'rpt cmd <node> cop 6 <macro>'.
    COP 6 is "Simulate COR being activated (phone only)" and returns
    DC_INDETERMINATE for any command source other than SOURCE_PHONE; 'rpt cmd'
    always uses SOURCE_RPT. No macro was ever executed, yet this API reported
    success and wrote an audit entry claiming it had run.

    Macro execution is the separate 'macro' function class. A macro expands to
    an arbitrary DTMF string from the node's rpt.conf [macro] stanza, so its
    effect is unbounded from this API's perspective. It stays disabled until
    policy gating exists.
    """
    raise _capability_disabled(
        "macros",
        "Macro execution is disabled pending a safety redesign. The previous "
        "implementation issued a command that app_rpt ignored, despite "
        "reporting success. No macro was executed.",
        f"macro_number={body.macro_number}",
    )


# ---------------------------------------------------------------------------
# Routes — Announcements
#
# HTTP paths keep their /cop/ prefix for client compatibility, but none of
# these are COP (control operator) commands. Through 1.4.1 they issued COP
# 10/12/13/14, which are autopatch disable, link disable, query system control
# state and change system control state — not announcements. /cop/identify
# silently disabled autopatch and /cop/time silently disabled link functions.
# They now issue the app_rpt commands that match what the endpoints document.
# ---------------------------------------------------------------------------


@app.post("/cop/identify", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def cop_identify(request: Request):
    """
    Force the node to identify over the air. app_rpt: status 1.

    Fixed in 1.4.2. Previously issued COP 10, which disables autopatch.
    """
    try:
        result = await ami_client.force_id()
        audit_log("cop/identify", f"command={result['command']}")
        return {"success": True, "message": "Node ID playback triggered"}
    except CommandRejected as e:
        logger.error(f"/cop/identify rejected: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"/cop/identify error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/cop/time", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def cop_time(request: Request):
    """
    Announce the current time over the air. app_rpt: status 2.

    Fixed in 1.4.2. Previously issued COP 12, which disables link functions.
    """
    try:
        result = await ami_client.say_time()
        audit_log("cop/time", f"command={result['command']}")
        return {"success": True, "message": "Time announcement triggered"}
    except CommandRejected as e:
        logger.error(f"/cop/time rejected: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"/cop/time error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/cop/status", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def cop_status(request: Request):
    """
    Announce system/connection status over the air. app_rpt: ilink 5.

    Fixed in 1.4.2. Previously issued COP 13, which announces the system
    control state rather than connection status.
    """
    try:
        result = await ami_client.say_status()
        audit_log("cop/status", f"command={result['command']}")
        return {"success": True, "message": "System status announcement triggered"}
    except CommandRejected as e:
        logger.error(f"/cop/status rejected: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"/cop/status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/cop/version", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def cop_version(request: Request):
    """
    Announce the app_rpt software version over the air. app_rpt: status 3.

    Fixed in 1.4.2. Previously issued COP 14 with no argument, which changes
    the system control state when given one and did nothing without one.
    """
    try:
        result = await ami_client.say_version()
        audit_log("cop/version", f"command={result['command']}")
        return {"success": True, "message": "Version announcement triggered"}
    except CommandRejected as e:
        logger.error(f"/cop/version rejected: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"/cop/version error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Routes — Admin
# ---------------------------------------------------------------------------


@app.get("/audit", dependencies=[Depends(verify_api_key)], tags=["Admin"])
async def get_audit_log(lines: int = 50):
    """
    Return the most recent audit log entries (default: 50).

    Each entry is a structured dict with timestamp, command, and details
    fields parsed from the log file. Suitable for machine consumption.
    """
    try:
        with open(config.audit_file, "r") as f:
            all_lines = f.readlines()
        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines

        entries = []
        for line in recent:
            line = line.strip()
            if not line:
                continue
            parts = line.split(" | ", 2)
            entries.append({
                "timestamp": parts[0] if len(parts) > 0 else None,
                "command":   parts[1] if len(parts) > 1 else None,
                "details":   parts[2] if len(parts) > 2 else None,
                "raw":       line,
            })

        return {
            "entries": entries,
            "count": len(entries),
        }
    except FileNotFoundError:
        return {"entries": [], "count": 0}
    except Exception as e:
        logger.error(f"/audit error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Entry point (direct execution only — use systemd/uvicorn in production)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=config.api_host,
        port=config.api_port,
        log_level=config.log_level.lower(),
    )
