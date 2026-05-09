"""Configuration loader for ASL3-API."""
import yaml
from pathlib import Path
from typing import Any, Dict


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
        return self.get("api.host", "0.0.0.0")

    @property
    def api_port(self) -> int:
        return int(self.get("api.port", 8073))

    @property
    def api_key(self) -> str:
        return self.get("api.api_key", "")

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

        if not self.node_number:
            errors.append("node.number is required but not set")
        if not self.node_callsign:
            errors.append("node.callsign is required but not set")
        if not self.ami_password:
            errors.append("ami.password is required but not set")
        if not self.api_key:
            errors.append("api.api_key is required but not set")

        if errors:
            raise ValueError(
                "ASL3-API config validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
                + f"\n\nEdit {self.config_path} and restart the service."
            )


# Global singleton
config = Config()
