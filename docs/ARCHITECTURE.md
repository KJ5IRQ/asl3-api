# Architecture

The v1 API is implemented in the `vnext/` package and installed into the existing
FastAPI application. It needs Python, AMI, and local SQLite; no ORM, Redis, broker,
or external service is involved in operation dispatch.

| Module | Responsibility |
|---|---|
| `vnext/models.py` | Public state/operation schemas and fixed command mapping |
| `vnext/api.py` | Named credential authority, REST/SSE, problem responses |
| `vnext/observation.py` | Fresh XStat snapshots and strict ALINKS parsing |
| `vnext/software.py` | Read-only Asterisk/app_rpt version evidence for capabilities |
| `vnext/transport.py` | Separate one-shot AMI observation/control sessions |
| `vnext/ledger.py` | SQLite transactions, idempotency, OS lock, crash recovery |
| `vnext/service.py` | Admission, serialization, durable barrier, effect observation |
| `ami_event_listener.py` | Legacy SSE adapter over canonical snapshots |
| `ami_client.py` | Legacy observation compatibility and blocked old control helpers |
| `node_cache.py` | Public metadata cache, never target authorization |

Admission commits a QUEUED operation. An in-process async lock serializes its
execution. A dedicated session authenticates, the operation commits
DISPATCH_STARTED, and the transport writes one fixed command once. The transport
contains no reconnect or replay path. It correlates replies by ActionID and
rejects malformed framing. Acknowledgment and effect are recorded separately;
link effect checks use subsequent fresh native snapshots.

The OS advisory lock is held throughout the runtime, including recovery and
shutdown. Recovery runs before any network connection. Ledger updates are short,
synchronous transactions so there is no scheduling gap between the dispatch
commit and the control write. The service must run with a local filesystem and
one worker; all instances for a node must share the lock directory.

Panoramisk remains only on the legacy observation connection. Its reconnect
behavior cannot replay v1 controls, and the old generic command method rejects
anything outside its observation allowlist. Legacy HTTP controls map to the v1
operation service. H1 mapping tests inject a fake command boundary explicitly;
transport/ledger tests verify the production dispatch boundary independently.

Each native observation opens an independent AMI session, so events cannot be
mistaken for responses from an earlier connection. A serialized observer rejects
in-flight results after lifecycle invalidation. Both the v1 SSE resource and
legacy events use these snapshots; UserEvent payloads and external shell scripts
are not authoritative. Snapshot cadence may miss short transitions.

`/v1/capabilities` reports the installed Asterisk and app_rpt versions from a
separate bounded read-only probe (`vnext/software.py`): native `CoreSettings`
plus app_rpt's own `rpt show version`. The probe carries no node argument and no
dispatch barrier, opens no ledger transaction, and is never on the control path.
Unusable evidence degrades that component to `detected: false` instead of a
guess, and the resource still answers `200`. See
[v1 semantics](VNEXT.md#capabilities-and-software-versions).

See [v1 semantics](VNEXT.md) for state/effect distinctions, recovery, retention,
source references, and the exact limitations of the result contract.
