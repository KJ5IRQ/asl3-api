"""Asterisk Manager Interface client for ASL3-API."""
import asyncio
import logging
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
            await self.manager.close()
            self.connected = False
            logger.info("Disconnected from AMI")

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

    async def get_node_stats(self) -> Dict:
        """Return parsed statistics for the configured node."""
        response = await self.send_command(f"rpt stats {config.node_number}")
        return self._parse_stats_response(response)

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
        """
        ilink_mode = 2 if monitor_only else 3
        command = f"rpt cmd {config.node_number} ilink {ilink_mode} {node_number}"
        await self.send_command(command)

        # AllStar needs a few seconds to establish the link
        await asyncio.sleep(8)

        nodes = await self.get_connected_nodes()
        connected = any(n["node"] == node_number for n in nodes)

        if not connected:
            return {
                "success": False,
                "error": f"Node {node_number} did not connect — it may be offline or unreachable",
                "command": command,
            }
        return {"success": True, "command": command, "node": node_number}

    async def disconnect_node(self, node_number: str) -> Dict:
        """Disconnect from a specific remote node. ilink mode 1 = disconnect."""
        command = f"rpt cmd {config.node_number} ilink 1 {node_number}"
        await self.send_command(command)

        await asyncio.sleep(5)

        nodes = await self.get_connected_nodes()
        still_connected = any(n["node"] == node_number for n in nodes)

        if still_connected:
            return {
                "success": False,
                "error": f"Node {node_number} is still connected",
                "command": command,
            }
        return {"success": True, "command": command, "node": node_number}

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
        0-9, *, and #. The sequence is sent as-is; no validation is
        performed here — callers should validate before invoking.
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

    def _parse_stats_response(self, response: Dict) -> Dict:
        """Parse 'rpt stats' output into a structured dict."""
        output = response.get("Output", [])
        if isinstance(output, str):
            output = [output]

        stats: Dict = {
            "node": config.node_number,
            "callsign": config.node_callsign,
            "raw_output": output,
        }

        for line in output:
            line = line.strip()
            if not line:
                continue

            # Fix: split on first colon only so "HH:MM:SS" uptime parses correctly
            if "Uptime" in line and ":" in line:
                stats["uptime"] = line.split(":", 1)[1].strip()

            elif "Keyups today" in line and ":" in line:
                stats["keyups_today"] = line.split(":", 1)[1].strip()

            elif "Nodes currently connected" in line and ":" in line:
                val = line.split(":", 1)[1].strip()
                stats["connected_nodes"] = None if val == "<NONE>" else val

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
