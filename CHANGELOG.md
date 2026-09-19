## [1.4.2] - 2026-09-19

Safety hotfix. Corrects control commands that were verified wrong against
current `AllStarLink/app_rpt` source (`apps/app_rpt/rpt_functions.c`,
`apps/app_rpt/rpt_cli.c`).

### Fixed — announcement endpoints issued the wrong app_rpt commands

All four `/cop/*` endpoints were issuing control-operator commands instead of
announcements. Two of them silently changed node configuration:

| Endpoint | Issued (≤1.4.1) | What that actually does | Now issues |
|----------|-----------------|-------------------------|------------|
| `/cop/identify` | `cop 10` | **Autopatch disable** — sets `autopatchdisable = 1` | `status 1` (System ID) |
| `/cop/time` | `cop 12` | **Link disable** — sets `linkfundisable = 1` | `status 2` (System Time) |
| `/cop/status` | `cop 13` | Query system control state — announces `SS<n>` | `ilink 5` (Status) |
| `/cop/version` | `cop 14` | Change system control state; no-op without an argument | `status 3` (version) |

**If you ran 1.4.1 or earlier, check autopatch and link functions on your
node.** Calling `/cop/identify` disabled autopatch and `/cop/time` disabled link
functions, and both persist in the node's current system state. Re-enable from
the Asterisk CLI with `cop 9` and `cop 11` respectively — this API has no
endpoint that does so. See `docs/TROUBLESHOOTING.md`.

HTTP paths are unchanged for client compatibility. The `/cop/` prefix is now a
legacy path name only; no endpoint issues a COP command.

### Changed — DTMF and macro execution disabled (HTTP 503)

Neither ever worked, and both reported success anyway:

- `/dtmf` issued `rpt cmd <node> senddigits <seq>`. `senddigits` is not in
  app_rpt's `function_table`, so `rpt_function_lookup()` fails and app_rpt
  answers `Unknown action name senddigits.`
- `/macro` issued `rpt cmd <node> cop 6 <macro>`. COP 6 is *Simulate COR being
  activated (phone only)* and returns `DC_INDETERMINATE` unless the command
  source is `SOURCE_PHONE`; `rpt cmd` always uses `SOURCE_RPT`.

`ami_client` never inspected the AMI response, so both returned
`{"success": true}` and wrote an audit entry claiming execution.

They are **not** repaired in place. The commands that work (`rpt fun` and the
`macro` function class) execute arbitrary entries from the node's own
`rpt.conf`, so their effect is unbounded from this API's perspective. Both
endpoints now return HTTP 503 with a structured `capability_disabled` body,
issue no AMI command, and write an audit entry marked `rejected`. The routes are
retained so existing clients get a clear error rather than a 404.

### Added

- `CommandRejected` exception. Announcement endpoints now detect app_rpt's
  refusal diagnostics (`Unknown action name`, `Unknown node number`, `is not
  ready`, usage text) in the AMI response and return HTTP 502 instead of
  reporting success.
- `tests/` — first test suite for this repository. 39 tests covering exact
  app_rpt command strings, the disabled capabilities, audit honesty, and the
  `/capabilities` contract.

### Changed

- `GET /capabilities` now reports `"dtmf": false` and `"macros": false`, adds an
  `announcements` list and an `unavailable` map explaining each disabled
  capability. The `cop_commands: [10, 12, 13, 14]` field is **removed** — it
  advertised the mis-mapped commands, so clients auto-configuring from it
  inherited the bug.
- `ami_client.cop()`, `send_dtmf()` and `execute_macro()` are removed and
  replaced by behaviour-named methods: `force_id()`, `say_time()`,
  `say_version()`, `say_status()`.

### Note on versioning

1.4.1 shipped without bumping `app.version`, which still read `1.4.0`. This
release sets it to `1.4.2`; there is no `1.4.1` entry below.

---

## [1.4.0] - 2026-05-10

### Added

- **SSE Event Stream** (`GET /events?api_key=KEY`) — persistent server-sent events endpoint
  delivering live node state to browsers and apps without polling. Events emitted:
  `node.rxkeyed`, `node.txkeyed`, `node.variables.snapshot`, `link.connected`,
  `link.disconnected`, `health.ami`
