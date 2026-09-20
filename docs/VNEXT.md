# v1 result contract

## Observation

`GET /v1/node/state` performs a fresh `Action: RptStatus`, `Command: XStat`,
`Node: <configured node>` request. Each observation has a distinct authenticated
AMI connection epoch and ActionID. No cached result from a previous connection
can satisfy the request. A bounded timeout yields a `STATE_UNKNOWN` snapshot.

Native repeated `Var` fields contain `RPT_RXKEYED`, `RPT_TXKEYED`,
`RPT_NUMALINKS`, and `RPT_ALINKS`. ALINKS is either an empty string with count
zero or `count,nodeModeKeyed,...`, e.g. `2,123TK,456RU`. `T` means transceive,
`R` monitor, `C` connecting; `K/U` are keyed/unkeyed evidence. The parser rejects
missing fields, nonbinary key states, duplicates, malformed tokens, count
mismatches, and truncation sentinels including `000000`. Native Conn entries
must also be structurally valid and agree with ALINKS adjacency/connection
state. Disagreement during app_rpt's non-atomic snapshot is unknown. A direct
link that app_rpt omits from ALINKS (for example local-monitor mode) likewise
prevents a false absence claim.

`RPT_TXKEYED` is app_rpt's main/local TX **logical** state. It is not proof of
RF, of a physically keyed transmitter, or of audio crossing a native link.
app_rpt mixes audio in two places: `RPT_CONF` carries audio on the native
AllStar links, while `RPT_TXCONF` carries local TX audio, where most ordinary
telemetry lands. On a radioless hub using `rxchannel = Local/pseudo` there is
no transmitter at all, so `RPT_TXKEYED=1` can be true while nothing is heard
on any link. Safety policy does not soften for this: `tx_keyed` true still
makes the traffic aggregate `ACTIVE`, and announcements and link changes are
refused. Reading it as "the transmitter is on" is the misinterpretation to
avoid; reading it as "the node is doing something locally, treat as busy" is
correct.

`LinkedNodes` is a topology-wide field, never direct adjacency. It is checked
for malformed/sentinel evidence but does not populate `direct_links`. Complete
snapshots have actual boolean values and a direct-link list; incomplete
snapshots have `complete: false`, `state_status: STATE_UNKNOWN`, null keyed
fields and null `direct_links`, plus diagnostic reasons. An empty list is used
only when complete evidence establishes absence.

Snapshots describe observed state at a time, not a guarantee against later
changes. `observed_at` is the API's observation time, not a hardware timestamp.

## Operations

The result-contract version is `1.0`. The backend contract version is the
adapter's AMI contract identifier, `app_rpt-rptstatus/1`. Neither is a claim
about installed software: the Asterisk and app_rpt versions a node actually
runs are reported separately in `backend_software` (see Capabilities).

| dispatch_status | Meaning |
|---|---|
| QUEUED | Admission committed; no dispatch boundary crossed |
| DISPATCH_STARTED | Durable barrier committed; transmission may have happened |
| ACKNOWLEDGED | A complete, correlated AMI success response was received |
| REJECTED | Explicit AMI or app_rpt CLI refusal |
| NOT_DISPATCHED | No control transmission was attempted |
| OUTCOME_UNKNOWN | Execution may have happened; never auto-resend |

| effect_status | Meaning |
|---|---|
| PENDING | Observation/dispatch still in progress |
| OBSERVED_SATISFIED | A complete subsequent snapshot satisfies the requested state |
| OBSERVED_PARTIAL | The target is present but the requested final state is not yet fully established |
| OBSERVED_UNSATISFIED | A complete subsequent snapshot does not satisfy the requested state |
| NOT_APPLICABLE | The requested effect is not directly observable as node state |
| UNKNOWN | Evidence is unavailable/incomplete; do not infer absence or false |
| NOT_ATTEMPTED | No effect check is appropriate because dispatch did not occur or was refused |

`STATE_UNKNOWN` is the node snapshot state used for admission and verification.
`OUTCOME_UNKNOWN` is the dispatch state used when control execution may have
happened. An acknowledged unlink with an incomplete post-dispatch snapshot is
`ACKNOWLEDGED / UNKNOWN`. Losing the command reply after possible dispatch is
`OUTCOME_UNKNOWN / UNKNOWN`.

