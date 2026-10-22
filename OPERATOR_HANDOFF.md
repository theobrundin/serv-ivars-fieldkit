# Ivar’s Pi — handoff to the next operator

This guide is for taking over the **existing** Pi. Setup commands below target
macOS. All `./serv` commands run on your computer from the cloned repository,
unless a block explicitly says **on the Pi**. Nothing needs reinstalling on
the Pi to use these commands.

Repository: <https://github.com/theobrundin/serv-ivars-fieldkit> (private).

**Status checked 22 October 2026, 00:36 Pacific:** sandbox mode, cellular
connected, Tailscale reachable, Tapo feeding Plate Recognizer at 15 fps, local
camera routing and isolation active. Installed release:
`29b16c8626b31866663bf3bf3b74513717a64cd2`. Production activation is still pending
real site acceptance. A working video feed alone does not prove accurate counts
or cloud delivery.

## 1. Theo: arrange access before leaving

There are three independent access requirements:

1. **GitHub:** invite the person’s GitHub account to this private repository
   through repository **Settings → Collaborators → Add people**. They must
   accept. See [GitHub’s collaborator instructions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/inviting-collaborators-to-a-personal-repository).
2. **Tailscale:** invite their own account to the authorized tailnet, or share
   only the `serv-camera-bench` machine with them. Access policy must allow
   their computer to reach `100.77.200.119` on TCP port 22. Both parties’ policies
   can affect shared-machine access. See [inviting versus sharing](https://tailscale.com/docs/reference/inviting-vs-sharing)
   and [machine sharing](https://tailscale.com/kb/1084/sharing).
3. **SSH:** enroll their personal public key using section 3. Password SSH is
   disabled. GitHub and Tailscale access alone will not let them log in.

Give the approved operator the Pi’s **sudo password privately**, through your
password manager or a separate secure channel. It is different from their
personal SSH-key passphrase. Do not put either password, an API key, a Tailscale
auth key, or a private SSH key in this repository. Routine operation does not
require AWS administrator credentials.

**Before departure, have them run `./serv status`, `./serv doctor` and
`./serv ssh` successfully on their own computer.** Have them type `exit` to
leave the Pi shell. Identify a person with physical access for power, camera
aiming, and recovery if cellular is unavailable.

## 2. New operator: set up your Mac

Install [Tailscale for macOS](https://tailscale.com/docs/install/mac), sign in
with your own account, accept the invitation/share, and turn Tailscale on.
Keep your Mac on its usual Wi-Fi. Do not join `SERV-Camera` for remote operation.

Install [Homebrew](https://brew.sh/) if needed and follow its PATH instructions.
Then run in Terminal:

```sh
brew install git gh python ffmpeg
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
mkdir -p "$HOME/Projects"
cd "$HOME/Projects"
git clone --branch codex/ivars-001-fieldkit https://github.com/theobrundin/serv-ivars-fieldkit.git
cd serv-ivars-fieldkit
./serv --help
```

Use your own GitHub account in the browser login. If you already cloned the
repository, enter that directory and use `git pull --ff-only` instead of
cloning again. Stop if Git reports local changes or a merge conflict.
[GitHub CLI login reference](https://cli.github.com/manual/gh_auth_login).
FFmpeg supplies the `ffplay` preview window.

Create a dedicated SSH key. Choose a passphrase when prompted. If this key
filename already exists, do not overwrite it; use the existing key or ask the
maintainer to help choose another filename.

```sh
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/serv_ivars_001" -C "ivars-001-operator"
ssh-add "$HOME/.ssh/serv_ivars_001"
ssh-keygen -lf "$HOME/.ssh/serv_ivars_001.pub"
pbcopy < "$HOME/.ssh/serv_ivars_001.pub"
```

Send Theo the copied **public** key and its fingerprint. The `.pub` file is safe
to share; the similarly named file without `.pub` is your private key and stays
on your computer.

Add this block once to `~/.ssh/config` using a text editor. If a block for this
IP already exists, update it instead. This lets the operator CLI select your
dedicated key without depending on which other keys your SSH agent has loaded:

```sshconfig
Host 100.77.200.119
    User serv
    IdentityFile ~/.ssh/serv_ivars_001
    IdentitiesOnly yes
```

```sh
chmod 600 "$HOME/.ssh/config"
```

The CLI supplies the repository’s pinned Pi host key automatically. This uses
**ordinary OpenSSH through Tailscale**. Do not enable Tailscale’s separate SSH
server or re-enable password SSH to get around an access problem.

## 3. Theo: enroll their public key

Save their single public-key line as `~/Downloads/serv_ivars_001.pub` on your
authorized computer. Verify its fingerprint with the person through a trusted
channel. Run the following **from your local repository directory**, using your
existing working Pi SSH access. The command appends their public key and
preserves current access:

```sh
OPERATOR_PUBLIC_KEY="$HOME/Downloads/serv_ivars_001.pub"
ssh-keygen -lf "$OPERATOR_PUBLIC_KEY"
```

Only after confirming that fingerprint, run:

```sh
ssh -T \
  -o HostKeyAlias=servtest.local \
  -o UserKnownHostsFile="$PWD/fieldkit/known_hosts" \
  -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=10 \
  serv@100.77.200.119 \
  'umask 077; mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys" && printf "\n" >> "$HOME/.ssh/authorized_keys" && cat >> "$HOME/.ssh/authorized_keys"' \
  < "$OPERATOR_PUBLIC_KEY"
```

No sudo is needed for this addition to `serv`’s own authorized keys. The new
operator must now verify access; successfully adding a key is not proof their
Tailscale policy and laptop setup work. Do not send them Theo’s private key.

## 4. Power on and check the unit

Power the Pi/modem and camera with their intended supplies; connect the LTE
antennas. Keep the camera paired with `SERV-Camera`. Give startup a few minutes,
then run **on your computer**:

```sh
cd "$HOME/Projects/serv-ivars-fieldkit"
./serv status
./serv doctor
```

Enter the **Pi sudo password** if `doctor` asks. No characters appear while you
type a password; that is normal.

Expected while capture is scheduled to run:

| Status field | Expected |
|---|---|
| `tailscale.running` | `true` |
| `internet_interface` | `wwan0` when using cellular |
| `camera_local_route` | `true` |
| `camera.healthy` | `true`, with fresh status and received FPS above zero |
| `services` | application, PR, cellular and isolation `active` |
| `throttled` | ideally `throttled=0x0` |
| `camera_wifi_secured` / `camera_wifi_open_authorized` | `false` / `true`, the owner’s explicit open-Wi-Fi policy |

At handoff, `mode: sandbox`, device `serv-unit-bench-reolink`, location
`bench-reolink-house` are expected. The prepared production identity is
`serv-ivars-001` / `ivars-001`. `doctor` should still report
`ready_to_activate: false` until site acceptance is recorded. Do not turn
failed acceptance flags into `true` just to bypass that check.

Sandbox queues accumulate local records and do not send them to AWS. They are
kept separate from the production queue and are not replayed on activation.
In production, PR intentionally stops outside the capture schedule; the app
and heartbeat remain active. Check mode and local hours before diagnosing
an off-hours stopped recognizer as a fault.

## 5. View and aim the camera

```sh
./serv view --seconds 60
```

Read the displayed uplink. If cellular is available, answer `y` only if you
approve cellular use for that preview (roughly 10 MB/minute plus overhead).
Otherwise answer `n`; recognition continues. Close the video window or press
`q` to finish early. **Stopping the preview does not stop Plate Recognizer.**

The camera-to-Pi RTSP feed is local over `SERV-Camera`. The remote preview from
Pi to computer can cross cellular, even though the computer uses home Wi-Fi.
Normal AWS delivery contains hashes and descriptive vehicle attributes, not
continuous video or raw OCR text.

To inspect and optionally adjust the four exposed image controls:

```sh
./serv image
# Example adjustment only; note the previous value before changing it:
./serv image --brightness 50
```

`--contrast`, `--saturation` and `--sharpness` also accept 0–100. Change one
setting at a time and compare actual moving plates. Restore the recorded old
value if worse. Camera movement or image changes invalidate earlier optical
acceptance; the tool cannot automatically detect that invalidation.

This C113’s tested firmware exposed no optical zoom, motorized focus or working
manual bitrate increase. Its measured main stream is 2304×1296 at 15 fps and
about 1,280 kb/s. Improve plate size by moving/aiming the camera appropriately;
digital enlargement cannot restore missing detail. Readability at 15 metres
has not passed acceptance. Night mode may change the image, but automatic
adjustment alone does not prove moving plates are readable after dark.

## 6. Test recognition at the actual mounting position

Keep the unit in sandbox until the maintainer has reviewed actual site
evidence. Run before and after a short, timestamped group of passes:

```sh
./serv status
./serv metrics --minutes 15
```

Record the test’s Pacific start/end times, physical car count, direction,
lighting and a non-identifying label such as “test car A.” Include daylight,
darkness/headlights, two close cars, a slow or stopped car, cross traffic, and a
returning vehicle after the configured cooldown. Have the technical maintainer
review on-Pi outcomes, images and route geometry against these observations.
Keep plate text and audit media on the Pi; send only sanitized counts/results.

`metrics` counts recently written, bounded audit cases. `confirmed` is an OCR
outcome; `visit_queued` is local enqueue. Neither proves a unique physical car,
an AWS customer, or an end-to-end recognition rate. Zero recent outcomes can
mean no eligible pass, unreadable plates, delay, or a pipeline fault. Check
timestamps and camera health before changing detection settings. Do not lower
confidence gates to make the test appear successful.

With an empty lane and someone physically present, test a whole-unit restart:

```sh
./serv ssh
```

**On the Pi**, shut down cleanly:

```sh
sudo shutdown -h now
```

Wait for shutdown to finish, disconnect power to **Pi, modem and camera**,
then restore all power. Back on the computer, repeat `./serv status` and
`./serv doctor` after a few minutes. Verify local camera reception and cellular
return without manually starting services. A Pi-only `reboot` does not test a
modem/camera power cycle. Conduct any planned WAN-outage test with local
recovery available and a maintainer; do not disable the only remote uplink.

## 7. Finish commissioning and activate production

This part needs the technical maintainer and real evidence. The private
production identity and hours are already installed. The initial adoption and
open-Wi-Fi operator update are complete; **do not rerun either installer**.

The maintainer must calibrate/review this site’s lane geometry, review day/night
and counting results, then record the evidence in root-private
`/etc/serv-field/commissioning.json`. The required identity and pass fields are
listed in [FIELD_SETUP.md](FIELD_SETUP.md#commissioning-sequence). The current
inherited route classification is in shadow; it is not a validated Ivar’s
traffic filter. A JSON flag alone cannot establish that validation.

After evidence review, on the computer:

```sh
./serv doctor
```

Only when `ready_to_activate` is `true`, the maintainer approves commissioning,
and **the lane is physically empty**:

```sh
./serv mode production --lane-empty
./serv status
```

This restarts the application. Verify mode `production`, device
`serv-ivars-001`, location `ivars-001`, and the Pacific schedule. During open
hours, run a real vehicle through and have an authorized cloud maintainer
verify the Visit, Customer profile, descriptive MMC, correct local date/hour,
queue drain and repeat-visit deduplication. Repeat after a whole-unit cold boot
and verify queued delivery after a controlled WAN outage. Do not use synthetic
production visits as proof of camera recognition. Production activation persists
across boots; it is not a command to rerun every morning.

Location: **Ivar’s Lakewood Seafood Bar**, 10114 S Tacoma Way, Tacoma, WA 98499.
Hours: Sun–Thu 11am–9pm; Fri–Sat 11am–10pm, `America/Los_Angeles`.
Capture warmup begins at 10:45am. After commissioning, normal use is power on,
automatic cellular/recognition startup during the schedule, and periodic health
checks. The operator computer can disconnect; it is not part of recognition.

If commissioning fails, wait for an empty lane and return to sandbox:

```sh
./serv mode sandbox --lane-empty
./serv status
```

Sandbox stops cloud delivery but keeps local testing available. This is a mode
change, not a software rollback. Do not delete queues or keys to troubleshoot.

## 8. Network choices and troubleshooting

Keep the existing cellular preference unless intentionally changing it:

```sh
./serv network cellular
```

For restaurant Wi-Fi internet, a maintainer must first attach/configure a
**second Wi-Fi adapter**. Once that adapter has a working internet connection:

```sh
./serv network wifi
./serv status
```

These commands test the requested uplink and change its preference; they retain
fallback and can briefly interrupt SSH. Never repurpose `wlan0`: it serves the
camera. Wi-Fi preference, or plugging in router Ethernet, does not alone prove
a cellular-free preview. Strict no-cellular viewing requires a separately
verified uplink cutover and removal of cellular routes. The preview guard will
continue asking for consent while cellular is available.

| Symptom | Next step |
|---|---|
| GitHub “not found” or clone denied | Accept the private repo invite; check `gh auth status` uses your account. |
| SSH times out | Confirm Tailscale is on, Pi is powered, machine is shared/visible, and access policy permits TCP 22. Have the onsite person check power and LTE signal. |
| `Permission denied (publickey)` | Confirm section 3 was completed and your `~/.ssh/config` selects the matching key. Run `ssh-add "$HOME/.ssh/serv_ivars_001"` if needed. A Pi login password will not fix this. |
| Host key changed warning | Stop and ask the maintainer to verify the Pi’s identity. Do not delete the pinned key or disable strict checking. |
| `ffplay` missing | Run `brew install ffmpeg` on the computer and retry. |
| No preview picture | Close the player, recheck status and camera power/pairing. The CLI’s “preview stopped” message alone does not prove frames arrived. |
| Camera unhealthy during scheduled hours | Check camera power, pairing and local route; do not factory-reset or change Wi-Fi credentials as a first step. |
| Production gate refuses | Read the failed checks; complete real acceptance or repair the failed dependency. Do not bypass the gate. |
| Production queue grows or dead letters appear | Have the maintainer check uplink/cloud authentication and delivery before claiming visits reached AWS. Preserve queue data. |

For help, share `./serv status`, `./serv doctor` and `./serv metrics --minutes 15`
output with the authorized maintainer. These omit API keys and raw OCR. Avoid
copying private configuration, full logs, RTSP URLs or camera credentials into
GitHub issues or chat. Raw plate text must remain on-device.

## Maintenance notes for the technical helper

Read [AGENTS.md](AGENTS.md), [ARCHITECTURE.md](ARCHITECTURE.md) and
[FIELD_SETUP.md](FIELD_SETUP.md) before changing anything. This repository also
contains code/runbooks for other live sites. Do not apply their installers,
identity or geometry to this unit. Repository pulls only update your computer;
device code changes need the applicable exact-release human approval. Agents
must not self-approve deployments.

The installed software has known limitations: four baseline pipeline test
failures, no claimed crash-proof exactly-once ingest, and a Pi kernel without
enforced Docker memory limits. The PR trial expires at the end of 2026; arrange
ongoing licensing before then. Check Tailscale key expiry in status and arrange
renewal before access expires. Current local audit/queue totals and synthetic
AWS tests are not substitutes for measured field accuracy.