- **`ami_event_listener.py`** — persistent AMI UserEvent subscriber with per-client asyncio
  broadcast queues, reconnect-with-exponential-backoff, and 5-second fallback poll loop
- **`GET /capabilities`** — machine-readable endpoint describing node configuration,
  available features, supported COP commands, and event types. Intended for MCP
  auto-configuration and frontend feature detection
- **`rpt_events/`** — four shell scripts (`asl3-event-rxkeyed-true/false`,
  `asl3-event-txkeyed-true/false`) that bridge rpt.conf [events] triggers to AMI
  UserEvents consumed by the SSE listener. Includes installation README
- **Query-parameter API key** (`?api_key=`) for SSE endpoint — the browser EventSource
  API does not support custom headers, so `/events` accepts `?api_key=` as an
  alternative to `X-API-Key`
- **`events:` config section** in `config.yaml` — `enabled`, `keepalive_interval`,
  `snapshot_interval`
- **`X-Accel-Buffering: no`** header on SSE responses — prevents nginx from buffering
  the event stream

### Changed

- **`event_handler.py`** — poll interval reduced from 30s to 5s; node connect/disconnect
  events now broadcast to SSE clients via `ami_event_listener` in addition to webhooks;
  monitoring loop now always runs (previously gated on `webhooks_enabled`)
- **`/nodes`** response — all fields (`callsign`, `description`, `location`) are now always
  present in every node object regardless of `?enrich=` parameter. Missing data is `null`,
  never absent
- **`/lookup/{node}`** — guaranteed consistent schema; all fields always present
- **`/audit`** — entries are now returned as structured dicts with `timestamp`, `command`,
  `details`, and `raw` fields instead of raw text strings
- **`/ping`** response — now includes `sse_clients` (count of connected SSE subscribers)
- **`/version`** response — now includes `sse_clients` and `events_enabled`
- **`manager.conf`** AMI read permissions — `user` class added to receive UserEvents
- **Version bumped** to 1.4.0

### Fixed

- SSE connections no longer time out on idle proxies due to 15-second keepalive comments
- Slow SSE clients no longer block broadcast to other clients (full queue drops the slow client)

### Upgrade Notes

1. Run `pip install -r requirements.txt` to install `sse-starlette`
2. Add the `events:` block to `config.yaml` (see `config.yaml.example`)
3. Add `user` to the `read =` line in your `manager.conf` `[asl3-api]` block and run
   `sudo asterisk -rx "manager reload"`
4. For live RX/TX events, install the `rpt_events/` scripts and add the `[events]` stanza
   to `rpt.conf`. See `rpt_events/README.md`

# Changelog

All notable changes to ASL3-API will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.0] - 2026-05-09

### Added

- `GET /version` — no-auth endpoint returning version string, Python version, node info, and node cache stats
- `GET /variables` — live app_rpt node variables via `rpt show variables`: `rxkeyed` (signal on input), `txkeyed` (transmitter active), `ext_txkeyed`, `num_links`, `links`, `num_active_links`, `active_links`, `autopatch_up`. Sourced directly from local AMI — no external API, no rate limits.
- `POST /cop/identify` — play node ID over the air (COP 10)
- `POST /cop/time` — say current time over the air (COP 12)
- `POST /cop/status` — say system status over the air (COP 13)
- `POST /cop/version` — say app_rpt software version over the air (COP 14)
- `AMIClient.cop()` — generic COP command executor
- `AMIClient.get_node_variables()` — fetches and parses `rpt show variables` output
- `AMIClient._parse_variables_response()` — parser for RPT variable output

### Notes

- `keyed` state is sourced from `RPT_RXKEYED` via local AMI (`rpt show variables`), not the external stats API. This avoids rate limiting and external dependency.
- COP numbers 10, 12, 13, 14 confirmed working on ASL3 / Asterisk 22.8.2 / Debian 13.
- `rpt showvars` is not a valid ASL3 command — use `rpt show variables` instead.

---

## [1.2.0] - 2026-05-09

### Added

