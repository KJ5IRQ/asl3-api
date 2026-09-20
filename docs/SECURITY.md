# Security

The default bind is `127.0.0.1`. The systemd unit reads the configured bind through
`asl_agent.py`; an existing explicit `api.host` is preserved on upgrade. Use a
VPN such as Tailscale/WireGuard, or a TLS reverse proxy, for remote/untrusted
networks. Do not expose plaintext API or AMI ports publicly. Restrict AMI to the
node host and protect its credentials and permissions.

Use `X-API-Key`. `api.credentials` supports multiple named entries with separate
secrets and `observe`/`control` authority. Control includes observe access. Observe
credentials can read state, capabilities, public directory metadata, operation
history, and events; they cannot admit operations. Operations include credential
names, never secrets. All observation credentials see the configured node's
operation history. There is no per-user tenancy or OAuth/JWT IAM.

When named credentials are configured, they replace the legacy `api.api_key`.
Otherwise that key maps to a credential named `legacy` with observe/control
access. Rotate a secret while keeping its name to preserve the idempotency
namespace. Changing a name creates a new namespace. Names and secrets must be
unique within the configuration. Store config with mode 0600.

Both v1 and legacy control routes enforce authority. v1 never accepts query
credentials. Legacy `/events` accepts query credentials only with the explicit
`api.allow_legacy_query_key: true` compatibility setting. URLs can enter logs and
history, so keep that setting off. Use streaming fetch or another header-capable
SSE client. Do not put credential values into logs or shared command histories.

Control admission is rate-limited by source IP using the existing configurable
`security.rate_limit_per_minute`. Reverse proxy forwarded-header trust must be
limited to the actual proxy; do not blindly trust client-supplied forwarding
headers. This is a lightweight local API, not a general public multi-tenant IAM
or denial-of-service protection service.

The ledger directory is created private (0700); protect existing directories,
backups, and lock files with the same service-account ownership. SQLite must be
on local storage. All local processes controlling the configured node must use
the same lock directory. Do not unlink a held lock or share a database between
uncoordinated hosts. Advisory locks protect cooperating API owners; they do not
exclude Asterisk's scheduler, other AMI clients, or operator controls.

DTMF/macros remain disabled. v1 exposes only fixed semantic link and announcement
commands, with strict ASCII target validation. There is no public arbitrary AMI,
COP, shell, or playback endpoint. Announcements can transmit when deployed and
explicitly requested; a successful API admission never proves audio playback.

A transport loss after possible dispatch is uncertain and never auto-replayed.
Do not retry unknown effects under a new key without treating that as a new
operation. See [retention and recovery](VNEXT.md). This implementation does not
supply an active-QSO safety interlock or a guarantee that other actors cannot
change state between snapshots.
