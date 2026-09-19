"""
Shared fixtures for the ASL3-API test suite.

config.py builds a module-level ``Config()`` that reads /opt/asl3-api/config.yaml
at import time and raises FileNotFoundError when it is missing. Nothing in the
application can be imported without that file present, so a stub ``config``
module is injected into sys.modules here, before any application import.

Replacing that stub with the real Config class is architecture-phase work; see
KNOWN LIMITATIONS in the H1 report.
"""

import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Audit log destination for the whole session. Created before the app imports
# so audit_log() never touches a real deployment path.
_AUDIT_DIR = tempfile.mkdtemp(prefix="asl3api-test-audit-")
AUDIT_FILE = str(Path(_AUDIT_DIR) / "audit.log")

TEST_NODE = "637050"
TEST_CALLSIGN = "KJ5IRQ"
TEST_API_KEY = "test-api-key"


class _StubConfig:
    """Mirrors the attribute surface of config.Config without reading a file."""

    node_number = TEST_NODE
    node_callsign = TEST_CALLSIGN

    ami_host = "127.0.0.1"
    ami_port = 5038
    ami_username = "asl3-api"
    ami_password = "test-secret"

    api_host = "127.0.0.1"
    api_port = 8073
    api_key = TEST_API_KEY

    events_enabled = True
    events_keepalive_interval = 15
    events_snapshot_interval = 10

    webhooks_enabled = False
    webhook_url = ""

    log_level = "INFO"
    audit_file = AUDIT_FILE

    # High enough that rate limiting never interferes with assertions.
    rate_limit = 100000

    connect_timeout = 12
    disconnect_timeout = 8

    def get(self, key, default=None):
        return default

    def validate(self):
        return None


def _install_stub_config():
    import types

    module = types.ModuleType("config")
    module.Config = _StubConfig
    module.config = _StubConfig()
    sys.modules["config"] = module


_install_stub_config()


class FakeManager:
    """
    Stand-in for panoramisk.Manager that records every AMI action.

    ``commands`` holds the exact Command strings submitted, which is what the
    command-correctness tests assert against.
    """

    def __init__(self, response=None):
        self.actions = []
        self.commands = []
        self._response = response if response is not None else {"Output": []}

    async def send_action(self, action):
        self.actions.append(action)
        if action.get("Action") == "Command":
            self.commands.append(action["Command"])
        if callable(self._response):
            return self._response(action)
        return self._response

    async def close(self):
        return None


@pytest.fixture
def fake_manager():
    """A connected AMIClient backed by FakeManager. Yields the manager."""
    from ami_client import ami_client

    original_manager = ami_client.manager
    original_connected = ami_client.connected

    manager = FakeManager()
    ami_client.manager = manager
    ami_client.connected = True
    try:
        yield manager
    finally:
        ami_client.manager = original_manager
        ami_client.connected = original_connected


@pytest.fixture
def audit_log_path():
    """Truncate the audit log before a test and return its path."""
    path = Path(AUDIT_FILE)
    path.write_text("")
    return path


@pytest.fixture
def client():
    """
    FastAPI TestClient.

    Constructed without the context-manager form on purpose: entering the
    context would run the lifespan handler, which opens a real AMI connection.
    """
    from fastapi.testclient import TestClient

    import asl_agent

    return TestClient(asl_agent.app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": TEST_API_KEY}