- `node_cache.py` — in-memory cache of the AllStar node database (allmondb), fetched on startup and refreshed every 15 minutes. Eliminates per-request HTTP calls and respects the official AllStar 15-minute cache policy.
- `GET /nodes?enrich=true` — optionally includes callsign, location, and description for each connected node, served from the node cache at zero extra cost.
- Rate limiting via `slowapi` — control endpoints (`/connect`, `/disconnect`, `/disconnect-all`, `/dtmf`, `/macro`) are now rate-limited per IP. Configurable via `security.rate_limit_per_minute` in config.yaml.
- Config validation on startup — required fields (`node.number`, `node.callsign`, `ami.password`, `api.api_key`) are checked before the service binds. Clear error message on failure instead of cryptic AMI errors.
- TX time normalized — `tx_time_today` and `tx_time_total` now return structured objects (`raw`, `seconds`, `display`) matching the uptime format. Format: `HH:MM:SS:mmm` (ASL3 native).
- Configurable connect/disconnect timeouts via `timeouts.connect_max_seconds` and `timeouts.disconnect_max_seconds` in config.yaml.

### Changed

- `/lookup/{node}` now served from local node cache — instant response, no external HTTP call per request.
- `ami_client.py` — `lookup_node()` method removed; lookup is now handled entirely by `node_cache.py`.

---

## [1.1.0] - 2026-05-09

### Added

- `GET /lookup/{node_number}` — look up any AllStar node's callsign, location, and description from the public AllStar node database
- `check_ami_health()` — active AMI keepalive check used by `/ping`; verifies the connection is alive at call time rather than returning a stale cached boolean
- Uptime now returned as a structured object with `raw`, `seconds` (integer), and `display` (human-readable) fields instead of a raw unparsed string
- Additional fields now parsed from `rpt stats`: `keyups_total`, `kerchunks_today`, `kerchunks_total`, `dtmf_commands_today`, `dtmf_commands_total`, `tx_time_today`, `tx_time_total`, `last_dtmf_command`

### Changed

- `/connect` now polls for link confirmation every second (up to 12s) instead of waiting a hard 8-second fixed delay — fast connections return sooner
- `/disconnect` now polls every second (up to 8s) instead of a hard 5-second wait
- `connected_nodes` field removed from `/status` response — it was misleading (returned a raw mode-prefixed node string, not a count). Use `/nodes` for connected node data.
- `/ping` now performs a live AMI ping on every call instead of returning a cached connection state

### Fixed

- Uptime parser now handles both `HH:MM:SS` and `D:HH:MM:SS` formats correctly

---

## [1.0.0] - 2026-05-09

Initial public release.

### Added

- `GET /ping` — unauthenticated health check, returns service identity and AMI connection state
- `GET /status` — node uptime, keyup count, and connected node summary
- `GET /nodes` — list of currently connected nodes with connection mode (T/M/R)
- `POST /connect` — connect to a remote node in transceive or monitor-only mode
- `POST /disconnect` — disconnect from a specific node
- `POST /disconnect-all` — drop all active node connections
- `POST /dtmf` — send a DTMF sequence to the node; requires `confirmed: true`
- `POST /macro` — execute a macro defined in rpt.conf
- `GET /audit` — recent command history from the audit log
- FastAPI auto-generated interactive docs at `/docs`
- YAML-based configuration with typed property accessors
- API key authentication on all control endpoints
- Timestamped audit log of all executed commands
- Webhook support for node connect/disconnect events (disabled by default)
- systemd service with security hardening flags
- Dual-mode installer: guided (explains each step) and auto (prompts only for config values)
- Installer auto-detects the current user — no hardcoded username assumptions

### Tested On

- ASL3 / Asterisk 22.8.2
- Debian 13 (Trixie)
- Raspberry Pi 4B (aarch64)
- Python 3.13

### Known Issues

- Node connection verification takes ~8 seconds (Asterisk/AllStar timing constraint)
- Webhook event batching not yet implemented; webhooks disabled by default
- Uptime string format varies by ASL3 version

---

[1.3.0]: https://github.com/KJ5IRQ/asl3-api/releases/tag/v1.3.0
[1.2.0]: https://github.com/KJ5IRQ/asl3-api/releases/tag/v1.2.0
[1.1.0]: https://github.com/KJ5IRQ/asl3-api/releases/tag/v1.1.0
[1.0.0]: https://github.com/KJ5IRQ/asl3-api/releases/tag/v1.0.0
