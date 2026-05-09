"""
Event handler for ASL3-API.

Monitors node connection changes by polling AMI at a configurable interval
and optionally sends webhook notifications when nodes connect or disconnect.

Webhooks are disabled by default. To enable, set webhooks.enabled = true
in config.yaml and provide a webhooks.url endpoint that accepts POST requests
with JSON payloads.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Set

import aiohttp

from config import config

logger = logging.getLogger(__name__)

# How often to poll for node connection changes (seconds)
POLL_INTERVAL = 30


class EventHandler:
    """Poll for node connection changes and fire webhooks when enabled."""

    def __init__(self, ami_client):
        self.ami_client = ami_client
        self._session: Optional[aiohttp.ClientSession] = None
        self._known_nodes: Set[str] = set()

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
    # Monitoring loop (runs as background task when webhooks are enabled)
    # ------------------------------------------------------------------

    async def monitoring_loop(self):
        """Background task: poll for node changes every POLL_INTERVAL seconds."""
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

            # New connections
            for node in current_set - self._known_nodes:
                info = next(
                    (n["info"] for n in current_nodes if n["node"] == node), ""
                )
                await self._on_node_connect(node, info)

            # Dropped connections
            for node in self._known_nodes - current_set:
                await self._on_node_disconnect(node)

            self._known_nodes = current_set

        except Exception as e:
            logger.error(f"Node change check failed: {e}")

    # ------------------------------------------------------------------
    # Event callbacks
    # ------------------------------------------------------------------

    async def _on_node_connect(self, node_number: str, info: str = ""):
        logger.info(f"Node connected: {node_number}")
        await self._send_webhook(
            "node_connected",
            {"connected_node": node_number, "info": info},
        )

    async def _on_node_disconnect(self, node_number: str):
        logger.info(f"Node disconnected: {node_number}")
        await self._send_webhook(
            "node_disconnected",
            {"disconnected_node": node_number},
        )

    # ------------------------------------------------------------------
    # Webhook delivery
    # ------------------------------------------------------------------

    async def _send_webhook(self, event_type: str, data: dict):
        """POST a webhook payload to the configured URL. Silently skips if disabled."""
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
