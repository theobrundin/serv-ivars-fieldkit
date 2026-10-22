# Working in this repo

**Read [ARCHITECTURE.md](ARCHITECTURE.md) first.** It describes how the system
actually works and, more usefully, the traps that have already caused live
outages. Its factual claims are machine-checked by
`tools/test_architecture_doc.py`, so it can be trusted where a normal doc
could not.

This file is the short version: the rules that change what you should do.

## This is a live system in a real drive-thru

`serv-unit-001` reads plates at an operating restaurant. A bad change does not
fail a test suite, it stops counting customers — or worse, records the wrong
ones. Before changing detection behaviour, ask what happens to a car that is
mid-lane when it lands.

## Never change these

- **`plate_reader/normalizer.py` and `hasher.py`.** Every historical vehicle
  hash depends on their exact behaviour. Changing them turns every returning
  customer into a new one, silently, with no error anywhere.
- **The on-device hashing boundary.** Raw plate text must never leave the Pi.
  The webhook listener refuses to bind a non-loopback address; keep it that
  way. This is a pending-patent claim, not a style preference.
- **Make/model/colour as descriptive-only.** They must never enter hash input.

## Confidence gates are engine-specific

Plate Recognizer's OCR scores sit near 0.99; ultimateALPR's gates assumed
0.75. The engine and its thresholds are one unit — swap both or neither.
`docs/STREAM_THRESHOLD_TUNING.md` shows the measurements behind the current
values, including a site eval that overruled a video-derived recommendation.

## Verify against reality, not documents

Three documents have been confidently wrong here and each cost real time: a
brief specifying a codec this hardware does not have, vendor docs describing
the wrong webhook format, a handoff naming a UI control that does not exist.
When a claim is load-bearing, check it against the running system — capture
the actual payload, read the actual config, query the actual table.

## Deploying is gated, and it is two steps

```sh
sudo /usr/local/sbin/serv-update --approve <full-40-char-sha>   # consent
sudo /usr/local/sbin/serv-update                                # apply
```

**Do not self-approve.** `SECURITY.md` is explicit that this gate exists so a
push — or a misaligned agent — cannot become fleet-wide root execution. Ask
the human to run the approve step, even if you have passwordless sudo. Note
the commit must already be fetched locally and must equal the head of the
channel in `.update_channel` (currently `codex/pi-security-hardening`, not
`main`).

## Before you finish

Run the suites that matter for what you touched:

```sh
python3 tools/test_architecture_doc.py     # doc still matches the code
python3 tools/test_stream_engine.py        # webhook parsing, coalescing, gates
python3 -m unittest discover -s aws/tests  # Lambda behaviour
```

`tools/test_pipeline_features.py` has **4 known pre-existing failures** — that
is the baseline, not a regression you caused. Confirm the count is still 4.

If you changed the engine list, shipping gates, capture ownership, or any of
the documented traps, `test_architecture_doc.py` will fail until you update
`ARCHITECTURE.md`. That is the mechanism keeping it honest. Update the doc;
do not weaken the test.
