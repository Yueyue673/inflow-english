# Changelog

All notable public changes are documented here.

## 0.2.6 — 2026-09-05

### Fixed from real-use feedback

- Dock the status, word panel and teaching card beside the video when horizontal space exists; theater/fullscreen keeps a compact in-video fallback.
- Make status and word panels mutually exclusive; page click and Escape close transient panels.
- Align `关闭本视频` / `开启本视频` labels with their exact inverse actions in both the page surface and popup.
- Keep subtitle word-state results visible, explain their future effect and add append-only `撤销` that restores the exact previous scheduling state from a server-owned snapshot.
- Accept feedback for a newly resolved WordNet subtitle key instead of incorrectly returning `knowledge_key_mismatch`.
- Add a visible `跳过这次` action to the mapping stage and keep all four teaching actions in a fixed, unclipped side action area.
- Hide fixed InFlow surfaces while the player is outside the usable viewport and restore them when it returns; an active teaching step scrolled out of view ends as a technical interruption without auto-resume.
- Reduce the caption background footprint and move it closer to the control-safe lower edge.

### Reliability and safety

- Keep the native timed-text hook installed across YouTube Home/Search → watch SPA navigation while capturing only on `/watch`.
- Bind each word lookup/feedback response to an immutable panel-selection token so two `bank` contexts cannot mix key, gloss and sentence.
- Abort stale learning activation after every await; a close during bootstrap cannot start a later import, and a prepare response arriving after close is immediately cancelled.
- Prevent an old video bridge from switching or restoring caption tracks after SPA navigation to a new video.
- Make player visibility a hard prerequisite for the 8-second learning gate and teaching pause; scrolling away clears pause ownership and cannot resume playback invisibly.
- Stream and cancel oversized response clones before buffering; cap payloads at 5 MB, 50,000 events and 10,000 segments per event.
- Recover a same-owner stale interaction as a technical failure; an explicit cross-tab claim now closes its orphan server-side before ownership transfers, so the claimant cannot convert it into completed/known evidence.
- Restart an in-flight subtitle task when learning is turned off instead of retaining a dead Promise.
- Do not let an advertisement consume the one automatic subtitle retry.
- Settle definitive no-caption failures within five seconds instead of automatically running a second four-second attempt.
- Remove the unsupported three-hour standalone-caption cutoff; keep a 24-hour sanity bound while payload limits remain authoritative.
- Include adaptive `reason` and `last_changed_at` in profile rebuild comparison so behaviorally different profiles cannot report a false match.
- Never stamp an old profile with a new reducer version without replay: server startup backs up and replays older ledgers, while missing events/content or a future reducer fails closed.
- Require the same cross-process `server.lock` before `rebuild_profile --write` or `--migrate-reducer`, preventing compare/write from overwriting concurrent API events.
- Keep replayed/assisted probes as practice: record the used variant and practice evidence without moving memory windows.

### Distribution

- Default optional-learning translation to local Argos. Google translation now requires explicit `INFLOW_TRANSLATION_BACKEND=google` configuration and matching privacy disclosure.
- Replace padded Store art with direct 1280×800 full-viewport captures.
- Correct the Store privacy draft to disclose local Web history, website content and user-activity processing.
- Add a separate `-CWS-first-upload.zip` build that strips the development-only manifest `key` required to pass a brand-new Chrome Web Store upload.
- Keep the development ZIP and fixed local test ID unchanged.
- Build and verify both archives in GitHub Actions.

### Versioning

- Extension `0.2.6`.
- Reducer `rules-v6`.

## 0.2.5 — 2026-09-04

### Fixed in signed-in Chrome

- Capture the YouTube player's successful native timed-text responses at document start instead of re-fetching one-time/PO-bound URLs.
- Keep the capture cache in the current tab only: six entries, 5 MB each, two-minute TTL.
- Switch briefly to missing English/Chinese tracks through the player, capture their native responses, then restore the user's prior track.
- Add a hard Promise timeout so a hung response stream cannot suppress another usable track.
- Align each English cue to one closest Chinese row instead of concatenating overlapping rolling captions from neighbouring sentences.
- After a paused/background open fails, retry captions once automatically when playback first starts.
- Reset expected play/pause ownership counters across navigation.

### Real Chrome evidence

- `arj7oStGLkU`: visible bilingual InFlow caption in the signed-in Chrome profile.
- `iG9CE55wbtY`: visible bilingual caption after native-response capture; English and Chinese remained within the same sentence scope.
- The 0.2.5 card was read back from `chrome://extensions` before testing.

## 0.2.4 — 2026-09-04

### Fixed

- Preserve a newer long-video focus epoch when an older in-flight window finishes.
- Hard-cap ProgressivePack prefetch to one adjacent window even if a caller requests more.
- Remove DSH/DeepSeek from the default VideoPack builder; unresolved senses remain provisional.
- Add an explicit OpenAI-compatible GPT adapter without reusing Hermes/Codex credentials.
- Make public CI distinguish four intentionally private-media audits from distributable tests.
- Remove private fixture assumptions from context, ProgressivePack and subtitle-first browser contracts.

### Verified

- A tracked-files-only export passes 156 unit tests with four declared private-fixture skips.
- Standalone and closed-shadow browser contracts pass from that public export.
- Anonymous real YouTube currently returns `LOGIN_REQUIRED`; signed-in Chrome remains an explicit release gate.

## 0.2.3 — 2026-09-04

### Standalone captions

- Mount the visible status at `document_start`; it is no longer a 10×10 hover-only dot.
- Read the current YouTube player's caption tracks through a bounded page bridge.
- Remove `yt-dlp` metadata and localhost from the caption first-paint path.
- Work without a Python backend or local-service permission.
- Stop missing-track failures within five seconds and provide an in-page retry.
- Handle advertisements and same-document YouTube navigation without stale captions.

### Playback and learning

- Keep captions on when experimental learning is disabled.
- Make automatic teaching pause an explicit opt-in, off by default.
- Start the eight-second intent gate only after opt-in and only during visible, non-ad continuous playback.
- Replace mandatory three-state teaching feedback with `看清了，继续`; keep replay separate.
- Add the optional, reversible `这个义项以后不用解释` known override.
- Use the current caption bundle's Chinese sentence on the teaching card.
- Circuit-break failed imports until a user explicitly retries.
- Require explicit cross-document session takeover.

### Security and reliability

- Use a closed production shadow root and trusted-input guards.
- Split popup/content message capabilities and validate top frame, document ID and video URL.
- Move session owner IDs to per-document `chrome.storage.session` state.
- Make localhost an optional host permission requested only for experimental learning.
- Add loopback Host/Origin/Fetch-Metadata checks, JSON content type and 32 KiB write limits.
- Replace the broad `/media` mount with an exact fixture allowlist.
- Serialize expensive work to one worker with four pending slots.
- Add stat-fingerprint caching for verified pack manifests.
- Add bounded launcher logging and public-data exclusions.
- Advance the profile reducer to `rules-v5` so replay counts project independently from familiarity.

### Distribution

- Add deterministic allowlist ZIP builds with SHA-256 output.
- Add CI, privacy/security/install/troubleshooting documentation, issue templates and Chrome Web Store listing assets.
- Remove personal paths and runtime data from the public candidate tree.

### Known limits

- Chrome Web Store review has not been submitted/approved.
- Real YouTube reliability still needs a broader representative matrix.
- The optional Python learning companion has no signed consumer installer.
- Long-video active learning remains disabled in production.
- No durable learning-effect claim is made.
