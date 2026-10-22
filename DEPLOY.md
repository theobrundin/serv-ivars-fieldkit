# DEPLOY.md — Pi Unit Deployment Runbook

This is the **only** document Erik needs to deploy a unit. Follow it top to bottom, do not skip steps, do not improvise. If anything goes wrong, stop and ping Theo — do not try to "fix it" by guessing.

---

## What you should have before starting

- A Raspberry Pi 5 that has already gone through `Serv_Build_Instructions.pdf` and is at **Checkpoint 4** (cellular working, plate reader v1 was running). This is the May 2026 build doc.
- The new `serv-pi` repository cloned somewhere on the Pi (instructions below).
- A config bundle from Theo: a small JSON file containing `device_id`, `api_key`, and `location_id` for this specific physical unit. **Do not share these credentials.**

You should be SSH'd into the Pi as the `serv` user.

---

## Step 1 — Stop the old service (one-time)

The Pi shipped with a v1 plate reader running. We're replacing it with v2.

```bash
sudo systemctl stop plate_reader.service 2>/dev/null || true
sudo systemctl disable plate_reader.service 2>/dev/null || true
```

(These are no-ops if v1 isn't installed. That's fine.)

---

## Step 2 — Clone the repo

```bash
cd ~
git clone https://github.com/SERV-ORG/serv-pi.git
cd serv-pi
git log -1 --oneline   # note the commit; Theo wants this in your handoff
```

If you don't have GitHub access yet, ping Theo to add your SSH key to the org.

---

## Step 3 — Run the installer

**Before running:** if this repo was downloaded/unzipped instead of git-cloned, or was ever opened in a Windows editor, the shell scripts may have Windows line endings or missing execute bits. Both cause `sudo ./scripts/install.sh` to fail with a confusing `command not found` error. The safe form is:

```bash
sudo bash scripts/install.sh
```

Using `bash scripts/install.sh` (instead of `./scripts/install.sh`) does two things: it works even if the script doesn't have the execute bit set, and installer step 0 will then normalize line endings and permissions on all the *other* scripts so `cellular-connect.sh`, `network-priority.sh`, and `bringup.sh` don't hit the same issue later.

This takes 2-5 minutes. It will:

