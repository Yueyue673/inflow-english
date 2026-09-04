# InFlow English 0.2.7 — lifecycle and recovery hardening

This is a **public source alpha / GitHub Pre-release**, not a Chrome Web Store approval or stable one-click installation.

## Fixed after the delayed 0.2.5 audit

### Continuous playback is actually continuous

The eight-second learning gate now resets immediately when the user:

- seeks;
- pauses, even briefly between polling ticks;
- hides the page;
- scrolls or resizes until the player is outside the usable viewport.

An invisible player cannot begin a teaching pause. The one automatic subtitle retry is represented as consumed after it fires and remains hard-limited to one attempt.

### Captions remain attached to the right route

- Chinese captions no longer use the same array index as a shortcut. A Chinese row must meaningfully overlap the English row in time; otherwise InFlow shows no Chinese line rather than a previous sentence.
- The MAIN-world one-shot observer obeys the persisted automatic-caption setting. Turning captions off clears its cache and restores the page's prior fetch/XHR functions.
- With captions enabled, the observer remains installed but performs no timed-text processing outside `/watch`; this preserves Home/Search → watch SPA capture timing.

### The event ledger can recover the profile

- If `profile.json` is missing but an event ledger exists, the server replays the ledger instead of creating a default profile and appending a reset event.
- New `profile_created` events record seed known IDs.
- New `session_created` events contain the bounded item catalog needed to replay learning after the original pack is unavailable.
- Historical events that reference unavailable content fail closed and ask for the pack/backup; they are not silently discarded.

### Process and tab ownership

- Import-job recovery runs only after the process obtains `server.lock`. A losing second server process performs zero recovery writes.
- Progressive focus requires the active `session_id`, `client_id` and `owner_epoch`.
- Stored focus epochs include the owner epoch, so an old tab's higher local counter cannot block a newly claimed owner.

## Reproducible packages

- ZIP entries use a fixed timestamp, Unix creator metadata and fixed regular-file mode.
- `.sha256` files use explicit LF bytes and work with `sha256sum -c`.
- Public Release assets are taken from the successful GitHub Actions artifact rather than rebuilt after CI.
- GitHub private vulnerability reporting is enabled.
- All InFlow source-alpha releases are marked Pre-release.

## Packages

- `InFlow-English-Chrome-0.2.7.zip` — development/source-alpha package with the fixed development key.
- `InFlow-English-Chrome-0.2.7-CWS-first-upload.zip` — first Store upload candidate without `manifest.key`.

For a new Chrome Web Store item, upload only the `-CWS-first-upload.zip` file. Google account registration, payment, identity/contact checks and review remain account-owner/external gates.

## Boundaries

Long-video active learning remains disabled in production. A passing transport/UI suite does not demonstrate 24-hour retention, seven-day retention or cross-speaker transfer.
