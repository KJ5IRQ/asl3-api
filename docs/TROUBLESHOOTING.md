# Troubleshooting

## Quick Diagnostics

Run these first — they cover most issues:

```bash
# Is the service running?
sudo systemctl status asl3-api

# What does the log say?
sudo journalctl -u asl3-api -n 50 --no-pager

# Is the API responding?
curl http://localhost:8073/ping

# Is AMI connected?
sudo asterisk -rx "manager show connected"
```

---

## Service Won't Start

**Symptom:** `sudo systemctl start asl3-api` fails or the service immediately exits.

**Check the logs first:**

```bash
sudo journalctl -u asl3-api -n 30 --no-pager
```

**Common causes:**

**Config validation failed:**
```
CRITICAL asl_agent ASL3-API config validation failed:
  - node.number is required but not set
  - api.api_key is required but not set
```
Fix: Open `/opt/asl3-api/config.yaml` and fill in the missing fields. All four of these must be set: `node.number`, `node.callsign`, `ami.password`, `api.api_key`.
```bash
nano /opt/asl3-api/config.yaml
sudo systemctl restart asl3-api
```

**Config file not found:**
```
FileNotFoundError: Config file not found: /opt/asl3-api/config.yaml
```
Fix: Copy the example and fill it in:
```bash
cp /opt/asl3-api/config.yaml.example /opt/asl3-api/config.yaml
nano /opt/asl3-api/config.yaml
```

**Python packages missing:**
```
ModuleNotFoundError: No module named 'fastapi'
```
Fix: Install into the venv:
```bash
cd /opt/asl3-api
source venv/bin/activate
pip install -r requirements.txt
deactivate
```

**Port already in use:**
```
ERROR: [Errno 98] Address already in use
```
Fix: Find what is using port 8073 and stop it:
```bash
sudo ss -tlnp | grep 8073
```

**Wrong user in service file:**
The service file has a `User=` line. If that user does not exist on your system, the service won't start. Check it:
```bash
cat /etc/systemd/system/asl3-api.service | grep User
```
Fix: Edit the service file to match your actual username:
```bash
sudo nano /etc/systemd/system/asl3-api.service
sudo systemctl daemon-reload
sudo systemctl restart asl3-api
```

---

## AMI Not Connecting

**Symptom:** Service starts but `/ping` returns `"ami_connected": false`. The log shows repeated connection attempts.

**This is almost always a password mismatch.**

The most common cause: the password in `manager.conf` has quotes around it, making the actual password `"yourpassword"` including the quote characters, while `config.yaml` has `yourpassword` without quotes.

**Check manager.conf:**
```bash
sudo grep -A5 "\[asl3-api\]" /etc/asterisk/manager.conf
```

The `secret` line must have no quotes:
```ini
# Correct
secret = yourpassword

# Wrong
secret = "yourpassword"
```

**Fix quoted password:**
```bash
sudo sed -i 's/secret = "\(.*\)"/secret = \1/' /etc/asterisk/manager.conf
sudo asterisk -rx "manager reload"
sudo systemctl restart asl3-api
```

**Verify the AMI user loaded:**
```bash
sudo asterisk -rx "manager show user asl3-api"
```

If it says `No such manager user`, the block was not added correctly or the reload failed. Re-add it and reload:
```bash
sudo asterisk -rx "manager reload"
```

**Verify AMI is enabled:**
```bash
sudo grep -A5 "\[general\]" /etc/asterisk/manager.conf
```
You should see `enabled = yes`. If not, add it and reload.

**Check Asterisk is running:**
```bash
sudo systemctl status asterisk
```

If Asterisk is not running, ASL3-API cannot connect to AMI regardless of credentials.

---

## API Returns 401 Unauthorized

**Symptom:** Requests to protected endpoints return HTTP 401.

**Cause:** The `X-API-Key` header is missing or does not match what is in `config.yaml`.

Check your API key:
```bash
grep api_key /opt/asl3-api/config.yaml
```

Make sure your client is sending it as `X-API-Key` (note the capitalization — HTTP headers are case-insensitive but some clients are finicky). Test with curl:

```bash
curl -H "X-API-Key: YOUR_KEY_HERE" http://localhost:8073/status
```

---

## Connect/Disconnect Commands Appear to Succeed but Node Doesn't Change State

**Symptom:** `/connect` returns `{"success": true}` but the node is not actually connected in Allmon3 or AllScan.

**Check the node number:**
Verify the node number in your request matches a real, reachable AllStar node. The 55553 parrot node is a good test target — it is always online and echoes audio back.

**Check your node number in config.yaml:**
```bash
grep number /opt/asl3-api/config.yaml
```

If this is wrong, ASL3-API is issuing commands to the wrong node.

**Check AMI permissions:**
The AMI user needs write access to the `command` context:
```bash
sudo grep -A10 "\[asl3-api\]" /etc/asterisk/manager.conf
```
You should see `write = command,reporting`.

**Check Asterisk logs:**
```bash
sudo tail -50 /var/log/asterisk/messages
```

---

## Connect Takes Much Longer Than 8 Seconds

The 8-second wait in `/connect` is a fixed delay to allow AllStar time to negotiate the IAX2 link. If the remote node is slow to respond or unreachable, the command will complete after 8 seconds but report failure (the verification step finds the node is not connected).

This is normal behavior. The 8 seconds is the wait, not a timeout — the API will always respond after approximately 8 seconds regardless of whether the connection succeeded.

---

## DTMF Endpoint Returns 400

**Symptom:** POST to `/dtmf` returns HTTP 400.

**Cause 1:** `confirmed` is not set to `true`:
```json
{"sequence": "#", "confirmed": true}
```

**Cause 2:** The sequence contains invalid characters. Only `0-9`, `*`, and `#` are accepted.

---

## Macro Endpoint Has No Effect

**Symptom:** `/macro` returns `{"success": true}` but nothing happens on the node.

Macros must be defined in `/etc/asterisk/rpt.conf` before they can be executed. If macro 1 is not defined, the command fires but Asterisk silently ignores it.

Check your rpt.conf for a `[macro]` section or `macro1=` entries. See the [ASL3 Macros documentation](https://allstarlink.github.io/adv-topics/macros/) for how to define macros.

---

## Audit Log is Empty or Missing

The audit log is created at first write. If no commands have been executed, the file won't exist yet. That is normal.

If you are executing commands and the log is not being written, check permissions:
```bash
ls -la /opt/asl3-api/audit.log
```

The file should be owned by your user. If it is owned by root (from a previous sudo run), fix it:
```bash
sudo chown $(whoami):$(whoami) /opt/asl3-api/audit.log
```

---

## Service Starts but Crashes After a Few Minutes

Check for a pattern in the logs:
```bash
sudo journalctl -u asl3-api --since "10 minutes ago"
```

A common cause is AMI connection drops. panoramisk will retry, but if Asterisk is restarting or unstable, the retries can eventually fail hard. The service is configured to restart automatically (`Restart=always`), so brief crashes are self-healing.

If the service is crash-looping, look for a recurring error in the log and address the root cause.

---

## Getting Help

If you have worked through this guide and the issue persists:

1. Collect the relevant log output: `sudo journalctl -u asl3-api -n 100 --no-pager`
2. Note your ASL3 version: `sudo asterisk -rx "core show version"`
3. Note your OS and hardware: `uname -a` and `cat /etc/os-release`
4. Open an issue at [https://github.com/KJ5IRQ/asl3-api/issues](https://github.com/KJ5IRQ/asl3-api/issues) with the above information