`OBSERVED_SATISFIED` means the desired state was observed, not that this
operation caused it. `OBSERVED_UNSATISFIED` does not promise the effect cannot
happen later. Even a complete CLI success may only mean app_rpt accepted or
queued a command; configured control states may suppress its execution.
Announcements therefore finish `ACKNOWLEDGED / UNKNOWN` with
`PLAYBACK_EFFECT_NOT_OBSERVABLE`; audio playback is not claimed.

Exact target unlink is `ilink 11`, including permanent links. Unlink-all is a
single `ilink 6`. Link creation is `ilink 3` or `ilink 2`. Announcement mappings
are identify=`status 1` and status=`ilink 5`.
No arbitrary command text comes from request fields.

`time` and `version` are withdrawn from the supported announcement set. Their
mappings (`status 2` / `status 3`) were correct, but app_rpt converts both into
link telemetry text — `T <node> STATS_TIME,<epoch>` and
`STATS_VERSION,<version>` — addressed to transceive links, and the *receiving*
node's telemetry policy decides whether anything is spoken. This API can
neither observe nor control that, so it cannot honestly advertise them as
announcements. `identify` queues `ID1` into `RPT_CONF`, which is hub-originated
audio on the native links; `status` is rendered by app_rpt's own status
telemetry path.

One version sensitivity applies to `status`: on app_rpt 3.8.3 through 3.9.x an
ordinary linked STATUS carries a receiver-side telemetry exemption. On app_rpt
3.10+ that exemption moved to `LOCALSTATUS`, so linked STATUS again becomes
dependent on receiver telemetry policy. The command is correct on both; the
audible result on a remote node is not guaranteed.

A withdrawn kind is refused with `422 UNSUPPORTED_ANNOUNCEMENT` before
admission. The narrowed request schema rejects it at the route boundary,
`command_for()` refuses it as a last gate on every dispatch path, and the
legacy `/cop/time` and `/cop/version` aliases raise the same problem code
directly. No ledger row is created and no AMI command is dispatched.

All admission paths, including legacy aliases, share a local file lock,
serialization lock, ledger, and dispatch adapter. SQLite uses WAL and
`synchronous=FULL`. A failed admission or dispatch-barrier commit prevents
control writes. If result persistence fails after dispatch, the runtime marks itself unhealthy
and refuses new admissions and operation reads with `503 LEDGER_UNAVAILABLE`.
It does not guess a terminal result from stale durable data. The previously
committed dispatch boundary remains available, so a service restart recovers the
operation as `OUTCOME_UNKNOWN` without redispatch. Storage must honor fsync;
catastrophic storage loss is outside an application ledger's guarantees.

On startup the owner lock is acquired before recovery. Nonterminal rows that
crossed the barrier become terminal `OUTCOME_UNKNOWN`; queued rows become
`NOT_DISPATCHED`. Neither is auto-resumed. A process that cannot acquire the lock
fails startup before any AMI connection. Keep the lock directory stable and
shared by all local instances; use one uvicorn worker. A ledger is local to
this API, not a distributed lock or an exactly-once guarantee for Asterisk.

## Idempotency and retention

The key namespace is `(configured node, credential name, Idempotency-Key)`.
Canonical requests include operation kind plus validated parameters, including
defaults; explicit `mode: transceive` matches omitted `mode`. HTTP method/path
aliases map to semantic operation kind. Authentication is checked on every
retry. Keys are case-sensitive, 1–128 visible ASCII characters.

The same request returns the same operation and Location (202), including
terminal unknowns, without dispatch. A changed request is
`409 IDEMPOTENCY_CONFLICT`. Without a key, each admission is a new operation.
No automatic pruning exists: all operations and keys are retained indefinitely.
Operator backups must preserve SQLite consistently (use SQLite backup or stop
the service and preserve database/WAL together). Removing/replacing the ledger,
restoring an older backup, or renaming a credential can lose deduplication history.
Credential secret rotation under the same name preserves its key namespace.

## Events and errors

