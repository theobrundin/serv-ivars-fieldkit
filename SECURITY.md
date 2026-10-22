# Serv field-camera security baseline

The camera is an Internet-connected system handling pseudonymous vehicle data.
Security failures can expose customer activity, falsify analytics, disable the
lane, or turn a fleet update mechanism into remote root execution.

## Trust boundaries

- The Pi application runs as the unprivileged `serv` account. Executable
  application code is root-owned under `/opt/serv`; writable state remains
  separate under `/home/serv/plate_reader`.
- The application has `NoNewPrivileges` and no `sudo` permission. A confined
  root oneshot collects read-only modem statistics into `/run/serv`.
- Remote administration is accepted only through Tailscale.
- Raw plate text is normalized and hashed on-device and must never leave the
  Pi or enter persistent logs.
- GitHub is a source repository, not deployment authority. A push, merge, or
  coding-agent action cannot authorize field installation by itself.
- AWS accepts authenticated device events, but detection diagnostics do not
  create physical visits. Only `/visit` changes vehicle traffic totals.

## Required controls

1. Apply `scripts/harden_pi.sh` on every field unit. It installs a default-deny
   inbound firewall, blocks direct cellular/LAN administration, disables
   unnecessary headless services, removes passwordless tty1 auto-login, and
   tightens SSH.
2. Verify a separate public-key SSH session before applying
   `scripts/harden_pi.sh --key-only`. Password authentication is a temporary
   recovery mode, not the fleet target.
3. Keep API keys in `/home/serv/plate_reader/config.json` mode `0600`. Never
   paste them into tickets, chats, Git, logs, screenshots, or command output.
4. Keep queue databases and camera previews mode `0600`.
5. Install the updater at `/usr/local/sbin/serv-update`, owned by root. Never
   execute an updater from `/home/serv/serv-pi`, which is writable by `serv`.
6. Approve one exact reviewed commit with
   `sudo /usr/local/sbin/serv-update --approve <40-character-sha>`. The updater
   deploys only when `origin/<channel>` equals that SHA. Dependency, installer,
   and systemd changes always require a manual reviewed install.
7. Monitor heartbeat age, build SHA, runtime code hash, temperature,
   throttling, dead-letter count, and Tailscale health. Alert on missing
   heartbeats rather than assuming the camera is healthy.
8. Keep the local console password-protected. Audit systemd getty drop-ins for
   `--autologin` after imaging or re-running Raspberry Pi setup tools.

## Agent and supply-chain safety

Automation may propose code and run tests, but should not independently create
deployment authority. Protect `main`, require review for production paths, use
least-privilege GitHub tokens, and do not place AWS, Tailscale, SSH, or device
secrets in agent-accessible repositories. An approved SHA is intentionally a
local root decision so a compromised GitHub account or misaligned agent cannot
turn a push into fleet-wide root execution.

## Physical resilience

Software cannot prevent a pole, cable, lens, or enclosure from being damaged.
Use tamper-resistant fasteners and enclosure, concealed/armored cabling, a
mounting position outside easy reach, surge protection, and an independent
wide-angle camera covering the unit. Alert on power loss, camera/inference
failure, abrupt scene obstruction, and heartbeat loss. Retain only the minimum
data necessary for operations and incident response.

## Incident response

1. Isolate or revoke the device in Tailscale and AWS.
2. Preserve the queue database, journal, deployed SHA, runtime hash, and recent
   heartbeat records without copying raw plate text.
3. Rotate the per-device API key and affected SSH/Tailscale credentials.
4. Re-image from a known-good OS image if root compromise is possible.
5. Approve and deploy a reviewed commit; do not repair an untrusted root
   filesystem in place and return it to service.
