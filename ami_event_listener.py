"""
AMI Event Listener for ASL3-API.

Uses a 1-second poll loop to detect state changes in RPT_RXKEYED, RPT_TXKEYED,
and connected nodes. Only broadcasts events when state actually changes, so
clients receive clean discrete events rather than constant snapshots.

Note: The AMI UserEvent path (rpt.conf shell scripts) is retained in the code
but is not the primary event source on ASL3 -- the CLI command required to
inject UserEvents does not exist in this version of Asterisk. The poll loop
is the reliable path.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Set

from config import config

logger = logging.getLogger(__name__)

_BACKOFF_BASE  = 2
_BACKOFF_MAX   = 60
_POLL_INTERVAL = 1   # seconds -- state change detection
_SNAPSHOT_EVERY = 10  # seconds -- full variable snapshot for new clients


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AMIEventListener:
    def __init__(self, ami_client):
        self._ami = ami_client
        self._queues: Set[asyncio.Queue] = set()
        self._listener_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

        # Tracked state -- only broadcast when these change
        self._known_nodes: Set[str] = set()
        self._rxkeyed: Optional[bool] = None
        self._txkeyed: Optional[bool] = None
        self._last_snapshot: float = 0.0

    async def start(self):
        self._running = True
        self._listener_task = asyncio.create_task(
            self._listener_loop(), name="ami_event_listener"
        )
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="ami_poll"
        )
        logger.info("AMI event listener started")

    async def stop(self):
        self._running = False
        for task in (self._listener_task, self._poll_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("AMI event listener stopped")

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.add(q)
        logger.debug(f"SSE client subscribed (total: {len(self._queues)})")
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._queues.discard(q)
        logger.debug(f"SSE client unsubscribed (total: {len(self._queues)})")

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    async def broadcast(self, event: dict):
        if not self._queues:
            return
        dead = set()
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.add(q)
                logger.warning("SSE client queue full -- dropping slow client")
        self._queues -= dead

    def _make_event(self, event_type: str, data: dict) -> dict:
        return {
            "type": event_type,
            "timestamp": _now_iso(),
            "node": config.node_number,
            "callsign": config.node_callsign,
            **data,
        }

    async def _listener_loop(self):
        """Keep AMI UserEvent registration alive. Retained for future use."""
        backoff = _BACKOFF_BASE
        while self._running:
            try:
                manager = self._ami.manager
                if manager is None:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue

                manager.register_event("UserEvent", self._on_user_event)
                logger.info("AMI UserEvent listener registered")
                backoff = _BACKOFF_BASE

                while self._running:
                    await asyncio.sleep(10)
                    if not self._ami.connected:
                        logger.warning("AMI connection lost -- re-registering listener")
                        break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AMI listener loop error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def _on_user_event(self, manager, event):
        """Handle AMI UserEvents if they ever arrive (future use)."""
        try:
            if event.get("UserEvent") != "ASL3Event":
                return
            event_name = event.get("EventName", "").strip()
            node_num   = event.get("Node", config.node_number).strip()
            mapped = {
                "rxkeyed_true":  ("node.rxkeyed",  {"rxkeyed": True,  "node_number": node_num}),
                "rxkeyed_false": ("node.rxkeyed",  {"rxkeyed": False, "node_number": node_num}),
                "txkeyed_true":  ("node.txkeyed",  {"txkeyed": True,  "node_number": node_num}),
                "txkeyed_false": ("node.txkeyed",  {"txkeyed": False, "node_number": node_num}),
            }
            if event_name not in mapped:
                return
            event_type, data = mapped[event_name]
            payload = self._make_event(event_type, data)
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.broadcast(payload))
            )
        except Exception as e:
            logger.error(f"Error processing AMI UserEvent: {e}")

    async def _poll_loop(self):
        """
        Poll node variables every second. Only broadcast when state changes.

        rxkeyed/txkeyed: emit node.rxkeyed or node.txkeyed only on transition.
        connected nodes: emit link.connected or link.disconnected on change.
        Full snapshot: emit node.variables.snapshot every 10 seconds so new
        clients get current state immediately on connect.
        """
        import time
        while self._running:
            try:
                await asyncio.sleep(_POLL_INTERVAL)

                if not self._ami.connected:
                    continue

                # --- Variable state change detection ---
                try:
                    variables = await self._ami.get_node_variables()
                    now = time.monotonic()

                    rxkeyed = variables.get("rxkeyed")
                    txkeyed = variables.get("txkeyed")

                    # rxkeyed transition
                    if rxkeyed is not None and rxkeyed != self._rxkeyed:
                        self._rxkeyed = rxkeyed
                        await self.broadcast(self._make_event("node.rxkeyed", {
                            "rxkeyed": rxkeyed,
                            "node_number": config.node_number,
                        }))
                        logger.debug(f"rxkeyed -> {rxkeyed}")

                    # txkeyed transition
                    if txkeyed is not None and txkeyed != self._txkeyed:
                        self._txkeyed = txkeyed
                        await self.broadcast(self._make_event("node.txkeyed", {
                            "txkeyed": txkeyed,
                            "node_number": config.node_number,
                        }))
                        logger.debug(f"txkeyed -> {txkeyed}")

                    # Full snapshot every 10 seconds
                    if self._queues and (now - self._last_snapshot) >= _SNAPSHOT_EVERY:
                        self._last_snapshot = now
                        await self.broadcast(self._make_event(
                            "node.variables.snapshot", {"variables": variables}
                        ))

                except Exception as e:
                    logger.debug(f"Variable poll error: {e}")

                # --- Node connect/disconnect detection ---
                try:
                    current_nodes = await self._ami.get_connected_nodes()
                    current_set: Set[str] = {n["node"] for n in current_nodes}

                    for node in current_set - self._known_nodes:
                        info = next((n for n in current_nodes if n["node"] == node), {})
                        await self.broadcast(self._make_event("link.connected", {
                            "connected_node": node,
                            "mode": info.get("mode", ""),
                        }))

                    for node in self._known_nodes - current_set:
                        await self.broadcast(self._make_event("link.disconnected", {
                            "disconnected_node": node,
                        }))

                    self._known_nodes = current_set

                except Exception as e:
                    logger.debug(f"Node poll error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll loop error: {e}")


# Global singleton
ami_event_listener: Optional[AMIEventListener] = None
