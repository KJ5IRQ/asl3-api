# Changelog

All notable changes to ASL3-API will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/KJ5IRQ/asl3-api/releases/tag/v1.0.0
