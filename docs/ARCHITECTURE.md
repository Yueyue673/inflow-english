# Architecture

## Two independent paths

### 1. Standalone caption path

```text
YouTube player response (MAIN world)
        │ exact video ID + caption-track metadata
        ▼
page-bridge.js
        │ exact YouTube timed-text hosts/path
        │ credentials omitted, redirects rejected
        │ 3.2 s / 5 MB streaming bounds
        ▼
content-script.js (isolated world)
        │ validate video generation + parse JSON3
        ▼
closed Shadow DOM
        ├─ visible status
        ├─ Chinese primary line
        ├─ English supporting line
        └─ current natural-segment replay
```

This path does not call localhost, `yt-dlp`, ASR, WordNet or a translation model before caption paint. It is the public product slice.

### 2. Optional local learning path

```text
explicit user opt-in + optional localhost permission
        │
8 s continuous visible, non-ad playback
        ▼
MV3 service worker allowlist
        │
127.0.0.1:8767 FastAPI
        ├─ immutable VideoPack cache
        ├─ one heavy worker + four pending tasks
        ├─ per-document owner lease
        ├─ exact/provisional lexical identity
        └─ append-only events → replayable profile
        │
READY natural phrase + safe pause boundary
        ▼
content script teaching interaction
```

The local path may enrich the product, but it cannot delay or break the standalone caption path.

## Extension responsibilities

### `page-bridge.js`

Runs in the page's MAIN world because YouTube player response objects are not visible from the isolated content-script world. It receives one nonce and one expected video ID, validates the matching player response, fetches bounded timed-text payloads and returns them once through `window.postMessage`.

It never accesses the Chrome extension APIs or localhost.

### `content-script.js`

- mounts at `document_start`;
- owns the visible state machine and closed shadow root;
- validates bridge nonce, origin, video ID and route generation;
- binds captions and controls to the actual `<video>` rectangle;
- handles ads, seek, page visibility, SPA navigation and playback leases;
- holds page captions in memory;
- never infers mastery.

### `service-worker.js`

- maps finite message types to finite loopback endpoints;
- separates popup and top-frame YouTube sender capabilities;
- binds URL-bearing requests to the sender's current video;
- generates a per-document writer identity in `chrome.storage.session`;
- performs no arbitrary URL/method/path proxying.

## Local service responsibilities

- validates Host, Origin, Fetch Metadata, content type and request size;
- serializes heavy work and bounds pending demand;
- verifies pack integrity and caches verification only while file stat fingerprints remain unchanged;
- stores events transactionally and rebuilds derived state;
- serves only allowlisted fixed media and validated pack assets;
- binds only to loopback.

## Optional model backend

The default VideoPack builder does **not** invoke DSH, DeepSeek, GPT or any exact-sense model. Translation uses the bounded Google/Argos fallback chain; an unresolved sense remains occurrence-scoped provisional.

A developer may explicitly select an OpenAI-compatible GPT endpoint:

```text
INFLOW_MODEL_BACKEND=openai
INFLOW_OPENAI_CHAT_COMPLETIONS_URL=https://…/v1/chat/completions
INFLOW_OPENAI_MODEL=<model exposed by that endpoint>
INFLOW_OPENAI_API_KEY=<secret, environment only>
```

The endpoint must be HTTPS unless it is loopback. The key is never placed in prompts, manifests, identities or error messages. Invalid or low-confidence candidate IDs are rejected by trusted code and fall back to provisional identity.

`INFLOW_MODEL_BACKEND=dsh` exists only as an explicit legacy developer adapter. It is never selected by default.

## Lexical identity

```text
Reliable resolved sense
→ lexeme + POS + Open English WordNet sense
→ stable cross-context key

Unresolved or ambiguous occurrence
→ phrase/context fingerprint + occurrence identity
→ provisional key
```

Chinese wording and surface spelling are display data, not stable knowledge identity.

## Evidence classes

- Caption exposure with Chinese support: `GIST_ONLY`.
- Completed mapping interaction: `TEACH`, low confidence.
- Manual replay: behavior count only.
- Teaching-card continue: no familiarity write.
- `这个义项以后不用解释`: reversible `EXPLICIT_KNOWN_OVERRIDE`.
- Subtitle word-panel state edit: explicit state edit.
- Immediate or answer-exposed self-report: not clean evidence.
- Delayed three-choice success: at most `WEAK_SUCCESS`.

## Long videos

ProgressivePack targets approximately six-minute windows with analysis overlap. Candidates belong to one ownership window. Only the current focus window and one adjacent prefetch window may be prepared; a monotonically increasing focus epoch invalidates stale scheduling after seek.

Production active learning for long videos remains off until real-window cue quality and latency gates pass. Standalone captions do not share that restriction.
