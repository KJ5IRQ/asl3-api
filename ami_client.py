"""Asterisk Manager Interface client for ASL3-API."""
import asyncio
import logging
import re
from typing import Dict, List, Optional

from panoramisk import Manager

from config import config

logger = logging.getLogger(__name__)


class AMIClient:
    """Async AMI client wrapping panoramisk. Handles connection, commands, and parsing."""

    def __init__(self):
        self.manager: Optional[Manager] = None
        self.connected = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self):
        """Connect to the Asterisk Manager Interface."""
        try:
            self.manager = Manager(
                host=config.ami_host,
                port=config.ami_port,
                username=config.ami_username,
                secret=config.ami_password,
                ping_delay=60,
                ping_attempts=3,
            )
            await self.manager.connect()
            self.connected = True
            logger.info(f"Connected to AMI at {config.ami_host}:{config.ami_port}")
        except Exception as e:
            self.connected = False
            logger.error(f"AMI connection failed: {e}")
            raise

    async def disconnect(self):
        """Close the AMI connection."""
        if self.manager:
            try:
                await self.manager.close()
            except TypeError:
                pass  # panoramisk occasionally raises TypeError on close during restart
            self.connected = False
            logger.info("Disconnected from AMI")

    async def check_ami_health(self) -> bool:
        """
        Actively verify the AMI connection is alive by sending a ping action.

        Returns True if AMI responds, False otherwise. Updates self.connected.
        Used by /ping to provide a real-time health check rather than returning
        a stale cached boolean.
        """
        if not self.manager:
            self.connected = False
            return False
        try:
            await self.manager.send_action({"Action": "Ping"})
            self.connected = True
            return True
        except Exception:
            self.connected = False
            return False

    # ------------------------------------------------------------------
    # Core command execution
    # ------------------------------------------------------------------

    async def send_command(self, command: str) -> Dict:
        """Send an Asterisk CLI command via AMI and return the raw response."""
        if not self.connected or not self.manager:
            raise RuntimeError("AMI is not connected")
        try:
            response = await self.manager.send_action(
                {"Action": "Command", "Command": command}
            )
            return response
        except Exception as e:
            logger.error(f"AMI command failed [{command}]: {e}")
            raise

    # ------------------------------------------------------------------
    # Node status
    # ------------------------------------------------------------------

    async def get_node_stats(self, include_raw: bool = False) -> Dict:
        """Return parsed statistics for the configured node."""
        response = await self.send_command(f"rpt stats {config.node_number}")
        stats = self._parse_stats_response(response)
        if not include_raw:
            stats.pop("raw_output", None)
        return stats

    async def get_connected_nodes(self) -> List[Dict]:
        """Return a list of nodes currently connected to this node."""
        response = await self.send_command(f"rpt nodes {config.node_number}")
        return self._parse_nodes_response(response)

    # ------------------------------------------------------------------
    # Link control
    # ------------------------------------------------------------------

    async def connect_node(self, node_number: str, monitor_only: bool = False) -> Dict:
        """
        Connect to a remote AllStar node.

        ilink mode 3 = transceive (TX+RX)
        ilink mode 2 = monitor only (RX)

        Polls for connection confirmation every second up to max_wait seconds
        rather than waiting a fixed delay, so fast connections return sooner.
        """
        ilink_mode = 2 if monitor_only else 3
        command = f"rpt cmd {config.node_number} ilink {ilink_mode} {node_number}"
        await self.send_command(command)

        # Poll every second up to config timeout for the link to appear
        max_wait = config.connect_timeout
        for _ in range(max_wait):
            await asyncio.sleep(1)
            nodes = await self.get_connected_nodes()
            if any(n["node"] == node_number for n in nodes):
                return {"success": True, "command": command, "node": node_number}

        return {
            "success": False,
            "error": f"Node {node_number} did not connect within {max_wait}s — it may be offline or unreachable",
            "command": command,
        }

    async def disconnect_node(self, node_number: str) -> Dict:
        """
        Disconnect from a specific remote node. ilink mode 1 = disconnect.

        Polls for confirmation every second up to max_wait seconds.
        """
        command = f"rpt cmd {config.node_number} ilink 1 {node_number}"
        await self.send_command(command)

        max_wait = config.disconnect_timeout
        for _ in range(max_wait):
            await asyncio.sleep(1)
            nodes = await self.get_connected_nodes()
            if not any(n["node"] == node_number for n in nodes):
                return {"success": True, "command": command, "node": node_number}

        return {
            "success": False,
            "error": f"Node {node_number} is still connected after {max_wait}s",
            "command": command,
        }

    async def disconnect_all(self) -> Dict:
        """Drop all active node connections. ilink mode 6 = disconnect all."""
        command = f"rpt cmd {config.node_number} ilink 6"
        await self.send_command(command)
        return {"success": True, "command": command}

    # ------------------------------------------------------------------
    # DTMF
    # ------------------------------------------------------------------

    async def send_dtmf(self, sequence: str) -> Dict:
        """
        Send a DTMF sequence to the local node.

        Uses 'rpt cmd <node> senddigits <sequence>'. Valid characters are
        0-9, *, and #. The sequence is sent as-is; validation is handled
        by the caller.
        """
        command = f"rpt cmd {config.node_number} senddigits {sequence}"
        await self.send_command(command)
        return {"success": True, "command": command, "sequence": sequence}

    # ------------------------------------------------------------------
    # Macros
    # ------------------------------------------------------------------

    async def execute_macro(self, macro_number: str) -> Dict:
        """
        Execute a macro defined in rpt.conf.

        Macros are triggered via DTMF using the *D prefix, e.g. *D1 runs
        macro 1. This method sends the equivalent command directly via AMI
        without requiring an over-the-air DTMF transmission.
        """
        command = f"rpt cmd {config.node_number} cop 6 {macro_number}"
        await self.send_command(command)
        return {"success": True, "command": command, "macro_number": macro_number}

    # ------------------------------------------------------------------
    # Response parsers
    # ------------------------------------------------------------------

    def _parse_tx_time(self, raw: str) -> Dict:
        """
        Parse TX time string from 'rpt stats' into structured fields.

        ASL3 format: HH:MM:SS:mmm (hours:minutes:seconds:milliseconds)
        Example: "00:02:14:30" = 2 minutes 14 seconds 30 milliseconds

        Returns a dict with:
          raw       - the original string as-is
          seconds   - total whole seconds as an integer
          display   - human-readable string e.g. "2m 14s"
        """
        raw = raw.strip()
        result = {"raw": raw, "seconds": None, "display": raw}

        m = re.fullmatch(r"(\d+):(\d{2}):(\d{2}):(\d+)", raw)
        if not m:
            return result

        hours, minutes, secs, _ = (int(x) for x in m.groups())
        total_seconds = hours * 3600 + minutes * 60 + secs
        result["seconds"] = total_seconds

        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes or hours:
            parts.append(f"{minutes}m")
        parts.append(f"{secs}s")
        result["display"] = " ".join(parts)

        return result

    def _parse_uptime(self, raw: str) -> Dict:
        """
        Parse uptime string from 'rpt stats' into structured fields.

        ASL3 formats observed:
          HH:MM:SS          e.g. "63:47:58"
          D:HH:MM:SS        e.g. "2:14:33:22"  (days prefix)

        Returns a dict with:
          raw       - the original string as-is
          seconds   - total seconds as an integer (for sorting/math)
          display   - human-readable string e.g. "2d 14h 33m 22s"
        """
        raw = raw.strip()
        result = {"raw": raw, "seconds": None, "display": raw}

        # Try D:HH:MM:SS first, then HH:MM:SS
        m = re.fullmatch(r"(\d+):(\d{2}):(\d{2}):(\d{2})", raw)
        if m:
            days, hours, minutes, secs = (int(x) for x in m.groups())
        else:
            m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", raw)
            if m:
                days = 0
                hours, minutes, secs = (int(x) for x in m.groups())
            else:
                # Unrecognised format -- return raw only
                return result

        total_seconds = days * 86400 + hours * 3600 + minutes * 60 + secs
        result["seconds"] = total_seconds

        # Build display string, omitting zero leading units
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        if minutes or hours or days:
            parts.append(f"{minutes}m")
        parts.append(f"{secs}s")
        result["display"] = " ".join(parts)

        return result

    def _parse_stats_response(self, response: Dict) -> Dict:
        """
        Parse 'rpt stats' output into a structured dict.

        Fields returned (all optional -- None if not found in output):
          node                  - node number (from config)
          callsign              - callsign (from config)
          uptime                - structured uptime dict (raw, seconds, display)
          keyups_today          - int
          keyups_total          - int
          kerchunks_today       - int
          kerchunks_total       - int
          dtmf_commands_today   - int
          dtmf_commands_total   - int
          tx_time_today         - str (raw HH:MM:SS:mmm format from ASL)
          tx_time_total         - str
          last_dtmf_command     - str or None
          raw_output            - list of raw lines for debugging
        """
        output = response.get("Output", [])
        if isinstance(output, str):
            output = [output]

        stats: Dict = {
            "node": config.node_number,
            "callsign": config.node_callsign,
            "uptime": None,
            "keyups_today": None,
            "keyups_total": None,
            "kerchunks_today": None,
            "kerchunks_total": None,
            "dtmf_commands_today": None,
            "dtmf_commands_total": None,
            "tx_time_today": None,
            "tx_time_total": None,
            "last_dtmf_command": None,
            "raw_output": output,
        }

        for line in output:
            line = line.strip()
            if not line or ":" not in line:
                continue

            key, _, val = line.partition(":")
            key = key.strip().rstrip(".")
            val = val.strip()

            if not val:
                continue

            if key == "Uptime":
                stats["uptime"] = self._parse_uptime(val)

            elif key == "Keyups today":
                try:
                    stats["keyups_today"] = int(val)
                except ValueError:
                    stats["keyups_today"] = val

            elif key == "Keyups since system initialization":
                try:
                    stats["keyups_total"] = int(val)
                except ValueError:
                    stats["keyups_total"] = val

            elif key == "Kerchunks today":
                try:
                    stats["kerchunks_today"] = int(val)
                except ValueError:
                    stats["kerchunks_today"] = val

            elif key == "Kerchunks since system initialization":
                try:
                    stats["kerchunks_total"] = int(val)
                except ValueError:
                    stats["kerchunks_total"] = val

            elif key == "DTMF commands today":
                try:
                    stats["dtmf_commands_today"] = int(val)
                except ValueError:
                    stats["dtmf_commands_today"] = val

            elif key == "DTMF commands since system initialization":
                try:
                    stats["dtmf_commands_total"] = int(val)
                except ValueError:
                    stats["dtmf_commands_total"] = val

            elif key == "TX time today":
                stats["tx_time_today"] = self._parse_tx_time(val)

            elif key == "TX time since system initialization":
                stats["tx_time_total"] = self._parse_tx_time(val)

            elif key == "Last DTMF command executed":
                stats["last_dtmf_command"] = None if val == "N/A" else val

            # NOTE: "Nodes currently connected to us" is intentionally not
            # parsed here. That field returns a raw mode-prefixed node string
            # (e.g. "T55553") which is ambiguous and misleading. Use /nodes
            # for accurate connected node data.

        return stats

    def _parse_nodes_response(self, response: Dict) -> List[Dict]:
        """
        Parse 'rpt nodes' output into a list of connected node dicts.

        Each dict has keys: node (str), mode (str: T/M/R/blank), info (str).
        Mode prefix meanings:
          T = transceive (full duplex)
          M = monitor (receive only, legacy prefix)
          R = receive only (current ASL3 prefix)
        """
        output = response.get("Output", [])
        if isinstance(output, str):
            output = [output]

        nodes = []
        for line in output:
            line = line.strip()
            if not line or line.startswith("*") or "<NONE>" in line:
                continue

            # Lines may be comma-separated: "T427060, T516596, T54199"
            entries = [e.strip() for e in line.split(",") if e.strip()]
            for entry in entries:
                mode = ""
                node_num = entry
                if entry and entry[0] in ("T", "M", "R"):
                    mode = entry[0]
                    node_num = entry[1:]
                if node_num:
                    nodes.append({"node": node_num, "mode": mode, "info": ""})

        return nodes


# Global singleton used by asl_agent.py and event_handler.py
ami_client = AMIClient()
