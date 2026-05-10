# ASL3-API

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![ASL3](https://img.shields.io/badge/ASL-3-green.svg)](https://www.allstarlink.org/)
[![Version](https://img.shields.io/badge/version-1.4.1-blue.svg)](CHANGELOG.md)

A REST + live event API that runs on your Raspberry Pi and gives you full HTTP control over your AllStar Link node. Connect nodes, disconnect nodes, send DTMF, execute macros, trigger COP commands, stream live keyed state in real time, and look up any node in the AllStar network — all via clean JSON endpoints.

Built for ASL3 / Asterisk 22 on Debian (Raspberry Pi 4B tested).

---

## What This Is

AllStar Link nodes are controlled through the Asterisk Manager Interface (AMI) — a plain-text TCP protocol that is localhost-only, not documented for external use, and not friendly to consume from applications. ASL3-API wraps AMI in a FastAPI REST service that runs on your Pi alongside Asterisk. Any application that can make an HTTP request can now control your node.

**v1.4 adds a live event stream.** Connect to `GET /events` and receive real-time push notifications the moment your node keys up, a link connects, or transmitter state changes — no polling required.

**This is the backend.** It exposes no UI of its own. It is designed to be consumed by:

- The [ASL Node Panel](https://github.com/KJ5IRQ/asl-node-panel) Chrome extension
- curl / scripts
- n8n, Home Assistant, or any automation platform
- MCP clients (AI agent integration — coming soon)
- Anything else that speaks HTTP or SSE

---

## What Changed in v1.4

Before v1.4, the only way to know your node's state was to ask. You sent a request, got a snapshot back. If your node keyed up a millisecond after your last request, you wouldn't know until you asked again.

v1.4 adds a persistent event stream. Connect once and the API pushes updates to you the moment state changes — receiver keyed, transmitter keyed, link connected, link disconnected. Your frontend or automation tool stays live without hammering the API with polls.

| | Before v1.4 | v1.4+ |
|---|---|---|
| Know when node keys | Poll `/variables` repeatedly | Subscribe to `/events`, receive `node.txkeyed` instantly |
| Know when link connects | Poll `/nodes` every few seconds | Receive `link.connected` event automatically |
| Browser app feel | Stale unless polling aggressively | Genuinely live |
| AMI load | N × poll interval per client | One 1-second poll regardless of client count |

---

## Endpoints

Endpoints marked **Key** require an `X-API-Key` header. The `/events` endpoint uses `?api_key=` in the URL instead (required because browser EventSource does not support custom headers). Control endpoints are rate-limited per IP (default 60/minute, configurable).

### Health

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/ping` | None | Live AMI health check — confirms API is up and Asterisk is responding |
| GET | `/version` | None | Version info, Python version, node cache stats, SSE client count |

### Node

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/status` | Key | Node uptime, keyup count, TX time, DTMF stats. Add `?raw=true` for raw AMI output. |
| GET | `/nodes` | Key | Connected nodes with mode (T/M/R). Add `?enrich=true` for callsign and location. |
| GET | `/variables` | Key | Live app_rpt variables: keyed state, TX state, link count, autopatch state |
| GET | `/capabilities` | Key | Machine-readable API and node capabilities (for MCP and client auto-configuration) |
| GET | `/lookup/{node}` | Key | Callsign, location, description for any AllStar node. Served from local cache. |

### Events (Live Stream)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/events` | `?api_key=` | Server-Sent Events stream — live node state push |

### Control

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/connect` | Key | Connect to a remote node (transceive or monitor-only) |
| POST | `/disconnect` | Key | Disconnect from a specific node |
| POST | `/disconnect-all` | Key | Drop all active connections |
| POST | `/dtmf` | Key | Send a DTMF sequence to your node |
| POST | `/macro` | Key | Execute a macro defined in rpt.conf |
| POST | `/cop/identify` | Key | Play node ID over the air (COP 10) |
| POST | `/cop/time` | Key | Say current time over the air (COP 12) |
| POST | `/cop/status` | Key | Say system status over the air (COP 13) |
| POST | `/cop/version` | Key | Say app_rpt version over the air (COP 14) |

### Admin

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/audit` | Key | Recent command history (structured JSON, not raw text) |

Full interactive documentation at `http://your-pi-ip:8073/docs` once running.

---

## Live Event Stream

Connect to `/events` and receive push notifications as things happen on your node.

```bash
# Subscribe to the live event stream
curl -N "http://your-pi:8073/events?api_key=YOUR_KEY"
```

Sample output when a node keys up and a link connects:

```
event: node.variables.snapshot
data: {"type": "node.variables.snapshot", "node": "637050", "callsign": "KJ5IRQ", "variables": {"rxkeyed": false, "txkeyed": false, "num_links": 0, ...}}

event: node.txkeyed
data: {"type": "node.txkeyed", "node": "637050", "callsign": "KJ5IRQ", "txkeyed": true, "timestamp": "2026-05-10T21:18:26Z"}

event: node.txkeyed
data: {"type": "node.txkeyed", "node": "637050", "callsign": "KJ5IRQ", "txkeyed": false, "timestamp": "2026-05-10T21:18:29Z"}

event: link.connected
data: {"type": "link.connected", "node": "637050", "connected_node": "55553", "mode": "T"}
```

### Event Types

| Event | When it fires | Key fields |
|-------|--------------|------------|
| `node.variables.snapshot` | On connect + every 10s | `variables` object with full state |
| `node.rxkeyed` | RF receiver keyed/unkeyed | `rxkeyed: bool` |
| `node.txkeyed` | Transmitter keyed/unkeyed | `txkeyed: bool` |
| `link.connected` | Remote node connects | `connected_node`, `mode` |
| `link.disconnected` | Remote node disconnects | `disconnected_node` |
| `health.ami` | AMI connection state changes | `connected: bool` |

### Using EventSource in a browser

```javascript
const es = new EventSource(`http://your-pi:8073/events?api_key=${YOUR_KEY}`);

es.addEventListener("node.txkeyed", e => {
    const data = JSON.parse(e.data);
    console.log("TX keyed:", data.txkeyed);
});

es.addEventListener("link.connected", e => {
    const data = JSON.parse(e.data);
    console.log("Link connected:", data.connected_node);
});
```

> **Note for nginx users:** Add `proxy_set_header X-Accel-Buffering no;` to your location block or events will be buffered and not delivered in real time.

---

## Requirements

- AllStar Link 3 (ASL3) installed and running
- Raspberry Pi or any Debian-based Linux system
- Python 3.10 or later
- sudo access for installation

Tested on: ASL3 / Asterisk 22.8.2 / Debian 13 (Trixie) / Raspberry Pi 4B (aarch64)

---

## Installation

Clone the repo onto your Pi and run the installer:

```bash
git clone https://github.com/KJ5IRQ/asl3-api.git
cd asl3-api
chmod +x install.sh
./install.sh
```

The installer walks you through each step, explains what it is doing, and asks for confirmation before making any changes. If you have already read the docs and just want it done:

```bash
./install.sh --auto
```

Both modes produce identical results. Guided mode explains each step. Auto mode only prompts for your node number, callsign, and passwords.

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for the full manual installation guide, including the optional rpt.conf configuration for sub-second RX/TX keyed events.

---

## Upgrading from v1.3.x

```bash
cd ~/asl3-api
git pull

cd /opt/asl3-api
source venv/bin/activate
pip install -r ~/asl3-api/requirements.txt
deactivate

cp ~/asl3-api/ami_event_listener.py    ~/asl3-api/asl_agent.py    ~/asl3-api/config.py    ~/asl3-api/event_handler.py    /opt/asl3-api/
```

Then add the `events:` block to `/opt/asl3-api/config.yaml`:

```yaml
events:
  enabled: true
  keepalive_interval: 15
  snapshot_interval: 10
```

Add `user` to the read line in `/etc/asterisk/manager.conf` under your `[asl3-api]` block:

```ini
read = system,call,reporting,command,user
```

Then reload and restart:

```bash
sudo asterisk -rx "manager reload"
sudo systemctl restart asl3-api
```

---

## Quick Verify

```bash
curl http://localhost:8073/ping
```

```json
{
  "service": "ASL3-API",
  "node": "637050",
  "callsign": "KJ5IRQ",
  "ami_connected": true,
  "sse_clients": 0
}
```

Then verify the event stream:

```bash
API_KEY=$(grep "api_key" /opt/asl3-api/config.yaml | awk '{print $2}' | tr -d '"')
curl -N "http://localhost:8073/events?api_key=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$API_KEY'))")"
```

You should see an immediate `node.variables.snapshot` event. Key your radio — you should see `node.txkeyed` within 1-2 seconds.

---

## Example Usage

```bash
API_KEY="your-api-key-here"
PI="http://192.168.1.x:8073"

# Health (no auth)
curl $PI/ping
curl $PI/version

# Live event stream
curl -N "$PI/events?api_key=$API_KEY"

# Node capabilities (machine-readable)
curl -H "X-API-Key: $API_KEY" $PI/capabilities

# Node status and variables
curl -H "X-API-Key: $API_KEY" $PI/status
curl -H "X-API-Key: $API_KEY" $PI/variables

# Connected nodes with enrichment
curl -H "X-API-Key: $API_KEY" "$PI/nodes?enrich=true"

# Look up any AllStar node
curl -H "X-API-Key: $API_KEY" $PI/lookup/55553

# Connect / disconnect
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json"   -d '{"node": "55553", "monitor_only": false}' $PI/connect

curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json"   -d '{"node": "55553"}' $PI/disconnect

curl -s -X POST -H "X-API-Key: $API_KEY" $PI/disconnect-all

# DTMF (confirmed must be true)
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json"   -d '{"sequence": "*81", "confirmed": true}' $PI/dtmf

# COP commands
curl -s -X POST -H "X-API-Key: $API_KEY" $PI/cop/identify
curl -s -X POST -H "X-API-Key: $API_KEY" $PI/cop/time

# Audit log
curl -H "X-API-Key: $API_KEY" "$PI/audit?lines=20"
```

---

## Key Features

**Live SSE event stream** — Subscribe to `GET /events` and receive real-time push events for keyed state, link changes, and variable snapshots. Sub-2-second latency. No polling required from clients.

**Node database cache** — On startup, fetches the AllStar node database (~40,000 nodes) into memory. `/lookup` calls are instant. Refreshes every 15 minutes.

**Guaranteed response schemas** — All response fields are always present. Missing data is `null`, never absent. Consistent shapes make client code simpler.

**Rate limiting** — Control endpoints are rate-limited per IP. Default 60/minute, configurable.

**Startup validation** — Required config fields checked before the service binds. Clear error messages on misconfiguration.

**Structured audit log** — Every command logged with timestamp, command name, and details as structured JSON fields.

---

## Service Management

```bash
sudo systemctl status asl3-api
sudo journalctl -u asl3-api -f
sudo systemctl restart asl3-api
sudo systemctl stop asl3-api
```

---

## Configuration

All configuration in `/opt/asl3-api/config.yaml`. Edit and restart to apply changes.

The `config.yaml.example` file documents every available option.

---

## Security

- All control endpoints require `X-API-Key` header
- `/events` uses `?api_key=` query parameter (EventSource browser limitation)
- AMI bound to localhost only
- Runs as existing node user — no new system accounts
- `config.yaml` set to 600 permissions
- systemd hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`
- Config validated on startup — fails fast with clear error

For remote access, use Tailscale or Cloudflare Tunnel rather than exposing port 8073 directly.

See [docs/SECURITY.md](docs/SECURITY.md).

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Full installation and upgrade guide |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works — REST, event layer, AMI |
| [docs/SECURITY.md](docs/SECURITY.md) | Security hardening and remote access |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common problems and fixes |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

---

## Compatibility

| Component | Tested Version |
|-----------|---------------|
| ASL3 | 3.x (Asterisk 22.8.2) |
| Debian | 13 (Trixie) |
| Hardware | Raspberry Pi 4B (aarch64) |
| Python | 3.13 |

ASL2 is not supported.

---

## Contributing

Issues and pull requests welcome. Open an issue before submitting a PR for anything beyond a bug fix.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Credits

Created by [KJ5IRQ](https://github.com/KJ5IRQ).

Built on [FastAPI](https://fastapi.tiangolo.com/), [panoramisk](https://github.com/gawel/panoramisk), and [AllStar Link](https://www.allstarlink.org/).
