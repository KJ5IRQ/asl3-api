"""
Event handler for ASL3-API.

Monitors node connection changes by polling AMI every 5 seconds.
When a change is detected, it:
  1. Broadcasts the event to all SSE clients via ami_event_listener
  2. Optionally POSTs a webhook notification if webhooks are enabled

Webhooks are disabled by default. To enable, set webhooks.enabled: true
in config.yaml and provide a webhooks.url endpoint.

Note: Polling is the fallback for link connect/disconnect events only.
RX/TX keyed events are delivered via the AMI UserEvent listener which
requires the rpt.conf [events] configuration. See INSTALLATION.md.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Set

import aiohttp

from config import config

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5  # seconds -- reduced from 30 for faster fallback detection


class EventHandler:
    """Poll for node connection changes and optionally fire webhooks."""

    def __init__(self, ami_client):
        self.ami_client = ami_client
        self._session: Optional[aiohttp.ClientSession] = None
        self._known_nodes: Set[str] = set()
        # Reference to ami_event_listener set by asl_agent at startup
        self.event_listener = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Initialize the HTTP session if webhooks are enabled."""
        if config.webhooks_enabled:
            if not config.webhook_url:
                logger.warning(
                    "Webhooks enabled but webhooks.url is not set — disabling"
                )
            else:
                self._session = aiohttp.ClientSession()
                logger.info(f"Webhooks enabled → {config.webhook_url}")
        else:
            logger.info("Event handler started (webhooks disabled)")

    async def stop(self):
        """Close the HTTP session on shutdown."""
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("Event handler stopped")

    # ------------------------------------------------------------------
    # Monitoring loop
    # ------------------------------------------------------------------

    async def monitoring_loop(self):
        """
        Background task: poll for node changes every POLL_INTERVAL seconds.

        This runs regardless of webhook configuration when the event listener
        is active. It provides fallback node connect/disconnect detection for
        SSE clients and optionally delivers webhooks.
        """
        logger.info(f"Node monitoring loop started (interval: {POLL_INTERVAL}s)")
        while True:
            try:
                await self._check_node_changes()
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                logger.info("Monitoring loop cancelled")
                break
            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")
                await asyncio.sleep(POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    async def _check_node_changes(self):
        """Compare current connected nodes against last known state."""
        try:
            current_nodes = await self.ami_client.get_connected_nodes()
            current_set: Set[str] = {n["node"] for n in current_nodes}

            for node in current_set - self._known_nodes:
                info = next(
                    (n for n in current_nodes if n["node"] == node), {}
                )
                await self._on_node_connect(node, info.get("mode", ""))

            for node in self._known_nodes - current_set:
                await self._on_node_disconnect(node)

            self._known_nodes = current_set

        except Exception as e:
            logger.debug(f"Node change check failed: {e}")

    # ------------------------------------------------------------------
    # Event callbacks
    # ------------------------------------------------------------------

    async def _on_node_connect(self, node_number: str, mode: str = ""):
        logger.info(f"Node connected: {node_number} (mode={mode})")
        # Broadcast to SSE clients if listener is wired up
        if self.event_listener:
            await self.event_listener.broadcast(
                self.event_listener._make_event("link.connected", {
                    "connected_node": node_number,
                    "mode": mode,
                })
            )
        await self._send_webhook("node_connected", {
            "connected_node": node_number,
            "mode": mode,
        })

    async def _on_node_disconnect(self, node_number: str):
        logger.info(f"Node disconnected: {node_number}")
        if self.event_listener:
            await self.event_listener.broadcast(
                self.event_listener._make_event("link.disconnected", {
                    "disconnected_node": node_number,
                })
            )
        await self._send_webhook("node_disconnected", {
            "disconnected_node": node_number,
        })

    # ------------------------------------------------------------------
    # Webhook delivery
    # ------------------------------------------------------------------

    async def _send_webhook(self, event_type: str, data: dict):
        """POST a webhook payload. Silently skips if webhooks are disabled."""
        if not config.webhooks_enabled or not self._session:
            return

        payload = {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node": config.node_number,
            "callsign": config.node_callsign,
            "data": data,
        }

        try:
            async with self._session.post(
                config.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status == 200:
                    logger.debug(f"Webhook delivered: {event_type}")
                else:
                    logger.warning(
                        f"Webhook returned {response.status} for {event_type}"
                    )
        except asyncio.TimeoutError:
            logger.warning(f"Webhook timed out for {event_type}")
        except Exception as e:
            logger.error(f"Webhook delivery failed for {event_type}: {e}")
