# Ivar’s location 001 — Serv field kit

**Taking over this unit? Start with [OPERATOR_HANDOFF.md](OPERATOR_HANDOFF.md).**
It covers repo access, Tailscale, your own SSH key, live viewing, testing,
and the steps still required before production.

This repository operates the existing Raspberry Pi 5 + Tapo C113 + Sixfab
cellular unit for Ivar’s Lakewood Seafood Bar. The Pi already has its software
installed. Clone this repository **onto your computer** to use the operator CLI.

## Current state

Verified 22 October 2026, 00:36 Pacific: sandbox mode; cellular and Tailscale
working; camera received locally at 15 fps; application, recognizer and camera
isolation active. Installed Pi release:
`29b16c8626b31866663bf3bf3b74513717a64cd2`.

Production identity, credentials and restaurant hours are prepared. Onsite
day/night accuracy, lane calibration, whole-unit restart/recovery and a real
production customer visit still need acceptance. **Powering it on currently
starts the sandbox; it does not activate production.** Read live status rather
than treating this dated snapshot as a current health guarantee.

## Everyday commands

After completing the access and laptop setup in the handoff, run these from
this repository on your computer:

```sh
./serv status
./serv doctor
./serv view --seconds 60
./serv image
./serv metrics --minutes 15
./serv ssh
```

`doctor` asks for the Pi’s sudo password. `view` asks before sending video over
cellular and stops after its time limit. Closing the player leaves recognition
running. Camera-to-Pi video stays on `SERV-Camera`; a remote preview can use
cellular between the Pi and your computer. Metrics are local audit summaries,
not proof of AWS delivery or a measured recognition rate.

## Device and site

| Item | Value |
|---|---|
| Pi login | `serv@100.77.200.119`, ordinary SSH over Tailscale |
| Tailscale name / Linux hostname | `serv-camera-bench` / `servTest` |
| Camera | Tapo C113, local address `10.42.0.36` |
| Camera Wi-Fi | `SERV-Camera`, open by the owner’s explicit choice |
| Production device / location | `serv-ivars-001` / `ivars-001` |
| Restaurant | Lakewood Seafood Bar, 10114 S Tacoma Way, Tacoma, WA 98499 |
| Hours | Sun–Thu 11am–9pm; Fri–Sat 11am–10pm, Pacific |

The production schedule warms capture at 10:45am. Sandbox currently captures
all day. The computer stays on its usual internet connection; it need not join
the camera Wi-Fi. The built-in Pi Wi-Fi belongs to the camera, so a Wi-Fi
internet uplink requires a second adapter.

## Maintenance references

- [Operator handoff](OPERATOR_HANDOFF.md): what the next person should do.
- [Field setup and acceptance](FIELD_SETUP.md): installed state, evidence,
  commissioning and historical installation details.
- [Architecture](ARCHITECTURE.md) and [AGENTS.md](AGENTS.md): read before code
  changes. Much of the core also serves other, live restaurant units.
- [Security](SECURITY.md): device boundaries and human approval for deployments.
- [Site settings](fieldkit/sites/ivars-001.json): public identity and hours;
  credentials remain private on the Pi.

Do not run the generic `scripts/install.sh`, copy an example config onto this
Pi, or apply the LJS/IMX296 deployment instructions to this Tapo unit. Initial
adoption and the open-Wi-Fi operator update are already installed. A laptop
`git pull` does not deploy software to the Pi. Future device code deployments
require the applicable reviewed installer and exact human-approved release.

Raw OCR stays on the Pi and is hashed there before cloud delivery.
Make/model/colour are descriptive only. Never change `normalizer.py` or
`hasher.py` to accommodate a test, and never commit credentials or private keys.
