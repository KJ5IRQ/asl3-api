"""Legacy SSE adapter over native XStat snapshots; no external event scripts."""
import asyncio
import logging
from datetime import datetime, timezone

from config import config

logger = logging.getLogger(__name__)


class AMIEventListener:
    def __init__(self, ami_client):
        self._ami = ami_client
        self._queues = set()
        self._poll_task = None
        self._known_nodes = None
        self._rxkeyed = None
        self._txkeyed = None

    async def start(self):
        if config.events_enabled:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="native_snapshot_events")

    async def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None

    def subscribe(self):
        queue = asyncio.Queue(maxsize=200)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._queues.discard(queue)

    @property
    def subscriber_count(self):
        return len(self._queues)

    async def broadcast(self, event):
        for queue in self._queues:
            if queue.full():
                queue.get_nowait()  # Bounded latest-state delivery, no replay claim.
            queue.put_nowait(event)

    def _make_event(self, kind, fields):
        return {"type": kind, "timestamp": datetime.now(timezone.utc).isoformat(),
                "node": config.node_number, "callsign": config.node_callsign, **fields}

    async def _poll_once(self):
        state = await self._ami.observer.snapshot()
        await self.broadcast(self._make_event("node.variables.snapshot", {
            "variables": {"rxkeyed": state.rx_keyed, "txkeyed": state.tx_keyed,
                          "complete": state.complete, "state_status": state.state_status,
                          "direct_links": [link.model_dump() for link in state.direct_links]
                          if state.direct_links is not None else None},
        }))
        if not state.complete:
            # A gap breaks transition continuity. Never invent disconnect/unkey.
            self._known_nodes = self._rxkeyed = self._txkeyed = None
            return
        for field, value in (("rxkeyed", state.rx_keyed), ("txkeyed", state.tx_keyed)):
            if value != getattr(self, f"_{field}"):
                await self.broadcast(self._make_event(f"node.{field}", {field: value, "node_number": state.node}))
                setattr(self, f"_{field}", value)
        current = {link.node for link in state.direct_links}
        if self._known_nodes is not None:
            for node in current - self._known_nodes:
                await self.broadcast(self._make_event("link.connected", {"connected_node": node}))
            for node in self._known_nodes - current:
                await self.broadcast(self._make_event("link.disconnected", {"disconnected_node": node}))
        self._known_nodes = current

    async def _poll_loop(self):
        while True:
            try:
                await self._poll_once()
            except Exception:
                logger.exception("Native snapshot event refresh failed")
            await asyncio.sleep(max(0.1, config.events_snapshot_interval))
