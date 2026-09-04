# Optional local learning service contract

The FastAPI service at `127.0.0.1:8767` is an **experimental companion**, not a prerequisite for InFlow captions.

## Entry condition

The extension may contact this service only after the user enables experimental automatic learning and grants Chrome's optional loopback permission.

## Responsibilities

- enumerate and verify immutable VideoPacks;
- prepare natural-phrase learning material outside the caption first-paint path;
- maintain per-document session ownership;
- append versioned events and rebuild the derived profile;
- serve validated caption/transcript/phrase assets;
- serialize expensive import, caption and offline-translation work.

## Network boundary

- Bind only to `127.0.0.1`.
- Acquire the data-directory `server.lock` before transaction/import recovery; a losing process performs zero writes.
- If `profile.json` is missing while events exist, rebuild from the ledger; missing historical item data fails closed.
- Accept only the configured loopback Host.
- Allow browser Origin only from the local UI, fixed development extension ID, or Store origins explicitly configured in `INFLOW_ALLOWED_EXTENSION_ORIGINS`.
- Reject other cross-site browser requests.
- Require `application/json` for writes and cap JSON bodies at 32 KiB.
- Do not enable wildcard CORS.

## Resource boundary

```text
1 active heavy worker
+ at most 4 pending heavy tasks
```

Overload returns `429 heavy_queue_full` with `Retry-After: 5`. A rejected request must not leave a queued job or staging directory.

A canonical video URL that already failed is circuit-broken. Automatic requests read the existing failure; only `force_retry:true` from an explicit user retry creates a new job.

## Data

New installs default to `%LOCALAPPDATA%\InFlow-English\`. A development checkout with an existing legacy `data/live` continues using that directory in place; no automatic move or deletion occurs.

Persistent state:

```text
events.jsonl          append-only facts
profile.json          replayable derived projection
sessions/*.json       per-video state and owner lease
transactions/*.json   recoverable multi-file writes
import-jobs/*.json    bounded preparation state
```

Downloaded media, packs, logs and user state are excluded from the public repository and extension archive.

## Ownership

Each YouTube top-level document receives a session-scoped random `client_id`. A session stores `owner_client_id + owner_epoch`.

- Same owner may continue without incrementing the epoch.
- Another document receives `owner_conflict=true` and cannot write.
- A trusted `接管学习` action calls claim and increments the epoch.
- The old document's later writes are rejected.

## Long videos

ProgressivePack may prepare only the current focus window and one adjacent prefetch window. Production active learning remains disabled until real-window quality and latency gates pass. Standalone captions remain available.

## Non-goals

The service is not a remote multi-user API, a cloud synchronization server, a public media host or a proof of learning effectiveness.
