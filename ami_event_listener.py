"""
AMI Event Listener for ASL3-API.

Maintains a persistent subscription to the Asterisk Manager Interface event
stream using panoramisk. When app_rpt fires AMI UserEvents (injected by the
rpt.conf [events] shell scripts), this module parses them and broadcasts
structured JSON to every connected SSE client via per-client asyncio Queues.

Also emits node connect/disconnect events detected by diffing get_connected_nodes()
on a 5-second fallback poll, so those events work even without rpt.conf hooks.

Reconnect behaviour: exponential backoff starting at 2s, capped at 60s.
"""
import asyncio
import logging
import weakref
from datetime import datetime, timezone
from typing import Optional, Set

from config import config

logger = logging.getLogger(__name__)

# Backoff config
_BACKOFF_BASE   = 2    # seconds
_BACKOFF_MAX    = 60   # seconds
_FALLBACK_POLL  = 5    # seconds between node list polls


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AMIEventListener:
    """
    Persistent AMI event listener with per-client SSE broadcast queues.

    Usage:
        listener = AMIEventListener(ami_client_instance)
        await listener.start()

        # In SSE endpoint:
        q = listener.subscribe()
        try:
            event = await asyncio.wait_for(q.get(), timeout=15)
            ...
        finally:
            listener.unsubscribe(q)

        await listener.stop()
    """

    def __init__(self, ami_client):
        self._ami = ami_client
        self._queues: Set[asyncio.Queue] = set()
        self._listener_task: Optional[asyncio.Task] = None
        self._fallback_task: Optional[asyncio.Task] = None
        self._known_nodes: Set[str] = set()
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start the event listener and fallback poll loop."""
        self._running = True
        self._listener_task = asyncio.create_task(
            self._listener_loop(), name="ami_event_listener"
        )
        self._fallback_task = asyncio.create_task(
            self._fallback_poll_loop(), name="ami_fallback_poll"
        )
        logger.info("AMI event listener started")

    async def stop(self):
        """Cancel listener and fallback tasks."""
        self._running = False
        for task in (self._listener_task, self._fallback_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("AMI event listener stopped")

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        """Register a new SSE client. Returns a queue that receives events."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.add(q)
        logger.debug(f"SSE client subscribed (total: {len(self._queues)})")
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """Deregister an SSE client queue."""
        self._queues.discard(q)
        logger.debug(f"SSE client unsubscribed (total: {len(self._queues)})")

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def broadcast(self, event: dict):
        """Push an event to all connected SSE client queues."""
        if not self._queues:
            return
        dead = set()
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow client -- drop and mark for removal
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

    # ------------------------------------------------------------------
    # AMI UserEvent listener loop (with reconnect backoff)
    # ------------------------------------------------------------------

    async def _listener_loop(self):
        """
        Register a callback on panoramisk Manager for UserEvent events.

        panoramisk fires registered callbacks when unsolicited AMI events
        arrive. We register for the UserEvent event type and filter by
        UserEvent header == "ASL3Event".

        On connection loss panoramisk will attempt its own reconnect (ping_delay
        config). We watch for that and re-register callbacks after reconnect.
        """
        backoff = _BACKOFF_BASE
        while self._running:
            try:
                manager = self._ami.manager
                if manager is None:
                    logger.warning("AMI manager not available -- waiting for connection")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue

                # Register callback for UserEvent
                manager.register_event("UserEvent", self._on_user_event)
                logger.info("AMI UserEvent listener registered")
                backoff = _BACKOFF_BASE  # reset on success

                # Keep alive -- wait until cancelled or running stops
                while self._running:
                    await asyncio.sleep(10)
                    # Verify AMI is still alive
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
        """
        panoramisk event callback -- fires on every UserEvent from AMI.

        We filter for UserEvent == "ASL3Event" and parse the EventName field
        set by the rpt.conf shell scripts.

        This is called from the panoramisk event loop -- schedule broadcast
        as a coroutine to avoid blocking it.
        """
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

            # Schedule broadcast without blocking panoramisk
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.broadcast(payload))
            )

        except Exception as e:
            logger.error(f"Error processing AMI UserEvent: {e}")

    # ------------------------------------------------------------------
    # Fallback poll loop -- node connect/disconnect + variable snapshot
    # ------------------------------------------------------------------

    async def _fallback_poll_loop(self):
        """
        Poll connected nodes every 5 seconds and emit link events.
        Also emits a node.variables.snapshot every 10 seconds for
        clients that connect after the last UserEvent.
        """
        snapshot_counter = 0
        while self._running:
            try:
                await asyncio.sleep(_FALLBACK_POLL)
                snapshot_counter += 1

                if not self._ami.connected:
                    continue

                # Node connect/disconnect detection
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
                    logger.debug(f"Fallback node poll error: {e}")

                # Periodic variable snapshot (every 10s = every 2 polls)
                if snapshot_counter % 2 == 0 and self._queues:
                    try:
                        variables = await self._ami.get_node_variables()
                        await self.broadcast(self._make_event(
                            "node.variables.snapshot", {"variables": variables}
                        ))
                    except Exception as e:
                        logger.debug(f"Variable snapshot error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fallback poll loop error: {e}")


# Global singleton -- wired up in asl_agent.py lifespan
ami_event_listener: Optional[AMIEventListener] = None
