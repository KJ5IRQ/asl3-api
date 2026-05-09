# Installation Guide

This guide covers manual installation of ASL3-API. If you want the installer to handle everything for you, use `install.sh` instead — see the [README](../README.md).

---

## Prerequisites

Before starting, confirm you have:

- AllStar Link 3 installed and Asterisk running (`sudo systemctl status asterisk`)
- Python 3.10 or later (`python3 --version`)
- `python3-venv` installed (`sudo apt install python3-venv` if missing)
- sudo access on your node

This guide was written for Debian 13 (Trixie) on a Raspberry Pi 4B. The steps are the same on any Debian-based system running ASL3.

---

## Part 1: Install the Files

SSH into your Pi and run:

```bash
# Clone the repo
git clone https://github.com/KJ5IRQ/asl3-api.git
cd asl3-api

# Create the install directory
sudo mkdir -p /opt/asl3-api
sudo chown $(whoami):$(whoami) /opt/asl3-api

# Copy source files
cp asl_agent.py ami_client.py config.py event_handler.py node_cache.py requirements.txt /opt/asl3-api/
cp config.yaml.example /opt/asl3-api/
```

---

## Part 2: Python Virtual Environment

A virtual environment keeps ASL3-API's dependencies isolated from the rest of your system:

```bash
cd /opt/asl3-api
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

---

## Part 3: Configuration

Copy the example config and edit it:

```bash
cp /opt/asl3-api/config.yaml.example /opt/asl3-api/config.yaml
nano /opt/asl3-api/config.yaml
```

Fill in these required values:

```yaml
ami:
  password: ""      # Generate with: openssl rand -base64 16

node:
  number: ""        # Your AllStar node number, e.g. "637050"
  callsign: ""      # Your callsign, e.g. "KJ5IRQ"

api:
  api_key: ""       # Generate with: openssl rand -base64 32
```

Generate the passwords:

```bash
# AMI password (shorter is fine for a localhost-only service)
openssl rand -base64 16

# API key (longer — this is what your clients authenticate with)
openssl rand -base64 32
```

Lock down the config file so only your user can read it:

```bash
chmod 600 /opt/asl3-api/config.yaml
```

---

## Part 4: Configure Asterisk AMI

ASL3-API connects to Asterisk through the Asterisk Manager Interface. You need to add a dedicated AMI user for it.

Open `/etc/asterisk/manager.conf`:

```bash
sudo nano /etc/asterisk/manager.conf
```

First, make sure the `[general]` section has AMI enabled. Look for or add:

```ini
[general]
enabled = yes
port = 5038
bindaddr = 127.0.0.1
```

Then add the ASL3-API user block at the bottom of the file:

```ini
[asl3-api]
secret = YOUR_AMI_PASSWORD_HERE
read = system,call,reporting,command
write = command,reporting
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
```

Replace `YOUR_AMI_PASSWORD_HERE` with the AMI password you put in `config.yaml`. **Do not wrap it in quotes** — Asterisk reads the value literally, and quotes will be treated as part of the password.

Reload the Asterisk manager:

```bash
sudo asterisk -rx "manager reload"
```

Verify the user was loaded:

```bash
sudo asterisk -rx "manager show user asl3-api"
```

---

## Part 5: Test Before Installing the Service

Before installing the systemd service, test that the API starts correctly:

```bash
cd /opt/asl3-api
source venv/bin/activate
python3 asl_agent.py
```

In another terminal, hit the health check:

```bash
curl http://localhost:8073/ping
```

You should see:

```json
{
  "service": "ASL3-API",
  "node": "your-node-number",
  "callsign": "your-callsign",
  "ami_connected": true
}
```

If `ami_connected` is `false`, the AMI credentials do not match. Double-check that the password in `config.yaml` and in `manager.conf` are identical, with no surrounding quotes in either file.

Press `Ctrl+C` to stop the test server.

---

## Part 6: Install the systemd Service

Open `asl3-api.service` from the repo and replace `INSTALL_USER` with your username:

```bash
# Find out your username
whoami

# Edit the service file
nano asl3-api.service
# Change both User= and Group= lines from INSTALL_USER to your actual username
```

Install and enable it:

```bash
sudo cp asl3-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable asl3-api
sudo systemctl start asl3-api
```

Check it started cleanly:

```bash
sudo systemctl status asl3-api
```

You should see `Active: active (running)` and a log line showing the node number and callsign.

---

## Part 7: Verify

```bash
# Health check (no auth needed)
curl http://localhost:8073/ping

# Status (requires API key)
curl -H "X-API-Key: YOUR_API_KEY" http://localhost:8073/status
```

---

## Accessing from Your LAN

By default the API binds to `0.0.0.0:8073`, so it is accessible from any device on your network at `http://your-pi-ip:8073`.

To find your Pi's IP:

```bash
hostname -I
```

For remote access outside your LAN, see [SECURITY.md](SECURITY.md) — do not expose port 8073 directly to the internet.

---

## Upgrading

To upgrade to a newer version:

```bash
cd ~/asl3-api          # wherever you cloned the repo
git pull
cp asl_agent.py ami_client.py config.py event_handler.py node_cache.py requirements.txt /opt/asl3-api/

cd /opt/asl3-api
source venv/bin/activate
pip install -r requirements.txt
deactivate

sudo systemctl restart asl3-api
```

Check [CHANGELOG.md](../CHANGELOG.md) for breaking changes before upgrading.

---

## Uninstalling

```bash
sudo systemctl stop asl3-api
sudo systemctl disable asl3-api
sudo rm /etc/systemd/system/asl3-api.service
sudo systemctl daemon-reload
sudo rm -rf /opt/asl3-api
```

Remove the `[asl3-api]` block from `/etc/asterisk/manager.conf` and reload:

```bash
sudo nano /etc/asterisk/manager.conf
sudo asterisk -rx "manager reload"
```
