# Architecture

## Overview

ASL3-API is a FastAPI application that runs on your AllStar node's Raspberry Pi. It translates HTTP REST requests into Asterisk Manager Interface (AMI) commands, executes them against the local Asterisk process, and returns structured JSON responses.

```
HTTP Client (browser, curl, Chrome extension, automation platform)
        |
        | HTTP  —  X-API-Key header  —  rate limited per IP
        |
ASL3-API  (FastAPI + uvicorn, port 8073, Raspberry Pi)
        |
        +-- node_cache.py (allmondb, refreshed every 15 min)
        |
        | AMI protocol  —  port 5038, localhost only
        |
Asterisk / ASL3  (your AllStar node)
        |
        | app_rpt ilink / cop / rpt commands
        |
AllStar Link network
```

On startup, ASL3-API also fetches the AllStar node database from `allmondb.allstarlink.org` and holds it in memory. All node lookups are served from this local cache — no per-request external HTTP calls.

---

## Components

### `asl_agent.py` — FastAPI Application

The entry point and HTTP layer. Responsibilities:

- Validates required config fields on startup before binding the port
- Connects to AMI via `ami_client` and starts the node cache
- Validates API keys on every protected request
- Enforces per-IP rate limits on control endpoints via `slowapi`
- Validates all request bodies (node numbers must be numeric, DTMF must be valid characters)
- Delegates all AMI operations to `ami_client`
- Delegates all node lookups to `node_cache`
- Writes timestamped entries to the audit log
- Returns structured JSON responses
- Starts the optional webhook monitoring loop on startup if enabled

FastAPI automatically generates interactive API documentation at `/docs` (Swagger UI) and `/redoc`.

### `ami_client.py` — AMI Client

All communication with Asterisk goes through this module. Responsibilities:

