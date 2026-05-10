# Architecture

## Overview

ASL3-API is a FastAPI application that runs on your AllStar node's Raspberry Pi.
It translates HTTP REST requests into Asterisk Manager Interface (AMI) commands,
listens for AMI events pushed by app_rpt, and delivers live state to clients via
Server-Sent Events (SSE).

```
Browser / App / MCP Client
        |
        | REST (control + snapshots)     SSE (live events)
        | X-API-Key header               ?api_key= query param
        |
ASL3-API  (FastAPI + uvicorn, port 8073, Raspberry Pi)
        |
        +-- node_cache.py       (allmondb, refreshed every 15 min)
        +-- ami_event_listener  (persistent event subscriber + SSE broadcast)
        +-- event_handler.py    (5s fallback poll, webhook delivery)
        |
        | panoramisk — persistent TCP connection to port 5038
        | Two streams on same connection:
        |   Action/Response  (REST-triggered commands)
        |   Unsolicited Events  (app_rpt UserEvents)
        |
Asterisk / ASL3  (app_rpt)
        |
        +-- AMI UserEvents (injected by rpt.conf [events] shell scripts)
        |     rxkeyed_true / rxkeyed_false
        |     txkeyed_true / txkeyed_false
        |
        +-- app_rpt ilink / cop / rpt commands
        |
AllStar Link network
```

## Event Flow: How Live RX/TX State Reaches a Browser

```
Operator keys mic
    |
    RF input to node radio
    |
app_rpt sets RPT_RXKEYED = 1
    |
    rpt.conf [events] fires:
    /usr/local/sbin/asl3-event-rxkeyed-true
    |
    asterisk -rx "manager userevent ASL3Event|EventName: rxkeyed_true|Node: 637050"
    |
AMI pushes UserEvent over TCP to panoramisk
    |
ami_event_listener._on_user_event() callback fires
    |
ami_event_listener.broadcast() puts event in every SSE client queue
    |
/events generator yields:
    event: node.rxkeyed
    data: {"type":"node.rxkeyed","rxkeyed":true,"node":"637050",...}
    |
Browser EventSource receives event in <100ms
```

Without the rpt.conf configuration, RX/TX state is still delivered via the
5-second fallback poll in `ami_event_listener._fallback_poll_loop()`, but with
up to 5 seconds of latency. The UserEvent path delivers sub-100ms.

## Components

### `asl_agent.py` — FastAPI Application

Entry point and HTTP layer. Responsibilities:

- Validates config on startup before binding the port
- Connects to AMI, starts node cache, event listener, event handler
- Validates API keys on every protected request (header or query param)
- Enforces per-IP rate limits on control endpoints via slowapi
- Validates all request bodies
- Delegates all AMI operations to `ami_client`
- Delegates lookups to `node_cache`
- Writes timestamped structured entries to the audit log
- Streams SSE events from `ami_event_listener` to clients

### `ami_client.py` — AMI Client

All AMI communication. Uses panoramisk for async AMI over TCP.

| Operation | AMI Command |
|-----------|-------------|
| Get node stats | `rpt stats {node}` |
| Get node variables | `rpt show variables {node}` |
| Get connected nodes | `rpt nodes {node}` |
| Connect (transceive) | `rpt cmd {node} ilink 3 {remote}` |
| Connect (monitor) | `rpt cmd {node} ilink 2 {remote}` |
| Disconnect one node | `rpt cmd {node} ilink 1 {remote}` |
| Disconnect all | `rpt cmd {node} ilink 6` |
| Send DTMF | `rpt cmd {node} senddigits {sequence}` |
| Execute macro | `rpt cmd {node} cop 6 {macro_number}` |
| COP command | `rpt cmd {node} cop {number}` |
| AMI health check | `Ping` action |

### `ami_event_listener.py` — SSE Event Broadcaster

Persistent AMI subscriber and SSE fan-out. Key behaviours:

- Registers a panoramisk callback for `UserEvent` events on startup
- Filters for `UserEvent == ASL3Event` and `EventName` field
- Broadcasts structured JSON to all subscribed SSE client queues
- Each SSE client gets its own `asyncio.Queue(maxsize=200)`
- Slow clients that fill their queue are silently dropped (does not block others)
- Fallback poll loop runs every 5s for link connect/disconnect and variable snapshots
- Reconnect-with-backoff if AMI connection is lost

