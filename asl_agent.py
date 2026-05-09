"""ASL3-API - REST API for AllStar Link node control."""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Request, status
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from ami_client import ami_client
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
# Event handler
# ---------------------------------------------------------------------------

event_handler = EventHandler(ami_client)
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
    description="REST API for AllStar Link node monitoring and control.",
    version="1.2.0",
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
# Routes
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
    }


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
    """
    try:
        nodes = await ami_client.get_connected_nodes()
        if enrich:
            node_cache.enrich_node_list(nodes)
        audit_log("nodes", f"{len(nodes)} connected")
        return {"connected_nodes": nodes, "count": len(nodes)}
    except Exception as e:
        logger.error(f"/nodes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/connect", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def connect_node(request: Request, body: ConnectRequest):
    """
    Connect to a remote AllStar node.

    Set monitor_only=true for receive-only (RX) mode.
    Connection verification takes approximately 8 seconds.
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

    Disconnection verification takes approximately 5 seconds.
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


@app.post(
    "/disconnect-all", dependencies=[Depends(verify_api_key)], tags=["Control"]
)
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


@app.post("/dtmf", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def send_dtmf(request: Request, body: DTMFRequest):
    """
    Send a DTMF sequence to the node.

    The confirmed field must be set to true. This requirement prevents
    accidental DTMF sends from misconfigured clients.

    Valid characters: 0-9, *, #
    """
    if not body.confirmed:
        raise HTTPException(
            status_code=400,
            detail="confirmed must be true to send DTMF",
        )
    try:
        result = await ami_client.send_dtmf(body.sequence)
        audit_log("dtmf", f"sequence={body.sequence}")
        return {
            "success": True,
            "message": f"DTMF sequence '{body.sequence}' sent",
            "sequence": body.sequence,
        }
    except Exception as e:
        logger.error(f"/dtmf error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/macro", dependencies=[Depends(verify_api_key)], tags=["Control"])
@limiter.limit(lambda: f"{config.rate_limit}/minute")
async def execute_macro(request: Request, body: MacroRequest):
    """
    Execute a macro defined in rpt.conf.

    Macros must be defined in your node's rpt.conf before use.
    See the ASL3 documentation for macro configuration.
    """
    try:
        result = await ami_client.execute_macro(body.macro_number)
        audit_log("macro", f"macro_number={body.macro_number}")
        return {
            "success": True,
            "message": f"Macro {body.macro_number} executed",
            "macro_number": body.macro_number,
        }
    except Exception as e:
        logger.error(f"/macro error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/lookup/{node_number}", dependencies=[Depends(verify_api_key)], tags=["Node"])
async def lookup_node(node_number: str):
    """
    Look up a node's callsign, location, and description from the AllStar
    node database. Served from the local cache — no external HTTP call.

    The cache is refreshed every 15 minutes from allmondb.allstarlink.org.
    """
    if not re.fullmatch(r"\d+", node_number):
        raise HTTPException(status_code=400, detail="Node number must contain only digits")
    return node_cache.lookup(node_number)


@app.get("/audit", dependencies=[Depends(verify_api_key)], tags=["Admin"])
async def get_audit_log(lines: int = 50):
    """Return the most recent audit log entries (default: 50)."""
    try:
        with open(config.audit_file, "r") as f:
            all_lines = f.readlines()
        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {
            "entries": [line.strip() for line in recent],
            "count": len(recent),
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
