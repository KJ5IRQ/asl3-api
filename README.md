# ASL3 Remote Platform vNext

A local REST API for observing and controlling one configured AllStarLink node.
The canonical contract is `/v1`; interactive schemas are at `/docs` and
`/openapi.json`. The legacy `/version` continues to report `1.4.2` for existing
clients; use `/v1/capabilities` for backend and result-contract versions.

The API binds to `127.0.0.1:8073` by default. Authenticate with `X-API-Key`.
Remote clients should connect through a VPN (Tailscale/WireGuard) or a TLS
reverse proxy. AMI remains private. See [security](docs/SECURITY.md).

## v1 resources

| Method | Path | Authority | Meaning |
|---|---|---|---|
| GET | `/v1/node/state` | observe | Fresh native AMI XStat observation |
| GET | `/v1/capabilities` | observe | Implemented/enabled features and contract versions |
| GET | `/v1/directory/{node}` | observe | Cached public metadata; absence does not invalidate a target |
| GET | `/v1/operations` | observe | Durable operations, newest first; `limit` and `offset` |
| GET | `/v1/operations/{id}` | observe | Dispatch and effect states, including uncertainty |
| GET | `/v1/events` | observe | Periodic `node.state` SSE snapshots |
| POST | `/v1/links` | control | `{"node":"123","mode":"transceive"}` or `"monitor"` |
| DELETE | `/v1/links/{node}` | control | Exact direct unlink, including permanent links |
| DELETE | `/v1/links` | control | All links off, including permanent links |
| POST | `/v1/announcements` | control | `{"kind":"identify"}`; also `time`, `status`, `version` |

Targets must match ASCII `^[1-9][0-9]{0,5}$`. No directory lookup is required
for control admission. Private/static nodes are valid targets. Permanent-link
creation, DTMF, macros, raw AMI/COP, shell, and arbitrary playback are absent
from the v1 control surface.

Normal control admission returns **202**, an operation document, and
`Location: /v1/operations/{id}`. This means the request was durably admitted;
it does not claim that a link changed or audio played. Read the operation to
check its dispatch and effect states. Authentication, schema, idempotency,
rate-limit, and persistence failures use `application/problem+json` with stable
`code` values. See [the result contract](docs/VNEXT.md).

Read-only examples (replace the header value with your configured secret):

```bash
curl -H 'X-API-Key: YOUR_KEY' http://127.0.0.1:8073/v1/node/state
curl -H 'X-API-Key: YOUR_KEY' http://127.0.0.1:8073/v1/capabilities
curl -N -H 'X-API-Key: YOUR_KEY' http://127.0.0.1:8073/v1/events
```

SSE requires a header-capable client such as streaming `fetch`; native browser
EventSource cannot set this header. Events are snapshots, not a lossless record
of every key transition. No `rpt.conf` event shell scripts are required.

## Safe dispatch and retries

Each admitted operation gets one control transmission attempt, with no library
replay. A dedicated AMI session authenticates first, then SQLite commits
`DISPATCH_STARTED` before control bytes can be written. Loss after that boundary
can produce `OUTCOME_UNKNOWN`; it never triggers an automatic resend.

Send an optional `Idempotency-Key` (1–128 visible ASCII characters). For the same
configured node and credential name, the same key and canonical request return
the original operation; a different request returns `409 IDEMPOTENCY_CONFLICT`.
Bindings survive restarts and are retained **indefinitely**. Never delete the
ledger or change credential names to retry an uncertain operation: that loses
its deduplication identity. A new key is a new authorized operation.

## Configuration and installation

See [config.yaml.example](config.yaml.example) and
[installation](docs/INSTALLATION.md). Named credentials can have `observe`,
`control`, or both authorities. Control includes read access. When
`api.credentials` is nonempty it replaces the legacy single `api.api_key`.

SQLite and the node's advisory lock live in `/opt/asl3-api/state` by default.
Use a local filesystem and one worker per node. Every process configured for
the same node must share `operations.lock_directory`, even if its database
path differs. Do not remove an active lock file. The lock governs cooperating
API processes, not Asterisk's own scheduler or other AMI clients.

The systemd unit starts `asl_agent.py` so the configured bind address is used.
An existing installation's explicit `api.host` remains unchanged on upgrade;
review it when migrating. The installer is a deployment tool and is never
needed to run the offline tests.

## Legacy compatibility

Legacy observation endpoints remain. `/variables`, `/nodes`, and event
snapshots now derive from native XStat; unavailable legacy variable fields are
null. CLI statistics remain a legacy-only interface. Legacy active control
paths (`/connect`, `/disconnect`, `/disconnect-all`, `/cop/identify`, `/cop/time`,
`/cop/status`, `/cop/version`) now return asynchronous **202 v1 operations**.
This intentional response change prevents a second, unsafe dispatch path.
`/dtmf` and `/macro` still refuse with 503 and send nothing.

Legacy `/events` accepts header authentication by default. The optional
`api.allow_legacy_query_key` compatibility flag enables query credentials only
on that old route; `/v1/events` always requires the header.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Tests use fake AMI sessions and temporary local databases, with no live node.
H1 command mapping/refusal tests remain; new tests exercise malformed evidence,
transport uncertainty, durable barriers, restart/idempotency, process locks,
authority, and the public contract. See [architecture](docs/ARCHITECTURE.md)
and [v1 semantics](docs/VNEXT.md).