- Normalize line endings and permissions on all shipped scripts
- Install apt and pip dependencies
- Copy the package to `/home/serv/plate_reader/`
- Install the systemd units `plate-reader.service`, `cellular-connect.service`, and `network-priority.service`
- Drop a placeholder `config.json` (you'll fill it in next)
- Enable and start `cellular-connect.service` and `network-priority.service`
- **Not** start `plate-reader.service` yet

If the installer prints an error, stop. Ping Theo with the exact error text.

**Sanity check that the service was actually registered before moving on:**

```bash
systemctl list-unit-files | grep plate-reader
```

You should see `plate-reader.service` in the output. If it isn't there, the installer didn't complete — scroll up in its output for the first error, don't proceed.

---

## Step 4 — Configure this unit

Open the config:

```bash
sudo nano /home/serv/plate_reader/config.json
```

Replace the three placeholder values:

```jsonc
{
  "device_id": "REPLACE_WITH_serv-unit-XXX",                  // <-- from Theo
  "api_key": "REPLACE_WITH_64_CHAR_HEX_KEY", // <-- from Theo (64 hex chars)
  "location_id": "REPLACE_WITH_LOCATION_ID",           // <-- from Theo
  ...
}
```

Save with Ctrl+X, Y, Enter.

Lock down the permissions (in case you saved as root):

```bash
sudo chown serv:serv /home/serv/plate_reader/config.json
sudo chmod 600 /home/serv/plate_reader/config.json
```

---

## Step 5 — Prepare the selected ALPR engine on Wi-Fi

The template selects ultimateALPR. The installer pins and builds that SDK on
the Pi. Run installation on Wi-Fi so the SDK checkout/build does not consume
the field SIM allowance.

If a unit deliberately selects `fast_alpr`, its first model load downloads
weights. Pre-warm that optional engine on Wi-Fi:

```bash
python3 -c "
import sys
sys.path.insert(0, '/home/serv/plate_reader')
from fast_alpr import ALPR
alpr = ALPR()
print('alpr model cached')
"
```

This prints `alpr model cached` when done. Skip this command on an
ultimateALPR unit; the fast-alpr and ONNX packages are intentionally not
installed there.

---

## Step 6 — Run the bringup test

**Before running bringup**, do a quick standalone camera check — this surfaces ribbon cable / overlay issues immediately with a clear error, instead of buried in the bringup output:

```bash
rpicam-hello --list-cameras
```

You should see a line containing `imx296` and a resolution like `1456x1088`. If you don't:

1. Reseat the CSI ribbon cable at **both ends** (Pi side and camera board side). Pull the black latch up, seat the ribbon fully and straight, press the latch back down.
2. Confirm you're using the CSI port your config expects.
3. Check that `dtoverlay=imx296` exists in `/boot/firmware/config.txt`. If not, add it and `sudo reboot`.
4. Rerun `rpicam-hello --list-cameras`.

If the camera check passes, run the full bringup:

```bash
cd ~/serv-pi
./scripts/bringup.sh
```

You should see a long colored report. The bottom line says one of:

- **`ALL GREEN — unit looks ready.`** → continue.
- **`OK with warnings`** → review the WARNs. Send them to Theo for sign-off before continuing.
- **`NOT READY — fix the FAIL items above before shipping.`** → stop. Fix the failed items.

**Save the output:**

```bash
./scripts/bringup.sh > ~/bringup-$(date +%Y%m%d-%H%M).txt 2>&1
```

You'll send this file to Theo as part of the handoff.

---

## Step 7 — Start the service

```bash
sudo systemctl start plate-reader.service
```

Verify it's running:

```bash
sudo systemctl status plate-reader.service
```

You want to see `Active: active (running)`. If it's `failed`, run:

```bash
sudo journalctl -u plate-reader -n 100 --no-pager
```

and send Theo the output.

---

## Step 8 — Watch the live log for 2 minutes

```bash
sudo journalctl -u plate-reader -f
```

Within ~30 seconds you should see lines like:

```
serv-pi starting
config loaded: device_id=REPLACE_WITH_serv-unit-XXX location_id=REPLACE_WITH_LOCATION_ID ...
hasher self-test passed
camera started: 1456x1088 @ ~5fps
ultimateALPR engine initialised (licensed=True)
license verified: unmasked output confirmed
auth preflight OK (status 200)
heartbeat thread started; interval=120s
boot complete; entering detection loop
FRAME_PIPELINE_HEALTHY: 3 successful camera + inference frames
```

Then it'll be quiet (no plates detected yet). That's correct.

Press Ctrl+C to stop watching the log — the service keeps running.

---

## Step 9 — Real-plate test

Point the camera at a real license plate from 5-15 meters. Wait 5-10 seconds. You should see a line in the log like:

```
CONFIRMED hash=9d3a8db14b91 det=0.91 ocr=0.99 frames=2 inference=1130ms
```

If you see it: success. The plate was read, voted, hashed, and sent to the cloud.

If you see `FAILED outcome=low_confidence` repeatedly: the camera focus or detection zone needs adjustment. Pull the zone preview image:

```bash
ls -la /home/serv/plate_reader/zone_preview.jpg
```

Then `scp` it to your laptop and look at where the red box falls relative to where plates actually appear. Ping Theo with the image.

---

## Step 10 — Reboot test

```bash
sudo reboot
```

Wait 60 seconds. SSH back in. Confirm the service came back up on its own:

```bash
sudo systemctl status plate-reader.service
```

`Active: active (running)` again. This proves the unit will recover from a power cycle in the field.

---

## Step 11 — Handoff

Send Theo:

1. The bringup report file (`~/bringup-YYYYMMDD-HHMM.txt`)
2. The Tailscale IP of this unit: `tailscale ip -4`
3. The zone preview image (`/home/serv/plate_reader/zone_preview.jpg`)
4. A photo of the camera aim (just the back of your screen showing the preview is fine)
5. The git commit you deployed from (the output of `git log -1 --oneline` from Step 2)
6. Any WARNs you saw and your judgment on whether they matter

Theo will sign off, then you ship the unit to the pilot site.

---

## Troubleshooting

### `sudo: ./scripts/install.sh: command not found`

Either the script lost its execute bit during file transfer, or it has Windows (CRLF) line endings that break the shebang. Fix both defensively:

```bash
cd ~/serv-pi
sudo apt install -y dos2unix
dos2unix scripts/*.sh
chmod +x scripts/*.sh
sudo bash scripts/install.sh
```

Prefer `sudo bash scripts/install.sh` over `sudo ./scripts/install.sh` — it works regardless of exec bit or shebang state.

### `cp: cannot create regular file '/home/serv/plate_reader/config.json': No such file or directory`

The runtime directory `/home/serv/plate_reader/` doesn't exist yet, which means `install.sh` never completed successfully. Go back to Step 3, resolve the actual installer failure, then retry — the installer creates this directory as part of its work.

### `Failed to start plate-reader.service: Unit plate-reader.service not found.`

Same root cause as above: `install.sh` didn't finish, so the unit file was never installed. Verify:

```bash
systemctl list-unit-files | grep plate-reader
```

If empty, rerun `sudo bash scripts/install.sh` and fix whatever error it prints.

### "config validation failed" in the log

The service starts, errors on config, and the unit goes into a backoff loop. Read the journal:

```bash
sudo journalctl -u plate-reader -n 50 --no-pager | grep -i config
```

The error message tells you which field is wrong. Edit the config, then:

```bash
sudo systemctl restart plate-reader.service
```

### "auth preflight failed with 401"

The `api_key` in config.json doesn't match what's in the cloud Devices table. Re-check the credentials Theo sent. If they look right, the device may have been revoked on the cloud side — ping Theo.

### "fast_alpr not installed" or "picamera2 not installed"

Re-run the installer:

```bash
sudo ./scripts/install.sh
```

If that doesn't fix it, the OS image may be missing packages — ping Theo.

### Crash loop (service keeps restarting)

After 5 failed restarts in 5 minutes, systemd will stop trying. The service will sit in a `failed` state.

```bash
sudo journalctl -u plate-reader -n 100 --no-pager
sudo systemctl reset-failed plate-reader.service
sudo systemctl start plate-reader.service
```

If it crashes again immediately, do not just keep restarting — read the log and fix the cause first.

### Lost SSH access

If you can no longer reach the unit by `ssh serv@servCam.local` but Tailscale is up, use the Tailscale IP:

```bash
ssh serv@<tailscale-ip-here>
```

If Tailscale is down too, you'll need physical access. This is why every shipped unit must have **both** SSH paths verified during bringup.

---

## Emergency rollback (if everything is on fire)

To temporarily go back to a known-good state:

```bash
sudo systemctl stop plate-reader.service
sudo systemctl disable plate-reader.service
```

The unit will stop sending data but will still be reachable for diagnosis. Then ping Theo.
