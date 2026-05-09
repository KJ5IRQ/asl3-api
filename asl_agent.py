"""ASL3-API - REST API for AllStar Link node control."""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from ami_client import ami_client
from config import config
from event_handler import EventHandler

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
    await ami_client.connect()
    await event_handler.start()

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
    await ami_client.disconnect()
    logger.info("ASL3-API stopped")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ASL3-API",
    description="REST API for AllStar Link node monitoring and control.",
    version="1.0.0",
    lifespan=lifespan,
)

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

    Returns service identity and AMI connection state. Use this to verify
    the API is reachable before polling /status or /nodes.
    """
    return {
        "service": "ASL3-API",
        "node": config.node_number,
        "callsign": config.node_callsign,
        "ami_connected": ami_client.connected,
    }


@app.get("/status", dependencies=[Depends(verify_api_key)], tags=["Node"])
async def get_status():
    """Return node statistics: uptime, keyup count, and connected node summary."""
    try:
        stats = await ami_client.get_node_stats()
        audit_log("status")
        return stats
    except Exception as e:
        logger.error(f"/status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/nodes", dependencies=[Depends(verify_api_key)], tags=["Node"])
async def get_nodes():
    """Return the list of nodes currently connected to this node."""
    try:
        nodes = await ami_client.get_connected_nodes()
        audit_log("nodes", f"{len(nodes)} connected")
        return {"connected_nodes": nodes, "count": len(nodes)}
    except Exception as e:
        logger.error(f"/nodes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/connect", dependencies=[Depends(verify_api_key)], tags=["Control"])
async def connect_node(request: ConnectRequest):
    """
    Connect to a remote AllStar node.

    Set monitor_only=true for receive-only (RX) mode.
    Connection verification takes approximately 8 seconds.
    """
    try:
        mode = "monitor" if request.monitor_only else "transceive"
        result = await ami_client.connect_node(request.node, request.monitor_only)
        audit_log("connect", f"node={request.node} mode={mode}")

        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error"))

        return {
            "success": True,
            "message": f"Connected to node {request.node} in {mode} mode",
            "node": request.node,
            "mode": mode,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/connect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/disconnect", dependencies=[Depends(verify_api_key)], tags=["Control"])
async def disconnect_node(request: DisconnectRequest):
    """
    Disconnect from a specific remote node.

    Disconnection verification takes approximately 5 seconds.
    """
    try:
        result = await ami_client.disconnect_node(request.node)
        audit_log("disconnect", f"node={request.node}")

        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error"))

        return {
            "success": True,
            "message": f"Disconnected from node {request.node}",
            "node": request.node,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/disconnect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/disconnect-all", dependencies=[Depends(verify_api_key)], tags=["Control"]
)
async def disconnect_all():
    """Drop all active node connections."""
    try:
        await ami_client.disconnect_all()
        audit_log("disconnect-all")
        return {"success": True, "message": "All node connections dropped"}
    except Exception as e:
        logger.error(f"/disconnect-all error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/dtmf", dependencies=[Depends(verify_api_key)], tags=["Control"])
async def send_dtmf(request: DTMFRequest):
    """
    Send a DTMF sequence to the node.

    The confirmed field must be set to true. This requirement prevents
    accidental DTMF sends from misconfigured clients.

    Valid characters: 0-9, *, #
    """
    if not request.confirmed:
        raise HTTPException(
            status_code=400,
            detail="confirmed must be true to send DTMF",
        )
    try:
        result = await ami_client.send_dtmf(request.sequence)
        audit_log("dtmf", f"sequence={request.sequence}")
        return {
            "success": True,
            "message": f"DTMF sequence '{request.sequence}' sent",
            "sequence": request.sequence,
        }
    except Exception as e:
        logger.error(f"/dtmf error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/macro", dependencies=[Depends(verify_api_key)], tags=["Control"])
async def execute_macro(request: MacroRequest):
    """
    Execute a macro defined in rpt.conf.

    Macros must be defined in your node's rpt.conf before use.
    See the ASL3 documentation for macro configuration.
    """
    try:
        result = await ami_client.execute_macro(request.macro_number)
        audit_log("macro", f"macro_number={request.macro_number}")
        return {
            "success": True,
            "message": f"Macro {request.macro_number} executed",
            "macro_number": request.macro_number,
        }
    except Exception as e:
        logger.error(f"/macro error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
