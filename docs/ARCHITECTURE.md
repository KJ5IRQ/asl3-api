# Architecture

## Overview

ASL3-API is a FastAPI application that runs on your AllStar node's Raspberry Pi. It translates HTTP REST requests into Asterisk Manager Interface (AMI) commands, executes them against the local Asterisk process, and returns structured JSON responses.

```
HTTP Client (browser, curl, Chrome extension, automation platform)
        |
        | HTTP  —  X-API-Key header
        |
ASL3-API  (FastAPI + uvicorn, port 8073, Raspberry Pi)
        |
        | AMI protocol  —  port 5038, localhost only
        |
Asterisk / ASL3  (your AllStar node)
        |
        | app_rpt ilink commands
        |
AllStar Link network
```

No component in this stack communicates outside your Pi except the outbound AllStar links that Asterisk itself manages. ASL3-API adds an HTTP interface on top of what Asterisk already does.

---

## Components

### `asl_agent.py` — FastAPI Application

The entry point and HTTP layer. Responsibilities:

- Starts up by connecting to AMI via `ami_client`
- Validates API keys on every protected request
- Validates request bodies (node numbers must be numeric, DTMF must be valid characters)
- Delegates all AMI operations to `ami_client`
- Writes timestamped entries to the audit log
- Returns structured JSON responses
- Starts the optional webhook monitoring loop on startup if enabled

FastAPI automatically generates interactive API documentation at `/docs` (Swagger UI) and `/redoc`.

### `ami_client.py` — AMI Client

All communication with Asterisk goes through this module. Responsibilities:

- Manages the persistent AMI connection using [panoramisk](https://github.com/gawel/panoramisk)
- Translates REST operations into AMI commands
- Parses AMI text responses into structured Python dicts and lists
- Verifies link state after connect/disconnect operations

Key AMI commands used:

| Operation | AMI Command |
|-----------|-------------|
| Get node stats | `rpt stats {node}` |
| Get connected nodes | `rpt nodes {node}` |
| Connect (transceive) | `rpt cmd {node} ilink 3 {remote}` |
| Connect (monitor) | `rpt cmd {node} ilink 2 {remote}` |
| Disconnect one node | `rpt cmd {node} ilink 1 {remote}` |
| Disconnect all | `rpt cmd {node} ilink 6` |
| Send DTMF | `rpt cmd {node} senddigits {sequence}` |
| Execute macro | `rpt cmd {node} cop 6 {macro_number}` |

### `config.py` — Configuration

Loads `config.yaml` on startup and exposes all settings as typed Python properties. Uses dot-notation getters (`config.ami_host`, `config.api_key`, etc.) so the rest of the codebase never parses YAML directly.

The config path defaults to `/opt/asl3-api/config.yaml` and can be overridden by subclassing `Config` if needed for testing.

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
2. FastAPI validates the API key
3. Pydantic validates the request body (node must be numeric)
4. `ami_client.connect_node("55553", False)` is called
5. AMI command sent: `rpt cmd 637050 ilink 3 55553`
6. 8-second wait for AllStar to establish the link
7. `ami_client.get_connected_nodes()` called to verify the link is up
8. Audit log entry written
9. JSON response returned: `{"success": true, "node": "55553", "mode": "transceive"}`

The 8-second wait is an AllStar/Asterisk constraint — the ilink command initiates an IAX2 connection that takes several seconds to negotiate. This is not a bug or a timeout; it is how AllStar works.

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

## Security Model

### Authentication

Every endpoint except `/ping` requires an `X-API-Key` header. The key is a random 256-bit value stored in `config.yaml`. There is no session management, no token expiry, and no user accounts — one key controls the API. Rotate it periodically or immediately if compromised.

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
3. Add an audit log call in the route handler
4. Update `CHANGELOG.md`

### Adding webhook events

1. Add a new event method to `EventHandler` in `event_handler.py`
2. Call `_send_webhook()` with an event type string and data dict
3. Wire it into `_check_node_changes()` or a new polling loop

### Running multiple nodes

ASL3-API is designed for a single node per instance. To run it against multiple nodes on the same Pi, run multiple instances on different ports with separate config files and service units.
