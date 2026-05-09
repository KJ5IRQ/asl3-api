# Security Guide

## Threat Model

ASL3-API gives HTTP clients control over your AllStar node. The primary threats are:

- **Unauthorized node control** — someone connects or disconnects your node without permission
- **API key theft** — your key is exposed and used by an attacker
- **Network eavesdropping** — API key or commands intercepted in transit
- **Denial of service** — API flooded with requests

The mitigations below address each of these.

---

## Layer 1: API Key

Every control endpoint requires an `X-API-Key` header. Without it, the request is rejected with HTTP 401.

**Generate a strong key:**

```bash
openssl rand -base64 32
```

**Rotate your key** if you suspect it has been exposed:

```bash
# Generate new key
NEW_KEY=$(openssl rand -base64 32)

# Update config
nano /opt/asl3-api/config.yaml
# Set api.api_key to the new value

# Restart service
sudo systemctl restart asl3-api
```

Update the key in any clients (Chrome extension, scripts, etc.) after rotating.

**Key storage:**

- On the Pi: `/opt/asl3-api/config.yaml` with permissions `600` (readable only by your user)
- In clients: stored in the client's own configuration (e.g. `chrome.storage.sync` in the Chrome extension)
- Never commit `config.yaml` to git — it is in `.gitignore`

---

## Layer 2: Network Access Control

By default, ASL3-API binds to `0.0.0.0:8073` and is reachable from any device on your network. For LAN-only use, this is typically fine. For anything beyond that, restrict access.

**LAN use only — firewall example (ufw):**

```bash
# Allow only from your local network
sudo ufw allow from 192.168.1.0/24 to any port 8073

# Or allow only from a specific machine
sudo ufw allow from 192.168.1.100 to any port 8073

# Deny everything else on that port
sudo ufw deny 8073
```

**Remote access (outside your LAN):**

Do not expose port 8073 directly to the internet. Use one of:

- **Tailscale** (recommended) — zero-config mesh VPN, the API is only reachable from devices on your Tailscale network
- **Cloudflare Tunnel** — exposes the API via a Cloudflare subdomain with optional Access authentication
- **WireGuard or OpenVPN** — traditional VPN

If you use Tailscale, no firewall changes are needed — the API is unreachable from the public internet by design.

---

## Layer 3: AMI Isolation

The Asterisk Manager Interface is bound to `127.0.0.1:5038` — localhost only. This is enforced in `/etc/asterisk/manager.conf`:

```ini
[asl3-api]
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
```

This means even if someone gains access to your network, they cannot reach AMI directly. ASL3-API is the only path to it, and ASL3-API requires an API key.

**Never change AMI to bind to a non-localhost address.**

---

## Layer 4: AMI Password

The AMI password is separate from the API key. It is the credential ASL3-API uses internally to authenticate to Asterisk. It never leaves your Pi.

**Important:** Asterisk reads AMI passwords as literal strings. If you wrap the password in quotes in `manager.conf`, the quotes become part of the password. Always set it without quotes:

```ini
# Correct
secret = pFNfMvS854xTvFCDkU636R6DkGUGLASr

# Wrong — quotes are part of the password
secret = "pFNfMvS854xTvFCDkU636R6DkGUGLASr"
```

If ASL3-API is running but `ami_connected` is `false`, a quoted password mismatch is the most common cause.

---

## Layer 5: systemd Hardening

The service runs with several restrictions that limit what it can do if compromised:

| Setting | Effect |
|---------|--------|
| `NoNewPrivileges=true` | Cannot escalate to root |
| `PrivateTmp=true` | Isolated `/tmp` directory |
| `ProtectSystem=strict` | Filesystem is read-only except listed paths |
| `ProtectHome=true` | Cannot read home directories |
| `ReadWritePaths=/opt/asl3-api` | Only the install directory is writable |
| `ReadOnlyPaths=/etc/asterisk` | Can read Asterisk config but not modify it |

---

## Layer 6: Input Validation

All request inputs are validated by Pydantic before reaching the AMI client:

- Node numbers: digits only
- DTMF sequences: `0-9`, `*`, `#` only
- Macro numbers: digits only
- DTMF endpoint: `confirmed: true` required to prevent accidental sends

This prevents malformed or malicious input from being passed to AMI commands.

---

## Audit Log

Every command executed through the API is logged to `/opt/asl3-api/audit.log` with a UTC timestamp:

```
2026-05-09T12:00:00+00:00 | connect | node=55553 mode=transceive
2026-05-09T12:08:30+00:00 | disconnect | node=55553
2026-05-09T12:15:00+00:00 | status |
```

Review it periodically for unexpected activity:

```bash
tail -50 /opt/asl3-api/audit.log
```

**Log rotation** — create `/etc/logrotate.d/asl3-api`:

```
/opt/asl3-api/audit.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    create 0640 YOUR_USERNAME YOUR_USERNAME
}
```

---

## Incident Response

**If you suspect your API key is compromised:**

1. Rotate the key immediately (see Layer 1 above)
2. Review the audit log for unauthorized commands
3. Check `journalctl -u asl3-api` for unusual activity
4. Update the key in all your clients

**If unauthorized commands appear in the audit log:**

1. Stop the service: `sudo systemctl stop asl3-api`
2. Review `/opt/asl3-api/audit.log` and `sudo journalctl -u asl3-api -n 200`
3. Rotate all credentials (API key and AMI password)
4. Tighten firewall rules
5. Restart: `sudo systemctl start asl3-api`

---

## Security Checklist

**Initial setup:**
- [ ] Strong API key generated (32+ random bytes)
- [ ] Strong AMI password generated (16+ random bytes)
- [ ] `config.yaml` permissions set to 600
- [ ] AMI user locked to localhost in `manager.conf`
- [ ] Firewall configured if needed

**Ongoing:**
- [ ] Review audit log periodically
- [ ] Rotate API key every 90 days or after exposure
- [ ] Keep ASL3 and system packages updated
- [ ] Monitor `journalctl -u asl3-api` for errors

---

## Regulatory Note

Amateur radio operators using remote control capabilities are responsible for compliance with applicable regulations (FCC Part 97 in the US, or your jurisdiction's equivalent). Ensure your remote control setup meets the identification and control requirements for your license class.
