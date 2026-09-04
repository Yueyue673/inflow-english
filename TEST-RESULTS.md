# Verification record — InFlow English 0.2.6

_Verified: 2026-09-05. This file records observed engineering results, not learning-effect claims._

## Release identity

```text
product              inflow-english
extension            0.2.6
policy               adaptive-v5
profile schema       3
reducer              rules-v6
VideoPack builder    video-pack-builder/1.9.2
Progressive builder  progressive-pack-builder/0.2.0
lexical catalogue    oewn:2024
```

Production active learning for long videos remains disabled.

## Local candidate unit suite

```text
Ran 176 tests in 23.090s
OK
```

The suite now covers, among the existing pack, server, security and event tests:

- Home/Search → watch SPA installation of the native timed-text hook;
- same-surface concurrent word lookup/feedback responses bound to immutable selection identities;
- stale bootstrap/prepare activation unable to create work or overwrite a closed state;
- old-video page bridges unable to switch/restore tracks or publish success after SPA navigation;
- hidden players unable to mature the learning gate, pause or silently resume;
- streaming cancellation above 5 MB, 50,000-event and 10,000-segment limits;
- a named page-bridge worst-case budget below the 4,000 ms content RPC deadline;
- transient `player.getOption()` failure and four-hour caption metadata;
- no automatic second attempt for definitive missing-track failures;
- advertisements not consuming the one permitted play retry;
- clearing and restarting an invalidated in-flight subtitle task;
- same-owner recovery of a persisted open interaction without familiarity evidence, plus server-side technical closure before explicit cross-tab ownership transfer;
- newly resolved subtitle keys accepting their server-recomputed exact identity;
- visible word-state consequence plus append-only undo to the exact prior scheduling state;
- assisted/replayed probes not moving memory windows;
- adaptive `reason` and `last_changed_at` included in rebuild comparison;
- old reducers backed up and replayed instead of relabelled, with cross-process migration lock coverage;
- local Argos as the default translation backend and Google available only through explicit opt-in;
- separate development and first-Chrome-Web-Store-upload archives.

Four private-media human/audit tests remain conditionally skipped in a public checkout because those media are intentionally not committed. Their public synthetic contracts are covered.

## Browser contracts

All browser contracts used isolated temporary profiles and data directories, real extension loading, muted audio and a guard proving the formal `data/live` tree was unchanged.

### Standalone captions — no localhost permission

```text
host visible                         63 ms
bilingual caption ready             108 ms
paused missing-track error         4578 ms
playing missing-track error        4579 ms
definitive failure auto-retried     false
first-play recovery                  312 ms
same-document SPA switch              12 ms
status geometry                       124.94 × 32 px
status dock                           side; 10 px beyond video edge
viewport hide → return               passed
```

Observed:

- captions and sentence replay work without localhost permission or Python;
- on a wide viewport the status pill is outside the video rectangle;
- ads hide captions and block sentence replay;
- stale captions do not survive same-document navigation;
- a definitive no-track result settles within five seconds instead of running a second full attempt;
- a later user play event may still recover once if tracks become available;
- no backend caption job is created.

### Closed production boundary

```text
closed shadow root           true
page-private state exposed   false
synthetic keyboard blocked   true
accessible status            InFlow 字幕已就绪
```

### Native one-shot timed-text response

The fixture started on YouTube Home, then performed same-document navigation to `/watch`. Each English/Chinese native request succeeded only once and every duplicate returned an empty body. The document-lifetime hook still published:

```text
The native response is captured once.
原生响应只捕获一次。
```

### Optional learning and interaction UI

```text
automatic learning permission gate         passed
continuous-play gate to working             8199 ms
wide-layout status/video overlap             none
wide-layout teaching/video overlap           none
teaching side panel                           300 × 498 px
mapping actions visible without scrolling    true
natural-phrase interaction completed         1
mandatory familiarity write                  none
manual replay count                          1
subtitle feedback visible and undoable       true
learning-off preserved captions              true
profile rebuild matched event ledger         true
page / worker errors                          0 / 0
```

The mapping stage exposes `看清了，继续`, `再听一遍`, `以后跳过此义项` and `跳过这次`. The word panel keeps the saved consequence visible and offers `撤销`; it no longer disappears immediately after a write. `关闭本视频` and `开启本视频` were exercised as an inverse round trip.

### Context-scoped lexical identity

Two visible occurrences of `bank` produced separate provisional keys:

```text
river-bank context       known
financial-bank context   familiar
keys distinct            true
feedback result visible  true
```

Changing the financial occurrence did not mutate the river occurrence.

### ProgressivePack fixture

```text
first revision                 1
second revision                2
initial items                  1
items after adjacent delta     2
completed interactions         1
full video stored              false
page / worker errors           0 / 0
```

This verifies current-window publication, one adjacent-window delta, no full-video artifact, a hard one-window prefetch cap and stale-epoch isolation. It does not prove current real-YouTube long-video transport.

