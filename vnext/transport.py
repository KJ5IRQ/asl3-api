"""Bounded AMI sessions with no reconnect queue or control replay.

Observation and control use short-lived direct AMI sessions. A control session
gets exactly one control write, and that write is preceded synchronously by the
caller's durable DISPATCH_STARTED barrier.
"""
import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from .software import APP_RPT_VERSION_ACTION, ASTERISK_VERSION_ACTION


class ProtocolError(Exception):
    pass


class AMISession:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.epoch = uuid4().hex
        self.control_attempted = False

    async def frame(self) -> dict:
        raw = await self.reader.readuntil(b"\r\n\r\n")
        if len(raw) > 262144:
            raise ProtocolError("AMI frame too large")
        try:
            lines = raw[:-4].decode("utf-8", errors="strict").split("\r\n")
        except UnicodeError as exc:
            raise ProtocolError("Invalid AMI encoding") from exc

        result = {}
        for line in lines:
            key, separator, value = line.partition(":")
            if not separator or not key or any(char.isspace() for char in key):
                raise ProtocolError("Malformed AMI header")
            key, value = key.lower(), value.lstrip(" ")
            if key in result:
                if key not in {"var", "conn", "output"}:
                    raise ProtocolError("Duplicate AMI header")
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(value)
            else:
                result[key] = value
        return result

    async def action(self, fields: dict, *, before_write=None) -> dict:
        action_id = uuid4().hex
        fields = {**fields, "ActionID": action_id}
        if any("\r" in str(value) or "\n" in str(value) for value in fields.values()):
            raise ValueError("AMI header injection refused")

        payload = (
            "\r\n".join(f"{key}: {value}" for key, value in fields.items())
            + "\r\n\r\n"
        ).encode()

        if before_write is not None:
            if self.control_attempted:
                raise RuntimeError("A control session cannot be reused")
            self.control_attempted = True
            # No await is permitted between this durable commit and writer.write.
            before_write()

        self.writer.write(payload)
        await self.writer.drain()

        for _ in range(100):
            response = await self.frame()
            if "event" in response and "response" not in response:
                continue
            if response.get("actionid") != action_id:
                raise ProtocolError("Stale or mismatched AMI ActionID")
            return response
        raise ProtocolError("AMI response not received within frame budget")


class AMITransport:
    """Direct one-session transport. It contains no reconnect/retry mechanism."""

    contract_version = "app_rpt-rptstatus/1"

    def __init__(self, config):
        self.config = config
        self.timeout = float(config.get("timeouts.ami_seconds", 5))

    @asynccontextmanager
    async def session(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.config.ami_host,
                self.config.ami_port,
                limit=262144,
            ),
            timeout=self.timeout,
        )

        try:
            banner = await asyncio.wait_for(
                reader.readline(),
                timeout=self.timeout,
            )
            if not banner.startswith(b"Asterisk Call Manager/"):
                raise ProtocolError("Invalid AMI banner")

            session = AMISession(reader, writer)
            login = await asyncio.wait_for(
                session.action(
                    {
                        "Action": "Login",
                        "Username": self.config.ami_username,
                        "Secret": self.config.ami_password,
                        "Events": "off",
                    }
                ),
                timeout=self.timeout,
            )
            if login.get("response") != "Success":
                raise ProtocolError("AMI authentication failed")
            yield session
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except (OSError, asyncio.TimeoutError):
                pass

    async def observe(self, node: str) -> tuple[dict, dict, str]:
        """Take XStat and SawStat in one AMI connection epoch."""
        async with self.session() as session:
            xstat = await asyncio.wait_for(
                session.action(
                    {"Action": "RptStatus", "Command": "XStat", "Node": node}
                ),
                timeout=self.timeout,
            )
            sawstat = await asyncio.wait_for(
                session.action(
                    {"Action": "RptStatus", "Command": "SawStat", "Node": node}
                ),
                timeout=self.timeout,
            )
            return xstat, sawstat, session.epoch

    async def software_versions(self) -> tuple[dict, dict]:
        """Read-only version probe in one AMI session.

        The two reads below are constants: they carry no node argument, no
        dispatch barrier, and no control write. The whole probe shares the
        single AMI timeout, so it cannot outlive normal observation bounds.
        """
        async def probe():
            async with self.session() as session:
                asterisk = await session.action(
                    dict(ASTERISK_VERSION_ACTION)
                )
                app_rpt = await session.action(
                    dict(APP_RPT_VERSION_ACTION)
                )
                return asterisk, app_rpt

        return await asyncio.wait_for(probe(), timeout=self.timeout)

    async def dispatch(self, command: str, before_write) -> dict:
        """Send one control action once. No reconnect or replay exists here."""
        async with self.session() as session:
            return await asyncio.wait_for(
                session.action(
                    {"Action": "Command", "Command": command},
                    before_write=before_write,
                ),
                timeout=self.timeout,
            )


def command_rejected(response: dict) -> bool:
    if response.get("response") in {"Error", "Failed"}:
        return True
    output = response.get("output", [])
    if isinstance(output, str):
        output = [output]
    markers = (
        "Unknown node number",
        "is not ready",
        "Unknown action name",
        "Usage: rpt cmd",
    )
    return any(
        marker.lower() in line.lower()
        for line in output
        for marker in markers
    )
