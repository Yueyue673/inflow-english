# State matrix

| State | Evidence | User-facing message | Next event | User action | Safe fallback |
|---|---|---|---|---|---|
| Unsupported page | No valid `/watch?v=<11 chars>` | No InFlow surface | Navigate to a watch page | None | Do nothing |
| Starting | Content script at `document_start` | `InFlow 字幕准备中` | Player response or timeout | Open status | Keep YouTube untouched |
| Advertisement | `#movie_player.ad-showing` | `InFlow 等正片` | Ad exits | None | Hide InFlow captions; reset 8-second gate |
| Captions prepared during ad | Valid timed-text payload + ad active | `InFlow 已备好 · 等正片` | Ad exits | None | Keep captions hidden until content |
| Captions ready | Visible timed cue for current video generation | `InFlow 字幕已就绪` | Playback, seek or SPA navigation | Replay current sentence; click a phrase | Local learning may remain offline |
| Local service offline | Page captions ready; bootstrap timeout/failure | `字幕已就绪；本机学习服务未连接` | Service later responds | Keep watching | Caption and replay continue |
| No supported tracks | Page-caption budget expired | `InFlow 需要查看` + exact safe error code | Track metadata changes or user retries | `重试字幕` | Settle within five seconds; do not automatically repeat a definitive `tracks_missing` failure |
| Automatic learning off | `autoLearning !== true` | `字幕已就绪；自动教学暂停未开启` | User opts in | Toggle experimental learning | Never create a learning job |
| Learning intent gate | Opted in + visible, playing, non-ad content | Caption remains visible | Eight continuous seconds | Pause/seek/hide cancels gate | Reset timer; no long-term preference change |
| Preparing learning | Local import/session work admitted | `InFlow 学习准备中` + factual stage | READY, failed or cancelled | Pause this video / cancel | Never pause source video |
| Heavy queue full | Shared worker has 1 active + 4 pending | Retryable `heavy_queue_full` | Capacity becomes available | Retry later | HTTP 429 + `Retry-After: 5`; no orphan job |
| Previous learning failure | Failed job for same canonical URL | `不会自动重试` | Explicit retry | `重试学习` | Return existing failure; no duplicate work |
| Session owned elsewhere | `owner_conflict=true` for another document ID | `另一个标签页正在运行` | User claims or leaves it | `接管学习` | Do not change owner epoch |
| Orphan interaction | Active watch session returns persisted `open_interaction` | `正在收尾上次中断的学习步骤` for same owner | Same-owner technical close or explicit claim | `接管学习` only on owner conflict | Claim closes old lease as `owner_claimed` before transfer; claimant cannot submit success/familiarity for it |
| Learning active | Session owner lease valid; assets preloaded | `InFlow 正在工作` | Candidate safe boundary | Turn automatic learning off | Captions stay active |
| Teaching | Plugin caused pause and still owns lease | Natural audio + phrase + current sense | User continues/replays/opts out/skips | `看清了，继续`, `再听一遍`, `以后跳过此义项`, `跳过这次` | Dock beside video when space exists; no auto-dismiss; no required familiarity rating |
| Subtitle word state | User explicitly chooses a state | Saved consequence remains visible | User closes, changes state or undoes | `听到就懂 · 跳过`, `有点熟 · 少出现`, `还不清楚 · 早点出现`, `撤销` | Append-only undo restores the server-owned preceding lexical and scheduling state; never infer mastery from the click |
| Player outside viewport | A previously located video has less than 160×90 visible | No fixed InFlow surface | Scroll/resize returns player | None | Hide stale geometry; active teaching ends as `player_not_visible` without auto-resume |
| User takes playback | Trusted play/pause/seek/navigation/hide/ad event | Interaction ends | Normal playback | None | Revoke resume lease; record technical reason, not failure |
| SPA navigation | URL/player video ID changes | New video loading state | New captions or error | None | Increment generation; late old work ignored |
| Manual video pause | Current video explicitly disabled | `InFlow 已暂停` | New video or user enables | `开启本视频` | `关闭本视频` and `开启本视频` are exact inverses; auto mode cannot revive the closed video |
| Long video learning disabled | Production feature flag false | Captions still available | Future quality gate | None | Do not start ProgressivePack learning |

## State invariants

- `subtitleActive` and `enabled` are separate dimensions.
- `autoMode=true` authorizes automatic captions only.
- `autoLearning=true` is required before any automatic learning preparation.
- A visible error is terminal until a factual state change or explicit retry.
- Old route generations, owner epochs and interaction IDs cannot mutate the current state.
- Only a plugin-caused `playing → paused` transition creates a resume lease.
- Status, word and teaching surfaces are mutually exclusive where they share a dock; user-visible actions always have a visible exit.