### Subtitle before learning

```text
page caption ready before learning    true
backend caption jobs                  0
caption source                        youtube_page_player
learning imports before playback      0
learning import after continuous gate true
input shortcut protection             true
```

## Formal local service migration and read-back

A complete copy was created under the ignored `data/backups/pre-0.2.6-*` tree. Raw rules-v5 profile versus rules-v6 replay produced exactly one difference:

```text
reducer_version   rules-v5 → rules-v6
```

No item, lexicon or adaptive field changed. The 160-event ledger retained SHA-256:

```text
28b48f04ea1665b86e657cfda5fa63237416ea750c0cf7d611beb510f2fbb2fa
```

After migration and restart:

```text
product                  inflow-english
extension                0.2.6
profile reducer          rules-v6
schema                   3
WordNet warmup           ready
events                   160
pending transactions     0
heavy queue depth        0
progressive learning     false
rebuild mismatches       0
concurrent --write       refused: data_directory_in_use
```

## Release archives

The exact 12-file allowlist builder produced two verified archives:

```text
InFlow-English-Chrome-0.2.6.zip
SHA-256 fcfca3a1ecc55ee46af6e7d830d846df10964666590e048c9cd9664396467ea0
manifest.key present   true

InFlow-English-Chrome-0.2.6-CWS-first-upload.zip
SHA-256 9b8b511a5100e215c36bb4515e6e43e6b8a95f8b6e9ad287e2a801080ccf179a
manifest.key present   false
```

Both archives contain 12 members and `ZipFile.testzip()` returned no bad member. The second package is the only candidate for a brand-new Chrome Web Store item. Google must assign its Store ID before that origin can be added to `INFLOW_ALLOWED_EXTENSION_ORIGINS`.

## Product screenshots

The README and Store assets were regenerated from the browser runs after the UI changes:

- status/word surface docked beside the video;
- saved word result and `撤销` visible;
- teaching card docked beside the video;
- all four teaching actions visible in a fixed action area.

The Store assets are direct 1280×800 full-viewport captures with square corners. They are byte-for-byte copies of the corresponding post-fix browser screenshots: no padding, stretching, cropping or rounded-corner frame was added.

## Real Chrome and real YouTube boundary

### Existing signed-in Chrome — historical 0.2.5 transport proof

Version 0.2.5 previously produced visible two-line captions on `arj7oStGLkU` and `iG9CE55wbtY`, including a measured 2379 ms post-play caption attempt on the latter. That remains evidence for the signed-in native-response transport introduced in 0.2.5; it is not relabelled as a 0.2.6 run.

### 0.2.6 installed-path checks

- The actual Chrome extension card was reloaded and read back as `0.2.6`.
- An isolated current 0.2.6 Chromium page loaded the real YouTube player and extension host.
- The anonymous player then hit YouTube's own `LOGIN_REQUIRED` / “请登录，以便我们确认你不是聊天机器人” gate, so no 0.2.6 real-caption success is claimed from that run.
- The visible 0.2.6 status stayed beside the player rather than covering it.
- The user's test tab suffered a one-off blank YouTube renderer while both no-extension and 0.2.6 isolated controls loaded the same player; the tab was returned to a blank New Tab page. This is not counted as a product pass or failure.

## Public staged-checkout gate

The final staged index was exported to a clean directory containing 108 tracked public files and no working-tree-only files. From that export:

```text
Ran 176 tests in 23.203s
OK (skipped=4)
standalone caption browser     passed
closed-shadow security         passed
native one-shot + Home→watch   passed
development archive            passed; hash matched local build
CWS first-upload archive       passed; hash matched local build
```

A programmatic scan found zero matches for the developer's absolute Windows/QA paths, credential-shaped OpenAI/GitHub/Google/bearer/private-key values, and zero broken relative Markdown links. Both Store screenshots are direct 1280×800 captures byte-for-byte identical to their documentation images. The stale `Web history: not collected` statement is absent; Web history, User activity and Website content are disclosed for core functionality, and the popup discloses local URL/activity handling plus the offline translation default.

GitHub Actions is verified live after push and is intentionally not frozen into this pre-push file; the workflow itself runs the same unit, standalone, closed-shadow, native one-shot and dual-archive gates.

## Explicitly not passed

- 0.2.6 signed-in real-caption replay after the final installed reload;
- a user-authorized real 8-second learning interaction from the installed surface;
- representative real no-caption, network-loss, low-resource and endurance matrices;
- real long-video 60 s / 5220 s learning transport after a lawful source path;
- Chrome Web Store approval and one-click automatic updates;
- a signed optional-backend installer;
- 24-hour or seven-day retention, transfer to a new speaker/context, improved completion rate or repeated voluntary use.

## Product-value boundary

The verified value is immediate caption/replay assistance, corrected interaction semantics and safe learning-interaction transport. These tests do not demonstrate durable vocabulary learning or mastery.
