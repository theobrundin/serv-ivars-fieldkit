# ultimateALPR Licensing — unit register

Tracks which physical units hold a Doubango ultimateALPR activation, and the
handling rules that keep us from wasting them.

> **Never record a token string in this file, in the repo, in a log, in a
> ticket, or in a chat message.** Paths only. The token is the licence.

---

## Entitlement

| | |
|---|---|
| Product | `ANPR` |
| OS family | `eLinux` |
| Architecture | `AArch64` |
| Activations purchased | **15** |
| Activations used | **2** |
| **Remaining** | **13** |
| Terms | one-time, perpetual, machine-bound |

Activations are **irreversible and bound to the machine that requested them**.
There is no self-service way to release one. Burning a unit on a dev box, a
VM, or a machine that will later be reflashed is a permanent loss of 1/15.

---

## Activated units

### `servCamOG` — bench / validation unit

| | |
|---|---|
| Hostname | `servCamOG` |
| Tailscale IP | `100.68.10.100` |
| Hardware | Raspberry Pi 5, 4 cores, 3 GB RAM |
| OS | Debian GNU/Linux 13 (trixie) |
| Arch | `aarch64` |
| SDK | v3.13.2 (runtime banner — authoritative) |
| Binaries | `binaries/linux/aarch64` |
| Role | **licensed bench / validation unit** — not a field unit |
| Activated | **2026-08-01** (from token file mtime, 10:14 −0400) |
| Activation # | 1 of 15 |

**Paths (contents never reproduced):**

| What | Path |
|---|---|
| Licence token file | `/home/serv/license_token_servcamog.txt` (`chmod 600`, `serv:serv`, 261 bytes) |
| Runtime-key tool | `~/ultimateALPR-SDK/binaries/linux/aarch64/runtimeKey` |
| SDK checkout | `/home/serv/ultimateALPR-SDK` |

The engine loads the token via the SDK's `license_token_file` JSON config key,
pointed at the path above by the `license_token_file` setting in the unit's
config. The token string is never inlined, never passed on a command line
(where it would land in shell history and `ps`), and never committed.

### `serv-ljs-001` — production field unit (Louisville)

| | |
|---|---|
| Hostname | `serv-ljs-001` |
| Tailscale IP | Private inventory only; do not commit management addresses |
| Hardware | Raspberry Pi 5, 4 cores, 4 GB RAM |
| OS | Debian GNU/Linux 13 (trixie) |
| Arch | `aarch64` |
| SDK | v3.13.2 (runtime banner — authoritative) |
| Binaries | `binaries/linux/aarch64` |
| Role | **live production unit** — real customer traffic |
| Activated | **2026-08-02** via manual activation (Doubango License Manager web portal) |
| Activation # | 2 of 15 |

**Paths (contents never reproduced):**

| What | Path |
|---|---|
| Licence token file | `/home/serv/license_token_serv-ljs-001.txt` (`chmod 600`, `serv:serv`, 261 bytes) |
| SDK checkout | `/home/serv/ultimateALPR-SDK` |

Cutover from `fast_alpr` completed same-day: `config.json` switched to
`engine: "ultimatealpr"`, license verified (`license verified: unmasked
output confirmed`), service restarted clean twice (once to confirm continuity,
once from a fresh boot after `fast-alpr` was uninstalled from the unit), then
the `fast-alpr` pip package was removed entirely from this machine. Chosen
profile matches the documented standard: plates + vehicle box + colour
(VCR) on, LPCI/VMMR/VBSR off, `rectify_enabled` off. `presence.dwell_zone`
left **disabled** — this camera's mount position has never been calibrated
for it, so `vehicle_detect_enabled: true` correctly logs a one-shot warning
that vehicle-only events are suppressed until `tools/calibrate.py` is run
on site.

⚠️ **This unit was thermally throttled 100% of a measured 10-minute window
before this cutover** (82.9–87.8°C, no active cooling hardware present). The
engine switch does not address that — it is a hardware problem. See
`docs/STAGE2B_PIPELINE_BRINGUP.md`-style field notes for the thermal
measurement; re-measured after this cutover to see whether the new engine's
CPU profile changes the picture at all (it is not expected to fix it).

---

## ⚠️ Do not reflash this SD card — applies to EVERY activated unit

The activation is bound to the machine. **It is not established that a token
survives an OS reinstall**, even on identical hardware — the machine
fingerprint may derive from something the reinstall changes. This now applies
to **both** `servCamOG` and `serv-ljs-001`.

Before any reflash, re-image, OS upgrade that replaces the root filesystem, or
SD-card swap on an activated unit:

1. Confirm with `tech@doubango.org` whether the existing token remains valid,
   and whether a re-activation would consume another of the 15.
2. Back up the unit's token file off-device to somewhere secret-appropriate —
   **not** this repo:
   - `servCamOG`: `/home/serv/license_token_servcamog.txt`
   - `serv-ljs-001`: `/home/serv/license_token_serv-ljs-001.txt`
3. Only then proceed.

Getting this wrong costs a permanent activation and there is no undo.

---

## Rules for adding a unit

1. Activate **only** on hardware that will keep its role. Never on a laptop,
   container, CI runner, or throwaway VM.
2. Generate the runtime key **on the target device** —
   `UltAlprSdkEngine_requestRuntimeLicenseKey()` is reachable from Python, so
   no C++ sample is needed.
3. Store the token as a file, `chmod 600`, owned by the service user.
4. Point config at the **path**. Never inline the token.
5. Add a row above and decrement the remaining count in the same commit.

---

## Verifying a unit is genuinely licensed

A unit that fails to load its token does **not** error — it silently degrades
to trial mode, where the SDK redacts the last character of every plate
(`3PEDLM*`). That redaction survives `normalize()` (rule 2 strips `*`, and the
5–6 character remainder clears the rule-3 floor of 4), so it would hash into a
confident **wrong** vehicle identity with nothing downstream able to notice.

Two defences, both required:

- `UltimateAlprEngine` raises `RedactedPlateTextError` on any `*` in returned
  text, in the adapter and again in the harness. It never logs-and-continues.
- A one-shot check at init processes a known frame and asserts the text is
  unmasked, logging `license verified: unmasked output confirmed` exactly once.

On a licensed unit the `*` guard must **never** fire. If it does, the token is
not loading — treat it as a hard failure and fix the token path before running
anything that writes hashes.

See `docs/ULTIMATEALPR_SPIKE_FINDINGS.md` §1.3 and §8.5.
