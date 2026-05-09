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
