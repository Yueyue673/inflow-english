# InFlow English 0.2.6 — interaction and recovery correction

This release responds to real-use feedback that the UI obscured the video, open/close behavior was unpredictable, and word-state buttons appeared to do nothing.

## User-visible changes

- On normal wide YouTube pages, the status pill, status panel, word panel and teaching card dock beside the video instead of covering it.
- Theater and fullscreen modes retain a compact in-video fallback because no side rail exists.
- Status and word panels are mutually exclusive. Clicking the page or pressing Escape closes a transient panel.
- The page surface and popup now use the same exact inverse actions: `关闭本视频` and `开启本视频`.
- Word-state edits remain visible after saving and explain their consequence:
  - `听到就懂 · 跳过`
  - `有点熟 · 少出现`
  - `还不清楚 · 早点出现`
- `撤销` appends an explicit undo event; a server-owned one-step snapshot restores the exact preceding lexical and occurrence scheduling state instead of clearing prior evidence.
- Newly looked-up WordNet words can now save feedback; the previous exact/provisional key mismatch returned HTTP 422.
- The teaching mapping stage has a visible `跳过这次` action. Its actions remain visible while explanatory content scrolls.
- The bilingual caption background is lighter, smaller and closer to the lower control-safe edge.
- Fixed surfaces disappear when the player leaves the usable viewport and return with it; an active teaching step scrolled away ends as a technical interruption without resuming playback behind the user.

## Caption reliability

- Same-surface word lookups and feedback are bound to immutable selection tokens, so two contexts of `bank` cannot combine the first key with the second sentence.
- Closing a video or learning mode invalidates every in-flight activation step; no delayed bootstrap may create an import or replace the closed message.
- A page bridge rechecks URL and player identity before every temporary track switch, restore and final result, so video A cannot alter video B after SPA navigation.
- Hidden/out-of-viewport players cannot mature the eight-second gate or begin a teaching pause; scrolling away clears pause ownership.
- Home/Search → watch same-document navigation now keeps the native timed-text capture hook installed.
- A caption response clone is streamed and cancelled above 5 MB instead of being fully buffered first.
- Caption payloads are bounded to 50,000 events and 10,000 segments per event.
- A temporary `player.getOption()` failure no longer kills the entire bridge.
- Redundant second track-switch waits were removed; the worst named bridge budget remains below the content RPC deadline.
- A definitive missing-track result settles within five seconds and does not automatically repeat. A later user play event may still trigger one retry.
- Advertisements cannot consume that one retry.
- Standalone captions accept videos longer than three hours, with a 24-hour sanity bound and the existing payload limits.

## Learning-state integrity

- Same-owner reload closes a persisted orphan interaction as `technical_failure: recovered_stale_interaction`. Explicit cross-tab claim closes it server-side as `technical_failure: owner_claimed` before transferring ownership; neither path can write familiarity evidence.
- Turning learning off while captions are loading clears the invalidated task and starts one fresh subtitle request.
- Assisted/replayed probes remain practice and no longer postpone the memory window.
- Profile rebuild comparison includes adaptive `reason` and `last_changed_at`; server startup backs up and replays any older supported reducer instead of stamping its version, and fails closed if events or pack content are incomplete.
- `rebuild_profile --write` and `--migrate-reducer` require the same data-directory lock as the server, so a stale snapshot cannot overwrite a concurrent learning event.
- Reducer version is `rules-v6`.

## Privacy and Store packaging

- Optional-learning translation now defaults to local Argos. Google requires explicit `INFLOW_TRANSLATION_BACKEND=google` configuration.
- The privacy draft discloses local processing of the current watch URL/video ID, caption content, playback timing and explicit learning actions.
- Store screenshots are direct 1280×800 full-viewport captures with no added padding.

## Chrome Web Store package

Two archives are attached:

- `InFlow-English-Chrome-0.2.6.zip` — development/source-alpha package with the fixed development key.
- `InFlow-English-Chrome-0.2.6-CWS-first-upload.zip` — first Store upload package with `manifest.key` removed.

For a new Chrome Web Store item, upload only the `-CWS-first-upload.zip` asset. After Google assigns the Store ID, add `chrome-extension://<STORE_ID>` to `INFLOW_ALLOWED_EXTENSION_ORIGINS` before testing the optional local service.

## Boundaries

This remains a public source alpha until Chrome Web Store approval. Long-video active learning remains disabled. Passing caption and interaction tests does not demonstrate delayed vocabulary retention or transfer.
