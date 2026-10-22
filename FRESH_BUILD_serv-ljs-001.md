# FRESH_BUILD_serv-ljs-001.md — Building the replacement Louisville unit

> Historical unit build log, not the current deployment procedure. In
> particular, do not copy a credential-bearing `config.json` inside a general
> code bundle. Use `DEPLOY.md` and `SCP_DEPLOY.md` for current instructions.

This is the **full build** for a brand-new Pi that will **replace** the existing
Louisville unit (`REPLACE_WITH_serv-unit-XXX` at `REPLACE_WITH_LOCATION_ID`). The new Pi keeps the
**same credentials** as the old one, because it's a straight hardware swap.

> ⚠️ **CRITICAL — read before you start:**
> The new Pi uses the **same `device_id` (REPLACE_WITH_serv-unit-XXX)** as the unit currently
> running in Louisville. **Two machines with the same device_id must NEVER run at
> the same time** — they will corrupt each other's data in the backend.
>
> Rule: the **old unit stays powered OFF** while you build and test the new one,
> OR you only power off the old one at the very moment you physically swap them
> at the restaurant. Never both online at once.

This build has two big phases:
- **Phase A — Hardware + OS + Tailscale** (follow the Assembly Manual)
- **Phase B — Install the v2 software** (this doc; replaces the Manual's old
  sections 6 & 7)

---

## Phase A — Hardware, OS, and remote access

Follow **`Serv_Technologies_Assembly_Manual.docx`**, sections 2–5, with **one
change** in the SD-card step:

### SD card imaging (Manual section 3) — with our naming

In Raspberry Pi Imager, in the gear/settings screen, set:
- **Hostname:** `serv-ljs-001`  ← *(the Manual says `servCam`; use this instead)*
- **Enable SSH:** checked
- **Username:** `serv`
- **Password:** set a unique password in Raspberry Pi Imager and store it in
  the team's password manager; never put it in this repository
- **Configure WiFi:** your network name + password (needed for setup)
- Do **not** enable Raspberry Pi Connect (we use Tailscale)

Because the hostname is `serv-ljs-001`, everywhere the Manual says
`ssh serv@servCam.local`, you instead use:

```
ssh serv@serv-ljs-001.local
```

Then complete the Manual's **section 4 (physical assembly)** and **section 5,
Steps 1–7** (system update, **Tailscale install + sign-in**, camera libs, ALPR
libs, modem tools, and the camera/modem verification checks).

**Stop when you reach the Manual's Section 6 ("Plate Reader Script").**
Do **not** do Manual sections 6 or 7 — that's the old v1 software. Come back
here for Phase B instead.

At this point you should have: a booted Pi named `serv-ljs-001`, reachable over
Tailscale, with the camera detected (`imx296`) and the modem detected (Telit).

---

## Phase B — Install the v2 software

All of Phase B is done from **your Mac**. Some steps run on the Mac, some on the
Pi (over SSH). Each is labeled.

### B1 — (Mac) Put the code folder somewhere easy

Unzip the `serv-pi` bundle Theo gave you and move it to your Desktop, so the
path is exactly `~/Desktop/serv-pi`. Inside it you should see a `plate_reader`
folder, a `scripts` folder, and a `config.json` file.

### B2 — (Mac) Copy the whole bundle to the Pi

Open Terminal and run:

```bash
cd ~/Desktop/serv-pi
ls    # should list: plate_reader  scripts  config.json  (and some docs)
scp -r ./plate_reader ./scripts ./config.json ./config.example.json ./requirements.txt ./systemd serv@serv-ljs-001.local:~/serv-pi-staging/
```

- If it asks for a password, use the unique SSH password configured in
  Raspberry Pi Imager (or, preferably, an SSH key).
- If it complains the destination doesn't exist, run this once then repeat the
  scp:
  ```bash
  ssh serv@serv-ljs-001.local "mkdir -p ~/serv-pi-staging"
  ```

### B3 — (Mac → Pi) Log into the Pi

```bash
ssh serv@serv-ljs-001.local
```

Your prompt changes — you're now on the Pi. The rest of Phase B runs here.

### B4 — (Pi) Run the installer (first-time setup)

This sets up the service, directories, permissions, and cellular helpers. It
does **not** start the reader yet.

```bash
cd ~/serv-pi-staging
sudo bash scripts/install.sh
```

Wait for `Install complete.` If it prints an error, stop and send it to Theo.

### B5 — (Pi) Put this unit's config in place

The bundle already contains the correct Louisville credentials, so you just copy
it in (no hand-editing):

```bash
sudo cp ~/serv-pi-staging/config.json /home/serv/plate_reader/config.json
sudo chown serv:serv /home/serv/plate_reader/config.json
sudo chmod 600 /home/serv/plate_reader/config.json
```

Confirm the right unit loaded (should print `REPLACE_WITH_serv-unit-XXX` and
`REPLACE_WITH_LOCATION_ID`):

```bash
sudo jq -r '.device_id, .location_id' /home/serv/plate_reader/config.json
```

### B6 — (Pi) Pre-warm the AI model on Wi-Fi

First run downloads ~50 MB of model weights. Do it now, on Wi-Fi, so it never
happens over cellular:

```bash
python3 -c "from fast_alpr import ALPR; ALPR(); print('model cached')"
```

Wait for `model cached`.

### B7 — (Pi) Run the bringup check

```bash
bash ~/serv-pi-staging/scripts/bringup.sh
```

Read the summary at the bottom. You want **no red FAIL lines**. Warnings about
cellular/Tailscale are OK to review with Theo. If cloud auth passes, the
credentials are correct.

### B8 — (Pi) Start the service

```bash
sudo systemctl start plate-reader
sudo systemctl status plate-reader     # should say "active (running)"
```

Watch it live:

```bash
sudo journalctl -u plate-reader -f
```

You should see detection activity, and — with this build — occasional
`DRIVE_OFF` lines when a car passes without being identified. Press **Ctrl+C**
to stop watching (doesn't stop the service). Type `exit` to leave the Pi.

---

## The swap (at the restaurant)

Only now, when the new unit is built and verified:

1. Power **off** the old Louisville unit. Confirm it's off.
2. Physically install and power on `serv-ljs-001` in its place.
3. From your Mac, confirm it comes up: `ssh serv@<new Tailscale IP>` and
   `sudo systemctl status plate-reader`.
4. Do **not** power the old unit back on. If you need to retire it, wipe its SD
   card so it can never rejoin as REPLACE_WITH_serv-unit-XXX.

---

## Quick reference (Phase B only, once you've done it once)

```bash
# Mac:
cd ~/Desktop/serv-pi
scp -r ./plate_reader ./scripts ./config.json ./config.example.json ./requirements.txt ./systemd serv@serv-ljs-001.local:~/serv-pi-staging/
ssh serv@serv-ljs-001.local
# Pi:
cd ~/serv-pi-staging && sudo bash scripts/install.sh
sudo cp ~/serv-pi-staging/config.json /home/serv/plate_reader/config.json
sudo chown serv:serv /home/serv/plate_reader/config.json && sudo chmod 600 /home/serv/plate_reader/config.json
python3 -c "from fast_alpr import ALPR; ALPR(); print('model cached')"
bash ~/serv-pi-staging/scripts/bringup.sh
sudo systemctl start plate-reader && sudo systemctl status plate-reader
```