`GET /v1/events` emits `event: node.state` with the same snapshot schema,
immediately and every configured snapshot interval thereafter. It uses header
authentication. There is no durable event log, Last-Event-ID replay, transition
completeness claim, or external event script requirement. Consumers must handle
unknown snapshots and resubscribe after disconnects. Proxies should disable
buffering and allow an idle interval longer than snapshot interval plus the
observation timeout. The legacy event adapter also uses these native snapshots;
unknown gaps never generate invented unkey/disconnect events.

Problems follow RFC 9457 fields (`type`, `title`, `status`, `detail`, `instance`)
with an ASL `code`: `AUTHENTICATION_REQUIRED`, `AUTHORITY_DENIED`,
`INVALID_REQUEST`, `IDEMPOTENCY_CONFLICT`, `OPERATION_NOT_FOUND`,
`CONTROL_UNAVAILABLE`, `LEDGER_UNAVAILABLE`, `EVENTS_DISABLED`, `RATE_LIMITED`,
`NOT_FOUND`, `METHOD_NOT_ALLOWED`, or `HTTP_ERROR`. Unknown observation is a
successful state resource with explicit uncertainty, not a fabricated HTTP error.

## Capabilities and software versions

`GET /v1/capabilities` advertises only implemented and enabled features. It
carries semantic contract versions (`result_contract_version`,
`backend_contract_version`) and, separately, the software versions the node
actually runs, under `backend_software`:

```json
"backend_software": {
  "method": "read-only local AMI reads; no node targeting and no control dispatch",
  "asterisk": {"version": "22.5.2", "detected": true, "source": "ami:CoreSettings/AsteriskVersion"},
  "app_rpt": {"version": "1.2.3", "detected": true, "source": "ami:Command/rpt show version"}
}
```

`backend_software_version` and `backend_software_version_detected` are the flat
mirror of `backend_software.app_rpt`: the installed app_rpt this API controls.
They report `null` and `false` whenever the version was not read, and are never
derived from a contract or adapter version.

Detection uses only bounded read-only AMI reads against the local Asterisk:

- `Action: CoreSettings` — a native AMI action; `AsteriskVersion` is the running
  Asterisk build (needs the AMI `system` or `reporting` read class).
- `Action: Command` with the constant text `rpt show version` — app_rpt's own
  version command, which only prints `app_rpt version: <major>.<minor>.<patch>`
  (needs the AMI `command` class). The command text is a module constant; no
  request field, node number, or configured node can influence it.

Neither read targets a node, changes node state, or crosses the ledger's
dispatch boundary; a capabilities request cannot dispatch control. app_rpt's
on-air `status 3` is deliberately not used for discovery: it emits link
telemetry rather than returning a value to the caller. It is also not an
announcement this release supports.

Failures are explicit and per-component. If the probe cannot complete, if AMI
refuses it, or if a response is missing, repeated, or shaped differently than
the parsers accept, the affected entry reports `version: null` with
`detected: false`. Nothing is inferred from a partial answer, and one unknown
component does not make the other unknown if its own evidence was usable.
`/v1/capabilities` itself still returns `200`; it does not fail because the
node could not be asked. A version is never guessed or fabricated.

## Backend source evidence

Parsing and command mappings were checked against the upstream sources during
implementation; compatibility remains conservative when wire formats differ:

- [Native RptStatus/XStat](https://github.com/AllStarLink/app_rpt/blob/master/apps/app_rpt/rpt_manager.c)
- [ALINKS construction and truncation markers](https://github.com/AllStarLink/app_rpt/blob/master/apps/app_rpt/rpt_link.c)
- [ilink and announcement semantics](https://github.com/AllStarLink/app_rpt/blob/master/apps/app_rpt/rpt_functions.c)
- [app_rpt version command, `rpt show version`](https://github.com/AllStarLink/app_rpt/blob/master/apps/app_rpt/rpt_cli.c)
  (RptStatus itself exposes only `RptStat`, `NodeStat`, `XStat`, `SawStat`; it
  has no version command, so no native RptStatus version read exists)
- [AMI `CoreSettings` (`AsteriskVersion`) and `Command` response shapes](https://github.com/asterisk/asterisk/blob/master/main/manager.c)

No live-node or RF validation is part of the offline test suite.
