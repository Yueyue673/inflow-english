# Verification record — InFlow English 0.2.5

_Verified: 2026-09-04. This file records observed engineering results, not learning-effect claims._

## Release identity

```text
product              inflow-english
extension             0.2.5
policy                adaptive-v5
profile schema         3
reducer               rules-v5
VideoPack builder      video-pack-builder/1.9.2
Progressive builder    progressive-pack-builder/0.2.0
lexical catalogue      oewn:2024
```

Production active learning for long videos remains disabled.

## Clean-environment unit suite

A fresh CPython 3.11 virtual environment was created from `requirements/ci.txt`, Open English WordNet 2024 was installed, and the staged Git index was exported to a new directory containing **only tracked public files**. The suite was run from that export.

```text
Ran 158 tests in 21.345s
OK (skipped=4)
```

The four explicit skips are private-media human/audit fixtures that are intentionally excluded from the repository. Their public synthetic contracts remain covered; the private copies also pass on the development machine.

The suite covers:

- strict YouTube/source validation and immutable pack integrity;
- natural cue construction, caption alignment and compound-word boundaries;
- exact/provisional lexical identity and illegal model-ID rejection;
- GPT-compatible adapter isolation and no-model default behaviour;
- profile schema migration, transaction replay and idempotent events;
- replay counts independent from familiarity state;
- explicit known override and later reversal;
- session ownership and explicit cross-document claim;
- loopback Host/Origin/Fetch-Metadata/content-type/body-size boundaries;
- media allowlists, path traversal and symlink rejection;
- one-heavy-worker/four-pending resource limits;
- failed-import circuit breaking and explicit retry;
- launcher log rotation and pack verification-cache invalidation;
- extension manifest, sender allowlists and release-file contracts.

## Browser contracts

All browser contracts used isolated temporary profiles and data directories, a real extension load, muted audio, and a guard proving the formal `data/live` tree was unchanged.

### Standalone captions — no localhost permission

```text
host visible                 195 ms
bilingual caption ready      1560 ms
missing-track error          4581 ms
automatic play retry ready   770 ms
same-document SPA switch     579 ms
status geometry              124.94 × 32 px
in-script status metric      5 ms
in-script retry attempt      465 ms
```

Observed:

- extension works without localhost permission or Python;
- Chinese-primary and English-secondary caption hierarchy is visible;
- ads hide captions and block sentence replay;
- stale captions do not survive same-document navigation;
- missing tracks stop within five seconds;
- a paused/background open retries once automatically on first playback;
- no backend caption job is created.

### Closed production boundary

```text
closed shadow root             true
page-private state exposed     false
synthetic keyboard blocked     true
accessible status              InFlow 字幕已就绪
```

### Native one-shot timed-text response

The browser fixture served each English/Chinese native request successfully only once, then returned an empty body to every duplicate request. The document-start hook still published:

```text
The native response is captured once.
原生响应只捕获一次。
```

This regression fails if the extension returns to re-fetching one-time/PO-bound timed-text URLs.

### Optional learning path

```text
automatic learning default/permission gate  passed
continuous-play gate to working             8106 ms
natural-phrase interaction completed        1
mandatory familiarity write                 none
manual replay count                         1
learning-off preserved captions             true
profile rebuild matched event ledger        true
page / extension worker errors              0 / 0
```

The card used `看清了，继续`, `再听一遍`, and optional `这个义项以后不用解释`. The Chinese sentence shown in the card matched the active caption bundle.

### Context-scoped lexical identity

Two visible occurrences of `bank` produced different provisional keys:

```text
river-bank context       known
financial-bank context   familiar
keys distinct            true
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

This verifies current-window publication, one adjacent-window delta, no full-video artifact, a hard one-window prefetch cap, and that an in-flight old window cannot overwrite a newer seek focus epoch. It does not prove current real-YouTube long-video transport.

### Subtitle-before-learning timing

```text
page caption ready before learning    true
backend caption jobs                  0
caption source                        youtube_page_player
learning imports before playback      0
learning import after continuous gate true
input shortcut protection             true
```

## Formal local service read-back

After backing up and replaying the 160-event formal ledger:

```text
health latency           4.8 ms
product                  inflow-english
WordNet warmup           ready
profile reducer          rules-v5
schema                   3
events                   160
pending transactions     0
heavy queue depth        0
progressive learning     false
```

The migration changed only 20 `replay_count` projections that were already present in historical interaction events. `events.jsonl` retained SHA-256:

```text
28b48f04ea1665b86e657cfda5fa63237416ea750c0cf7d611beb510f2fbb2fa
```

A complete pre-migration copy was created under the ignored local `data/backups/` tree.

## Release archive

`python tools/build_extension.py --output-dir dist` produced an exact 12-file allowlist archive:

```text
InFlow-English-Chrome-0.2.5.zip
SHA-256 149751f6d76bb885cb58458ea6ba9fbd88a23bceb7da65881f3dfb22d6a2ab4c
```

Archive verification returned no bad member. Its manifest exactly matched `extension/manifest.json`:

```text
required permission        storage
required host              https://www.youtube.com/*
optional host              http://127.0.0.1:8767/*
```

## Public-source leak gate

The staged candidate contained 106 files and no match for:

- the developer's Windows username or QA-drive path;
- credential-shaped OpenAI/GitHub/bearer values;
- runtime data, events, profiles or backups;
- videos, audio, logs or ZIP archives;
- the local DSH model patch or personal launcher.

All public Markdown relative links resolved.

## Explicitly not passed

### Real anonymous YouTube

System Chrome in an isolated anonymous profile returned:

```text
playability  LOGIN_REQUIRED
reason       请登录，以便我们确认你不是聊天机器人
caption tracks 0
```

Three anonymous `yt-dlp` metadata attempts also failed with `yt_dlp_inspect_failed`. This is an external anti-bot/login boundary, not a passing compatibility result.

### Existing signed-in Chrome

The unpacked extension card was read back as `0.2.5`, then two ordinary watch pages were exercised through native Chrome UI/AX without DevTools access:

```text
arj7oStGLkU   visible two-line InFlow caption
               status: InFlow 字幕已就绪

iG9CE55wbtY   visible two-line InFlow caption
               status visible: 11 ms
               post-play caption attempt: 2379 ms
```

A screenshot inspection verified that the second video's visible English and Chinese lines covered the same sentence:

```text
despite all the expertise that's been on parade for the past four days,
儘管我們在過去四天中探討了各種專業知識—
```

The videos were muted during QA. The route-to-ready value from the second page included several minutes of deliberate diagnostic waiting and is intentionally excluded from latency claims.

The remaining release gates are:

- no-caption, network-loss, browser-restart and low-resource cases on representative real videos;
- real long-video 60 s / 5220 s transport after a lawful, non-cookie-dependent source path;
- Chrome Web Store approval;
- a signed optional-backend installer;
- a user-owned real learning interaction and restart recovery from the final install surface.

## Product-value boundary

The verified value is immediate caption/replay assistance and one safe learning-interaction transport. This record does **not** demonstrate 24-hour retention, seven-day retention, cross-speaker transfer, improved completion rate or repeated voluntary use.
