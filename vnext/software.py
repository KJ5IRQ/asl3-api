"""Read-only backend software version evidence.

Asterisk and app_rpt versions are detected with read-only local reads only:

* ``CoreSettings`` — a native Asterisk AMI action whose ``AsteriskVersion``
  field carries the running Asterisk build. It needs the AMI ``system`` (or
  ``reporting``) read class, not ``command``.
* ``Command: rpt show version`` — app_rpt's own version CLI command, whose only
  effect is to print ``app_rpt version: <major>.<minor>.<patch>``
  (``apps/app_rpt/rpt_cli.c``). The command text is a constant in this module;
  no request field can influence it.

Neither read targets a node, changes node state, or crosses the operation
ledger's dispatch boundary. The on-air ``status 3`` announcement is deliberately
not used: it transmits, and a version is not worth keying a transmitter for.

Any missing, malformed, repeated, or unavailable value stays explicitly unknown
(``version: null`` with ``detected: false``). A version is never inferred,
guessed, or substituted from a contract/adapter version.
"""
import re
from collections.abc import Mapping

ASTERISK_VERSION_ACTION = {"Action": "CoreSettings"}
APP_RPT_VERSION_ACTION = {"Action": "Command", "Command": "rpt show version"}

ASTERISK_SOURCE = "ami:CoreSettings/AsteriskVersion"
APP_RPT_SOURCE = "ami:Command/rpt show version"

METHOD = (
    "read-only local AMI reads; no node targeting and no control dispatch"
)

# Asterisk builds report values like "22.5.2" or "22.5.2+asl3-3.6.3". Evidence
# outside this numeric-dotted shape (a bare word such as "unknown", a truncated
# value, surrounding whitespace) is malformed, so it stays unknown. A version,
# even a suspicious "0.0.0", is still reported as what the node actually said.
ASTERISK_VERSION_PATTERN = re.compile(
    r"^[0-9]{1,4}(\.[0-9]{1,4})+([.+~:_-][0-9A-Za-z.+~:_-]{0,40})?$",
    re.ASCII,
)
APP_RPT_VERSION_PATTERN = re.compile(
    r"^app_rpt version: ([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})$",
    re.ASCII,
)


def _scalar(message: object, key: str) -> str | None:
    """Return exactly one non-empty value for ``key``, else None."""
    if not isinstance(message, Mapping):
        return None
    value = message.get(key)
    if not isinstance(value, str) or not value:
        return None
    return value


def _output_lines(message: object) -> list[str] | None:
    """Return the AMI ``Output`` lines, or None when the shape is unusable."""
    if not isinstance(message, Mapping):
        return None
    value = message.get("output")
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(line, str) for line in value):
        return None
    return [line.strip() for line in value if line.strip()]


def parse_asterisk_version(response: object) -> str | None:
    """Return the running Asterisk version, or None when evidence is unusable."""
    if _scalar(response, "response") != "Success":
        return None
    version = _scalar(response, "asteriskversion")
    if version is None or ASTERISK_VERSION_PATTERN.fullmatch(version) is None:
        return None
    return version


def parse_app_rpt_version(response: object) -> str | None:
    """Return the app_rpt version, or None when evidence is unusable.

    The CLI call must have been accepted (AMI ``Response: Success``) and must
    carry exactly one output line with the app_rpt version banner. Silence, a
    "No such command" refusal, or extra/duplicated output is unknown, not a
    version.
    """
    if _scalar(response, "response") != "Success":
        return None
    lines = _output_lines(response)
    if lines is None or len(lines) != 1:
        return None
    match = APP_RPT_VERSION_PATTERN.fullmatch(lines[0])
    if match is None:
        return None
    return match.group(1)


def _entry(version: str | None, source: str) -> dict:
    return {
        "version": version,
        "detected": version is not None,
        "source": source,
    }


def evidence(asterisk_response: object = None, app_rpt_response: object = None) -> dict:
    """Build the capabilities software-evidence block from raw AMI responses."""
    return {
        "method": METHOD,
        "asterisk": _entry(parse_asterisk_version(asterisk_response), ASTERISK_SOURCE),
        "app_rpt": _entry(parse_app_rpt_version(app_rpt_response), APP_RPT_SOURCE),
    }


def undetected() -> dict:
    """Evidence block for a probe that did not complete; nothing is claimed."""
    return evidence()