### `event_handler.py` — Webhook Delivery + Fallback Poll

Complementary to `ami_event_listener`. Runs the 5-second poll loop for node
connect/disconnect detection and optionally delivers webhooks to external URLs.
Also broadcasts to SSE via `ami_event_listener` to avoid duplication.

### `config.py` — Configuration

Loads `config.yaml` on startup. Dot-notation property accessors. Validates
required fields before the server binds its port.

### `node_cache.py` — AllStar Node Database

Fetches allmondb.allstarlink.org on startup, holds 39,000+ nodes in memory,
refreshes every 15 minutes. All `/lookup` and `?enrich=true` calls are served
from this cache -- no per-request external HTTP.

## Request / Event Flows

### REST: Connect to a node

```
POST /connect  {"node": "55553"}  X-API-Key: ...
    → auth validated
    → body validated (node must be numeric)
    → ami_client.connect_node("55553", False)
    → AMI: rpt cmd 637050 ilink 3 55553
    → poll every 1s up to 12s for node to appear in rpt nodes
    → audit log entry written
    → {"success": true, "node": "55553", "mode": "transceive"}
```

### SSE: Browser receives live keyed event

```
GET /events?api_key=...
    → api_key validated
    → initial variable snapshot sent immediately
    → generator blocks on queue.get(timeout=15)
    → operator keys mic
    → rpt.conf fires shell script → AMI UserEvent → listener → broadcast
    → queue.get() returns event
    → generator yields SSE frame
    → browser EventSource fires event listener in <100ms
```

## API Endpoint Summary

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | /ping | None | Health check, AMI status |
| GET | /version | None | Version, cache status |
| GET | /status | Header | Node stats (uptime, keyups, TX time) |
| GET | /nodes | Header | Connected node list |
| GET | /variables | Header | Live app_rpt variables |
| GET | /capabilities | Header | Node and API capabilities (MCP-friendly) |
| GET | /lookup/{node} | Header | Node callsign/location from cache |
| GET | /events | Query param | SSE live event stream |
| POST | /connect | Header | Connect to remote node |
| POST | /disconnect | Header | Disconnect specific node |
| POST | /disconnect-all | Header | Disconnect all nodes |
| POST | /dtmf | Header | Send DTMF sequence |
| POST | /macro | Header | Execute rpt.conf macro |
| POST | /cop/identify | Header | Play node ID (COP 10) |
| POST | /cop/time | Header | Say current time (COP 12) |
| POST | /cop/status | Header | Say system status (COP 13) |
| POST | /cop/version | Header | Say app_rpt version (COP 14) |
| GET | /audit | Header | Recent audit log entries (structured) |

## SSE Event Reference

All events include `type`, `timestamp` (ISO 8601 UTC), `node`, `callsign`.

| Event type | Additional fields | Source |
|-----------|-------------------|--------|
| `node.rxkeyed` | `rxkeyed: bool`, `node_number: str` | AMI UserEvent (rpt.conf required) |
| `node.txkeyed` | `txkeyed: bool`, `node_number: str` | AMI UserEvent (rpt.conf required) |
| `node.variables.snapshot` | `variables: object` | Periodic poll (every 10s) |
| `link.connected` | `connected_node: str`, `mode: str` | 5s fallback poll |
| `link.disconnected` | `disconnected_node: str` | 5s fallback poll |
| `health.ami` | `connected: bool` | AMI connection monitor |

## manager.conf Requirements

The `[asl3-api]` AMI user block must include `user` in the read class list
for UserEvents to be delivered:

```ini
[asl3-api]
secret = YOUR_PASSWORD
read = system,call,reporting,command,user
write = command,reporting
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
```

Without `user` in the read list, the AMI connection works for REST commands
but UserEvents are silently filtered by Asterisk and never reach the listener.
Only the fallback 5-second poll will provide events in that case.

## nginx Proxy Note

If you place nginx in front of uvicorn, add this to your location block
to prevent SSE stream buffering:

```nginx
proxy_set_header X-Accel-Buffering no;
proxy_buffering off;
proxy_cache off;
```

Without this, nginx buffers the event stream and clients may wait seconds
or minutes before receiving events.
