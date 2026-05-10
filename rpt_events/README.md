# rpt_events — Shell Scripts for app_rpt Event Integration

These scripts are called by the app_rpt [events] subsystem in `rpt.conf` when
RX/TX keyed state changes. They inject AMI UserEvents that ASL3-API's event
listener consumes to deliver real-time SSE updates to connected clients.

## Installation

Run the installer (`install.sh`) which handles this automatically, or do it manually:

### 1. Edit the scripts

Replace `__NODE_NUMBER__` in each script with your actual node number (e.g. `637050`):

```bash
for f in /opt/asl3-api/rpt_events/asl3-event-*; do
    sudo sed -i 's/__NODE_NUMBER__/637050/g' "$f"
done
```

### 2. Copy scripts to /usr/local/sbin and make executable

```bash
sudo cp /opt/asl3-api/rpt_events/asl3-event-* /usr/local/sbin/
sudo chmod +x /usr/local/sbin/asl3-event-*
```

### 3. Add to rpt.conf

Under your node number stanza, add `events = events_NODE` (replacing NODE with
your node number). Then add the events stanza at the bottom of rpt.conf:

```ini
; Under your node stanza (e.g. [637050]):
events = events_637050

; New stanza at the bottom of rpt.conf:
[events_637050]
/usr/local/sbin/asl3-event-rxkeyed-true  = s|t|RPT_RXKEYED
/usr/local/sbin/asl3-event-rxkeyed-false = s|f|RPT_RXKEYED
/usr/local/sbin/asl3-event-txkeyed-true  = s|t|RPT_TXKEYED
/usr/local/sbin/asl3-event-txkeyed-false = s|f|RPT_TXKEYED
```

### 4. Restart Asterisk

```bash
sudo systemctl restart asterisk
```

### 5. Verify

Key your mic and watch the ASL3-API log for AMI UserEvent receipts:

```bash
sudo journalctl -u asl3-api -f
```

You should see lines like:
```
ami_event_listener INFO AMI UserEvent received: rxkeyed_true node=637050
```

If you only see `link.connected` and `node.variables.snapshot` events but not
`node.rxkeyed` or `node.txkeyed`, the rpt.conf events are not configured.

## Why This Approach

app_rpt does not natively emit AMI events for RX/TX keyed state changes.
The [events] subsystem is the only supported mechanism to react to those
transitions. These scripts bridge the gap by converting rpt.conf event
triggers into AMI UserEvents that ASL3-API can subscribe to.

The AMI user in manager.conf must have `read` permissions that include `user`
for UserEvents to be delivered. The installer adds this automatically.
