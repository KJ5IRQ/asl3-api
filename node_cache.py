"""
Node database cache for ASL3-API.

Fetches the AllStar node description database (allmondb) on startup and
refreshes it every 15 minutes in the background. All lookups are served
from memory -- no per-request HTTP calls.

The official AllStar documentation specifies a 15-minute cache time for
allmondb.allstarlink.org. Do not reduce REFRESH_INTERVAL below 900 seconds.

allmondb format (pipe-delimited, one node per line):
  node_number|callsign|description|location

Example:
  637050|KJ5IRQ||Mineral Wells, Texas
  55553| Parrot+|enhanced parrot|Plano, TX
"""
import asyncio
import logging
import time
from typing import Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

ALLMONDB_URL = "https://allmondb.allstarlink.org/allmondb.php"
REFRESH_INTERVAL = 900  # 15 minutes -- matches allmondb CDN cache time
FETCH_TIMEOUT = 15       # seconds


class NodeCache:
    """In-memory cache of the AllStar node description database."""

    def __init__(self):
        # Dict keyed by node number string -> {callsign, description, location}
        self._cache: Dict[str, Dict] = {}
        self._last_fetch: float = 0.0
        self._fetch_lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Fetch the node database and start the background refresh loop."""
        await self._fetch()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(
            f"Node cache started — {len(self._cache)} nodes loaded, "
            f"refresh every {REFRESH_INTERVAL}s"
        )

    async def stop(self):
        """Cancel the background refresh task."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        logger.info("Node cache stopped")

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def lookup(self, node_number: str) -> Dict:
        """
        Look up a node by number. Returns a dict with node, callsign,
        description, and location. All fields except node may be None
        if the node is not in the database.
        """
        entry = self._cache.get(node_number)
        if entry:
            return {
                "node": node_number,
                "callsign": entry.get("callsign"),
                "description": entry.get("description"),
                "location": entry.get("location"),
            }
        return {
            "node": node_number,
            "callsign": None,
            "description": None,
            "location": None,
        }

    def enrich_node_list(self, nodes: list) -> list:
        """
        Add callsign, description, and location to each node dict in a list.
        Input nodes must have a 'node' key. Operates in-place and returns
        the same list.
        """
        for node in nodes:
            entry = self._cache.get(node.get("node", ""))
            if entry:
                node["callsign"] = entry.get("callsign")
                node["description"] = entry.get("description")
                node["location"] = entry.get("location")
            else:
                node["callsign"] = None
                node["description"] = None
                node["location"] = None
        return nodes

    @property
    def size(self) -> int:
        return len(self._cache)

    @property
    def last_updated(self) -> Optional[float]:
        return self._last_fetch if self._last_fetch > 0 else None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _refresh_loop(self):
        """Background task: refresh the cache every REFRESH_INTERVAL seconds."""
        while True:
            try:
                await asyncio.sleep(REFRESH_INTERVAL)
                await self._fetch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Node cache refresh failed: {e}")

    async def _fetch(self):
        """Fetch allmondb and rebuild the in-memory cache."""
        async with self._fetch_lock:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        ALLMONDB_URL,
                        timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
                    ) as response:
                        if response.status != 200:
                            logger.warning(
                                f"allmondb fetch returned HTTP {response.status} "
                                f"— keeping existing cache ({len(self._cache)} nodes)"
                            )
                            return

                        text = await response.text()

                new_cache: Dict[str, Dict] = {}
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split("|")
                    if not parts or not parts[0].strip():
                        continue

                    node_num = parts[0].strip()
                    callsign = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
                    description = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
                    location = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None

                    new_cache[node_num] = {
                        "callsign": callsign,
                        "description": description,
                        "location": location,
                    }

                self._cache = new_cache
                self._last_fetch = time.time()
                logger.info(f"Node cache refreshed — {len(self._cache)} nodes")

            except asyncio.TimeoutError:
                logger.warning(
                    f"allmondb fetch timed out after {FETCH_TIMEOUT}s "
                    f"— keeping existing cache ({len(self._cache)} nodes)"
                )
            except Exception as e:
                logger.error(
                    f"allmondb fetch failed: {e} "
                    f"— keeping existing cache ({len(self._cache)} nodes)"
                )


# Global singleton
node_cache = NodeCache()
