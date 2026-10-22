# September 16 audit: narrow route recovery and honest evidence

## Goal and boundaries

Recover real drive-through traffic without broadly weakening route filters or
merging adjacent queued customers. This release does not change hashes, OCR
thresholds, MMC ownership, camera exposure/resolution/FPS, Stream sampling,
count schema, production records or live settings.

## Reviewed evidence and plan

1. Pre-opening: AWS Visits query for Sept 16 midnight–10 AM Eastern returned
   zero. Keep existing 10 AM–10 PM traffic gating. Early local audit images are
   not visits. The capture scheduler's separate logging fault remains pending.
2. Photos 6 and 8: operator identifies real lane cars. Safe trajectory replay
   reproduces short-track false negatives (1.405s and 1.813s). Recover only
   when native geometry supplies a confirmed entry, consecutive terminal board
   samples, normal directional progress and enough distinct observations.
   Do not globally lower the two-second ordinary route threshold.
3. Shadows/edge fragments: photos 4 and 7 lack independent customer evidence.
   Preserve rejection of insufficient tracks; do not count every detected box.
4. Photos 5, 12 and long pickup: retained evidence is too early or partial.
   Opportunistically reuse later already-received full-frame JPEGs only when
   their camera/time belongs to the same completed trajectory. Record image
   age so an early frame is never presented as proof of plate invisibility.
5. Truck/trailer photos 9–11: visually one towing assembly, with model labels
   changing between Motorcycle and Big Truck. Photo 9's generating box owns
   the trailer/mower, not the pickup plate elsewhere in the image. A 32-second
   gap separates the two counted profiles without shared native proof. Do not
   deploy speculative time/colour/type-based merging: it could erase the next
   queue car. Trailer-related duplication remains unresolved, not fixed by
   this release.
6. Audit proof bug: a longer rejected trajectory was saved with a subsequent
   shorter qualifying trajectory's reason. Keep selected decision and native
   proof atomic, independent of which fragment has more points.

## Implementation

- Confirmed-entry alternative for the existing progressive-board OR. It needs
  at least eight distinct timestamps and two consecutive entry observations;
  existing board/time/progress checks remain. Repeated timestamps are not
  independent samples. Resolve a same-time disjoint phantom only with a unique
  >=70% overlapping continuation; deny the OR when ambiguous. Ordinary route
  proof stays unchanged, avoiding over-filtering established long stops.
- Upper-right terminal exclusion remains first and examines tied terminal
  alternatives before cleanup. All native boxes remain available for audit.
- Decision-owned route metadata across direct updates and profile merges,
  tested in authoritative and shadow modes. No new merge eligibility rules.
- Local-only shared original-frame cache capped at 16 frames / 8 MiB / 120s,
  validated against decoded native dimensions, matched by camera and provider
  time within 50ms. No additional camera consumer, inference, image crop or
  continuous recording. It improves audit evidence opportunistically, not OCR.
- Sidecars expose independent max plate scores, human-visibility uncertainty,
  and early/near-end/unknown image timing. Existing JPEG retagging cannot claim
  the timestamp of a later image that was not saved.

## Validation and release

Run architecture, Stream, route lifecycle, AWS, pipeline and new route/audit
tests. The pipeline suite's four pre-existing failures are the known baseline.
Replay retained field trajectories against baseline and candidate and inspect
any qualified-to-rejected change before release. An initial version failed
this requirement for two valid long tracks; cleanup was moved into the short
OR only to preserve their independent ordinary qualification.

Final replay of 77 available September 16 sidecars found 18 usable native
trajectories (59 confirmed-case sidecars did not contain full tracks): 11
rejected remained rejected, five qualified remained qualified, and exactly
the two reviewed false rejections became qualified. Zero previously qualified
replayed tracks became rejected. This tests classifier behavior, not complete
traffic recall or accepted OCR correctness. In particular, the trailer case's
historical proof mismatch cannot be corrected retrospectively by this replay.

Deploy only after exact human approval, during an empty lane: application
restart loses process-local encounters. No Stream restart or camera/threshold
config change is needed for these Python changes. Verify actual deployed SHA,
running Stream status, frame rate, live board routes, and AWS delivery after
deployment. Roll back to the previous approved application release if reviewed
true lane cars are newly excluded or adjacent cars are incorrectly joined.

No improvement percentage is established by unit tests. Recognition accuracy,
full traffic recall, dusk blur, thermal limits, unsupported trailer deduplication,
and real-time voice-AI timing still need separate live validation. No historical
counts are automatically rewritten by this release.
