"""Configuration loader for ASL3-API."""
import re
from pathlib import Path
from typing import Any, Dict

import yaml


class Config:
    """Load and provide typed access to YAML configuration."""

    def __init__(self, config_path: str = "/opt/asl3-api/config.yaml"):
        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = {}
        self.load()

    def load(self):
        """Load configuration from YAML file."""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {self.config_path}\n"
                f"Copy config.yaml.example to {self.config_path} and edit it."
            )
        with open(self.config_path, "r") as f:
            self._config = yaml.safe_load(f)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value using dot-notation key (e.g. 'ami.host')."""
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, default)
            else:
                return default
        return value

    # --- AMI ---
    @property
    def ami_host(self) -> str:
        return self.get("ami.host", "127.0.0.1")

    @property
    def ami_port(self) -> int:
        return int(self.get("ami.port", 5038))

    @property
    def ami_username(self) -> str:
        return self.get("ami.username", "asl3-api")

    @property
    def ami_password(self) -> str:
        return self.get("ami.password", "")

    # --- Node ---
    @property
    def node_number(self) -> str:
        return str(self.get("node.number", ""))

    @property
    def node_callsign(self) -> str:
        return self.get("node.callsign", "")

    # --- API ---
    @property
    def api_host(self) -> str:
        return self.get("api.host", "127.0.0.1")

    @property
    def api_port(self) -> int:
        return int(self.get("api.port", 8073))

    @property
    def api_key(self) -> str:
        return self.get("api.api_key", "")

    # --- Events (SSE) ---
    @property
    def events_enabled(self) -> bool:
        return bool(self.get("events.enabled", True))

    @property
    def events_keepalive_interval(self) -> int:
        """Seconds between SSE keepalive comments."""
        return int(self.get("events.keepalive_interval", 15))

    @property
    def events_snapshot_interval(self) -> int:
        """Seconds between periodic variable snapshots pushed to SSE clients."""
        return int(self.get("events.snapshot_interval", 10))

    # --- Webhooks (experimental) ---
    @property
    def webhooks_enabled(self) -> bool:
        return bool(self.get("webhooks.enabled", False))

    @property
    def webhook_url(self) -> str:
        return self.get("webhooks.url", "")

    # --- Logging ---
    @property
    def log_level(self) -> str:
        return self.get("logging.level", "INFO").upper()

    @property
    def audit_file(self) -> str:
        return self.get("logging.audit_file", "/opt/asl3-api/audit.log")

    # --- Security ---
    @property
    def rate_limit(self) -> int:
        return int(self.get("security.rate_limit_per_minute", 60))

    # --- Timeouts ---
    @property
    def connect_timeout(self) -> int:
        return int(self.get("timeouts.connect_max_seconds", 12))

    @property
    def disconnect_timeout(self) -> int:
        return int(self.get("timeouts.disconnect_max_seconds", 8))

    def validate(self):
        """Validate required fields are present and non-empty. Raise on failure."""
        errors = []

        if not re.fullmatch(r"[1-9][0-9]{0,5}", self.node_number, flags=re.ASCII):
            errors.append("node.number must be 1-6 ASCII digits with no leading zero")
        if self.ami_host not in {"127.0.0.1", "::1", "localhost"}:
            errors.append("ami.host must remain localhost")
        if not self.node_callsign:
            errors.append("node.callsign is required but not set")
        if not self.ami_password:
            errors.append("ami.password is required but not set")
        if not self.api_key and not self.get("api.credentials", []):
            errors.append("api.api_key is required but not set")

        entries = self.get("api.credentials", [])
        names, keys = set(), set()
        if not isinstance(entries, list):
            errors.append("api.credentials must be a list")
            entries = []
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                    or not entry.get("name") or not isinstance(entry.get("key"), str)
                    or not entry.get("key") or not isinstance(entry.get("authority"), list)
                    or not entry["authority"]
                    or any(scope not in ("observe", "control") for scope in entry["authority"])):
                errors.append("Each credential needs name, key, and observe/control authority")
                continue
            if entry["name"] in names or entry["key"] in keys:
                errors.append("Credential names and keys must be unique")
            names.add(entry["name"])
            keys.add(entry["key"])
        for value in (self.ami_username, self.ami_password):
            if "\r" in value or "\n" in value:
                errors.append("AMI credentials must not contain newlines")
        for setting in (
            "timeouts.ami_seconds",
            "timeouts.observation_seconds",
            "operations.dispatch_timeout",
            "operations.effect_timeout",
        ):
            value = self.get(setting, 5)
            if not isinstance(value, (int, float)) or not 0 < value <= 120:
                errors.append(f"{setting} must be between 0 and 120 seconds")

        for operation, defaults in {
            "link_node": ("deny", "deny"),
            "announce": ("deny", "deny"),
            "unlink_node": ("allow", "allow"),
            "unlink_all": ("allow", "allow"),
        }.items():
            for state, default in zip(("active", "unknown"), defaults):
                value = self.get(f"policy.traffic.{operation}.{state}", default)
                if value not in {"allow", "deny"}:
                    errors.append(
                        f"policy.traffic.{operation}.{state} must be allow or deny"
                    )

        if errors:
            raise ValueError(
                "ASL3-API config validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
                + f"\n\nEdit {self.config_path} and restart the service."
            )


# Global singleton
config = Config()