- Manages the persistent AMI connection using [panoramisk](https://github.com/gawel/panoramisk)
- Translates REST operations into AMI commands
- Parses AMI text responses into structured Python dicts and lists
- Polls for link state confirmation after connect/disconnect operations
- Provides active AMI health checking via `check_ami_health()`

Key AMI commands used:

| Operation | AMI Command |
|-----------|-------------|
| Health check | `Action: Ping` |
| Get node stats | `rpt stats {node}` |
| Get connected nodes | `rpt nodes {node}` |
| Get node variables | `rpt show variables {node}` |
| Connect (transceive) | `rpt cmd {node} ilink 3 {remote}` |
| Connect (monitor) | `rpt cmd {node} ilink 2 {remote}` |
| Disconnect one node | `rpt cmd {node} ilink 1 {remote}` |
| Disconnect all | `rpt cmd {node} ilink 6` |
| Send DTMF | `rpt cmd {node} senddigits {sequence}` |
| Execute macro | `rpt cmd {node} cop 6 {macro_number}` |
| Play node ID | `rpt cmd {node} cop 10` |
| Say time | `rpt cmd {node} cop 12` |
| Say system status | `rpt cmd {node} cop 13` |
| Say app_rpt version | `rpt cmd {node} cop 14` |

### `node_cache.py` — Node Database Cache

Fetches the AllStar node description database (allmondb) on startup and refreshes it every 15 minutes in a background asyncio task. All `/lookup` calls and `/nodes?enrich=true` requests are served from this in-memory dict — no per-request HTTP calls, instant response.

The official AllStar documentation specifies a 15-minute minimum cache interval for allmondb. ASL3-API respects this exactly.

On startup the cache logs how many nodes were loaded. On a typical AllStar network this is approximately 40,000 nodes. If a refresh fails, the stale cache is retained and a warning is logged — the service continues running.

### `config.py` — Configuration

Loads `config.yaml` on startup and exposes all settings as typed Python properties. Uses dot-notation getters (`config.ami_host`, `config.api_key`, etc.) so the rest of the codebase never parses YAML directly.

Includes a `validate()` method called during startup that checks all required fields are present and non-empty. If validation fails, the service logs a clear error and exits before binding the port.

Required fields: `node.number`, `node.callsign`, `ami.password`, `api.api_key`.

### `event_handler.py` — Webhook Events (Experimental)

Polls for node connection changes every 30 seconds and fires HTTP POST webhooks when nodes connect or disconnect. Disabled by default (`webhooks.enabled: false` in config.yaml).

When enabled, sends JSON payloads to the configured URL:

```json
{
  "event_type": "node_connected",
  "timestamp": "2026-05-09T12:00:00+00:00",
  "node": "637050",
  "callsign": "KJ5IRQ",
  "data": {
    "connected_node": "55553",
    "info": ""
  }
}
```

Compatible with n8n, Zapier, Home Assistant webhooks, or any HTTP endpoint.

---

## Request Flow

### Example: Connect to a node

1. Client sends `POST /connect` with `{"node": "55553", "monitor_only": false}` and `X-API-Key` header
2. slowapi checks the per-IP rate limit
3. FastAPI validates the API key
4. Pydantic validates the request body (node must be numeric)
5. `ami_client.connect_node("55553", False)` is called
6. AMI command sent: `rpt cmd 637050 ilink 3 55553`
7. Polling loop checks `rpt nodes` every second, up to `timeouts.connect_max_seconds` (default 12s)
8. On confirmation, audit log entry written
9. JSON response returned: `{"success": true, "node": "55553", "mode": "transceive"}`

The polling wait is an AllStar/Asterisk constraint — the ilink command initiates an IAX2 connection that takes several seconds to negotiate. The timeout ceiling is configurable in `config.yaml` for slow or intercontinental links.

### Example: Look up a node

1. Client sends `GET /lookup/55553` with `X-API-Key` header
2. FastAPI validates the API key
3. `node_cache.lookup("55553")` called — instant dict lookup, no HTTP call
4. Response returned: `{"node": "55553", "callsign": "Parrot+", "location": "Plano, TX", "description": "enhanced parrot"}`

### Example: Get keyed state

1. Client sends `GET /variables` with `X-API-Key` header
2. FastAPI validates the API key
3. `ami_client.get_node_variables()` sends `rpt show variables 637050` via AMI
4. Response parsed: `RPT_RXKEYED`, `RPT_TXKEYED`, `RPT_NUMLINKS`, etc.
5. Response returned with boolean and integer fields

---

## app_rpt Variables Reference

The `/variables` endpoint returns the following fields sourced from `rpt show variables`:

| Variable | API field | Type | Meaning |
|----------|-----------|------|---------|
| `RPT_RXKEYED` | `rxkeyed` | bool | Signal present on node input (being keyed) |
| `RPT_TXKEYED` | `txkeyed` | bool | Transmitter currently active |
| `RPT_ETXKEYED` | `ext_txkeyed` | bool | External TX keyed |
| `RPT_NUMLINKS` | `num_links` | int | Number of connected links |
| `RPT_LINKS` | `links` | str\|null | Comma-separated link list |
| `RPT_NUMALINKS` | `num_active_links` | int | Number of active links |
| `RPT_ALINKS` | `active_links` | str\|null | Active link list |
| `RPT_AUTOPATCHUP` | `autopatch_up` | bool | Autopatch currently active |

---

## Connection Mode Reference

AllStar ilink modes used by ASL3-API:

| Mode | ilink value | Meaning |
|------|-------------|---------|
| Transceive | 3 | Full duplex — your node TX and RX to the remote node |
| Monitor | 2 | Receive only — you hear the remote node but do not transmit to it |
| Disconnect | 1 | Disconnect from a specific node |
| Disconnect all | 6 | Drop all active links |

The `rpt nodes` output prefixes each node number with a mode character:
- `T` = transceive
- `R` = receive only (monitor, current ASL3)
- `M` = monitor (legacy prefix, older ASL versions)

---

## COP Command Reference

COP (Control Operator) commands confirmed on ASL3 / Asterisk 22.8.2:

| COP | API Endpoint | Effect |
|-----|-------------|--------|
| 10 | `POST /cop/identify` | Play node ID over the air |
| 12 | `POST /cop/time` | Say current time over the air |
| 13 | `POST /cop/status` | Say system status over the air |
| 14 | `POST /cop/version` | Say app_rpt software version over the air |

The underlying AMI command for all COP operations is `rpt cmd {node} cop {number}`.

---

## Data Formats

### Uptime

Returned by `/status` as a structured object:

```json
"uptime": {
  "raw": "64:22:47",
  "seconds": 231767,
  "display": "64h 22m 47s"
}
```

Handles both `HH:MM:SS` and `D:HH:MM:SS` formats from different ASL3 versions.

### TX Time

Returned by `/status` as a structured object:

```json
"tx_time_today": {
  "raw": "00:02:14:30",
  "seconds": 134,
  "display": "2m 14s"
}
```

ASL3 uses `HH:MM:SS:mmm` format (hours, minutes, seconds, milliseconds). The milliseconds field is preserved in `raw` but not included in `seconds`.

---

## Security Model

### Authentication

Every endpoint except `/ping` and `/version` requires an `X-API-Key` header. The key is a random 256-bit value stored in `config.yaml`. There is no session management, no token expiry, and no user accounts — one key controls the API. Rotate it periodically or immediately if compromised.

### Rate Limiting

Control endpoints (`/connect`, `/disconnect`, `/disconnect-all`, `/dtmf`, `/macro`, `/cop/*`) are rate-limited per source IP using `slowapi`. The default is 60 requests/minute, configurable via `security.rate_limit_per_minute` in `config.yaml`. Clients that exceed the limit receive HTTP 429.

### AMI Isolation

AMI is bound to `127.0.0.1:5038` by Asterisk. ASL3-API connects to it from the same machine. No external party can reach AMI directly regardless of firewall configuration.

### Service Hardening

The systemd service runs with:

- `NoNewPrivileges=true` — cannot escalate to root
- `PrivateTmp=true` — isolated temporary directory
- `ProtectSystem=strict` — filesystem is read-only except for `ReadWritePaths`
- `ProtectHome=true` — cannot access user home directories
- `ReadWritePaths=/opt/asl3-api` — only the install directory is writable
- `ReadOnlyPaths=/etc/asterisk` — can read Asterisk config but not modify it

---

## Extending ASL3-API

### Adding a new endpoint

1. Add a method to `AMIClient` in `ami_client.py` that issues the appropriate AMI command
2. Add a route to `asl_agent.py` with a Pydantic request model if the endpoint takes a body
3. Apply `@limiter.limit(...)` if it is a control endpoint
4. Add an audit log call in the route handler
5. Update `CHANGELOG.md`

### Adding webhook events

1. Add a new event method to `EventHandler` in `event_handler.py`
2. Call `_send_webhook()` with an event type string and data dict
3. Wire it into `_check_node_changes()` or a new polling loop

### Running multiple nodes

ASL3-API is designed for a single node per instance. To run it against multiple nodes on the same Pi, run multiple instances on different ports with separate config files and service units.
