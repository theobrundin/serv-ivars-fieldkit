# `docker/` — ultimateALPR container

## Why this exists

Doubango ships **no macOS build** of ultimateALPR and none is planned
(vendor-confirmed). `import ultimateAlprSdk` can therefore never work from host
Python on a Mac. This container is the only way to exercise the ualpr path
during development — it is **permanent infrastructure, not a workaround**.

fast-alpr is unaffected and still runs natively on the host.

## Architecture — read before changing anything

Build **native `linux/arm64`**. Never add `--platform=linux/amd64`.

| | |
|---|---|
| `binaries/linux/aarch64` | ✅ correct — 64-bit vanilla Linux, and the Pi 5 target |
| `binaries/linux/x86_64` | ❌ AVX/AVX-512, SIGILLs under QEMU on Apple Silicon |
| `binaries/raspbian/armv7l` | ❌ 32-bit only, wrong for Pi 5 |

The Pi 5 on 64-bit Raspberry Pi OS uses the *same* `linux/aarch64` binaries as
this container, so the dev surface is faithful to production.

## Usage

Build (from the repo root, so the build context includes `docker/`):

```bash
docker build -f docker/Dockerfile.ualpr -t serv-ualpr .
```

Run, mounting the SDK checkout and the repo:

```bash
docker run --rm -it -v ~/Desktop/alpr-spike/ultimateALPR-SDK:/sdk -v "$PWD":/work -w /work serv-ualpr bash
```

Inside, build the Python binding once per SDK checkout:

```bash
build_ualpr_binding
```

That script is idempotent and encodes the two non-obvious build steps:
`setup.py` must run from `binaries/<os>/<arch>/` (it uses `../../../` relative
paths), and the shipped binaries lose their exec bit through `git clone`.

## Version checking

Gate on the **runtime banner**, which reports `3.13.2`. The header macro in
`c++/ultimateALPR-SDK-API-PUBLIC.h` says `3.10.5` and is **stale** —
vendor-confirmed that the binary wins. Never gate on the header.

## Expect the `*` guard to fire

In trial mode the SDK redacts the last character of every plate (`3PEDLM*`).
`UltimateAlprEngine.infer()` raises `RedactedPlateTextError` on any `*`, and
the harness raises again as a second line of defence.

**This is correct behaviour, not a failure to work around.** A redacted read
would otherwise be stripped by `normalize()` to a plausible 5–6 character
string, clear the length floor, and hash into a confident wrong identity that
nothing downstream can detect. Whether a license removes the redaction is a
*working assumption* pending verification on the Pi after activation — see
`docs/ULTIMATEALPR_SPIKE_FINDINGS.md` §1.3 and §8.3.

Never generate a runtime key or pass `--tokendata` on a dev machine.
Activations are irreversible and machine-bound; there are only 15.
