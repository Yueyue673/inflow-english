# Changelog

All notable public changes are documented here.

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
