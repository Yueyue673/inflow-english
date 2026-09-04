# InFlow English 0.2.3 — public source alpha

This release makes the useful core independent from the experimental backend.

## Use now

- A visible InFlow state appears as a YouTube watch page starts.
- Supported videos show Chinese-first, English-second captions from the current YouTube player's own caption tracks.
- Press `S` to replay the current complete caption segment.
- Missing tracks end with a clear retryable error while YouTube keeps playing.
- Python and localhost are not required for captions.

## Deliberately opt-in

Experimental automatic learning pauses are off by default. Enabling them requests optional localhost access and starts preparation only after eight seconds of continuous, visible, non-ad playback.

## Install status

The attached ZIP is a reproducible developer-mode alpha. Verify it with the attached SHA-256 file, extract it, then load the folder through `chrome://extensions`.

This is **not yet a Chrome Web Store release**. One-click installation and automatic extension updates remain blocked on developer registration, account verification and Google review.

## Verification

The release candidate is covered by isolated unit and browser contracts for:

- standalone/offline captions;
- bounded failure and retry;
- ads and same-document navigation;
- closed-shadow privacy and synthetic-input rejection;
- explicit learning opt-in and eight-second gate;
- natural-phrase playback, continue/replay, playback ownership and event replay;
- exact/provisional lexical identity;
- loopback, media and heavy-work resource boundaries.

See `TEST-RESULTS.md` for the exact commands and results recorded for this tag.

## Known limits

- Broad real-YouTube compatibility remains under measurement.
- Videos without a usable YouTube caption track cannot use the standalone caption path.
- The optional local learning service remains developer/private-tester tooling.
- Long-video active learning remains disabled.
- This release does not claim 24-hour/7-day retention or transfer gains.
