# ASL3-API

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![ASL3](https://img.shields.io/badge/ASL-3-green.svg)](https://www.allstarlink.org/)

A REST API that runs on your Raspberry Pi and gives you full HTTP control over your AllStar Link node. Connect nodes, disconnect nodes, send DTMF, execute macros, and monitor status — all via clean JSON endpoints.

Built for ASL3 / Asterisk 22 on Debian (Raspberry Pi 4B tested).

---

## What This Is

AllStar Link nodes are controlled through the Asterisk Manager Interface (AMI) — a plain-text TCP protocol that is localhost-only, not documented for external use, and not friendly to consume from applications. ASL3-API wraps AMI in a FastAPI REST service that runs on your Pi alongside Asterisk. Any application that can make an HTTP request can now control your node.

**This is the backend.** It exposes no UI of its own. It is designed to be consumed by:

- The [ASL Node Panel](https://github.com/KJ5IRQ/asl-node-panel) Chrome extension
- curl / scripts
- n8n, Home Assistant, or any automation platform
- Anything else that speaks HTTP

---

## Endpoints

All endpoints except `/ping` require an `X-API-Key` header.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/ping` | Health check — no auth required |
| GET | `/status` | Node uptime, keyup count, connection summary |
| GET | `/nodes` | List of currently connected nodes with mode |
| POST | `/connect` | Connect to a remote node (transceive or monitor) |
| POST | `/disconnect` | Disconnect from a specific node |
| POST | `/disconnect-all` | Drop all active connections |
| POST | `/dtmf` | Send a DTMF sequence to your node |
| POST | `/macro` | Execute a macro defined in rpt.conf |
| GET | `/audit` | Recent command history |

Full request/response documentation is available at `http://your-pi-ip:8073/docs` once the service is running (FastAPI auto-generates it).

---

## Requirements

- AllStar Link 3 (ASL3) installed and running
- Raspberry Pi or any Debian-based Linux system
- Python 3.9 or later
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

The installer will walk you through each step, explain what it is doing, and ask for confirmation before making any changes. If you have already read the docs and just want it done:

```bash
./install.sh --auto
```

Both modes produce identical results. Guided mode explains each step before running it. Auto mode skips the explanations and only prompts for your node number, callsign, and passwords.

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for the full manual installation guide.

---

## Quick Verify

Once installed, confirm the service is up and AMI is connected:

```bash
curl http://localhost:8073/ping
```

Expected response:

```json
{
  "service": "ASL3-API",
  "node": "637050",
  "callsign": "KJ5IRQ",
  "ami_connected": true
}
```

If `ami_connected` is `false`, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

---

## Example Usage

```bash
API_KEY="your-api-key-here"
PI="http://192.168.1.x:8073"

# Check node status
curl -H "X-API-Key: $API_KEY" $PI/status

# Connect to a node (transceive)
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"node": "55553", "monitor_only": false}' $PI/connect

# Connect monitor-only
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"node": "55553", "monitor_only": true}' $PI/connect

# Disconnect a node
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"node": "55553"}' $PI/disconnect

# Drop all connections
curl -s -X POST -H "X-API-Key: $API_KEY" $PI/disconnect-all

# Send DTMF
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"sequence": "*81", "confirmed": true}' $PI/dtmf

# Execute a macro (must be defined in rpt.conf first)
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"macro_number": "1"}' $PI/macro
```

---

## Service Management

```bash
# Check status
sudo systemctl status asl3-api

# View live logs
sudo journalctl -u asl3-api -f

# Restart after config changes
sudo systemctl restart asl3-api

# Stop
sudo systemctl stop asl3-api
```

---

## Configuration

All configuration lives in `/opt/asl3-api/config.yaml`. The installer creates this file during setup. To change settings after installation, edit the file and restart the service.

```bash
nano /opt/asl3-api/config.yaml
sudo systemctl restart asl3-api
```

The `config.yaml.example` file in this repo documents every available option.

---

## Security

- All control endpoints require an API key sent in the `X-API-Key` header
- AMI is bound to localhost only and cannot be accessed externally
- The service runs as your existing node user — no new system accounts created
- `config.yaml` is set to 600 permissions (readable only by your user)
- The systemd service includes hardening flags: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`

For remote access (outside your LAN), use Tailscale or a Cloudflare Tunnel rather than exposing port 8073 directly to the internet.

See [docs/SECURITY.md](docs/SECURITY.md) for full security guidance.

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Manual installation guide |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works under the hood |
| [docs/SECURITY.md](docs/SECURITY.md) | Security hardening and best practices |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common problems and fixes |

---

## Known Issues

- Node connection verification takes approximately 8 seconds (this is an AllStar/Asterisk timing constraint, not an API bug)
- Webhook notifications are implemented but disabled by default pending real-world testing
- Uptime is reported as a raw string from `rpt stats` — formatting varies by ASL3 version

---

## Compatibility

| Component | Tested Version |
|-----------|---------------|
| ASL3 | 3.x (Asterisk 22.8.2) |
| Debian | 13 (Trixie) |
| Hardware | Raspberry Pi 4B (aarch64) |
| Python | 3.13 |

May work on earlier versions. ASL2 is not supported.

---

## Contributing

Issues and pull requests welcome. Please open an issue before submitting a PR for anything beyond a bug fix so we can discuss the approach first.

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Credits

Created by [KJ5IRQ](https://github.com/KJ5IRQ).

Built on [FastAPI](https://fastapi.tiangolo.com/), [panoramisk](https://github.com/gawel/panoramisk), and [AllStar Link](https://www.allstarlink.org/).
