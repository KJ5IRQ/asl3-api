"""Protected operator traffic policy.

Universal correctness lives elsewhere. These choices are operator policy and
can only be changed in server configuration, never by an HTTP/MCP request.
"""
from .models import ProblemError

_DEFAULTS = {
    "link_node": {"active": "deny", "unknown": "deny"},
    "announce": {"active": "deny", "unknown": "deny"},
    "unlink_node": {"active": "allow", "unknown": "allow"},
    "unlink_all": {"active": "allow", "unknown": "allow"},
}


class TrafficPolicy:
    def __init__(self, config):
        self.config = config

    def setting(self, kind: str, state: str) -> str:
        if kind not in _DEFAULTS or state not in {"active", "unknown"}:
            raise ValueError("Unsupported policy lookup")
        return self.config.get(
            f"policy.traffic.{kind}.{state}",
            _DEFAULTS[kind][state],
        )

    def characteristics(self) -> dict:
        return {
            kind: {
                state: self.setting(kind, state)
                for state in ("active", "unknown")
            }
            for kind in _DEFAULTS
        }

    def enforce(self, kind: str, node_state):
        """Raise a stable policy error when control is not currently eligible."""
        traffic = node_state.traffic_state
        if traffic == "CLEAR":
            return

        if traffic == "ACTIVE":
            if self.setting(kind, "active") == "allow":
                return
            raise ProblemError(
                409,
                "POLICY_DENIED_ACTIVE_TRAFFIC",
                f"{kind} is denied by protected server policy while traffic is active.",
            )

        if traffic == "UNKNOWN":
            if self.setting(kind, "unknown") == "allow":
                return
            raise ProblemError(
                409,
                "POLICY_DENIED_STATE_UNKNOWN",
                f"{kind} is denied by protected server policy while traffic state is unknown.",
            )

        raise ProblemError(
            503,
            "STATE_UNKNOWN",
            "Traffic state could not be classified safely.",
        )
