# InFlow English · Chrome extension contract

## Identity

- Manifest V3
- Version `0.2.6`
- Minimum Chrome `120`
- Fixed development ID `hfkkhkdpakcmpokgbihceoppleeokifd`
- Required permission: `storage`
- Required host: `https://www.youtube.com/*`
- Optional host: `http://127.0.0.1:8767/*`

## Public path

```text
document_start visible status + bounded native-response hook
→ MAIN-world player response bridge
→ captured exact YouTube timed-text payload (bounded same-origin fallback)
→ isolated content validation
→ closed Shadow DOM captions and replay
```

The extension must remain useful when localhost is absent. Page-caption first paint cannot depend on pack enumeration, `yt-dlp`, ASR, WordNet, translation or learning-session state.

## Security boundary

- `page-hook.js` installs once at document start in YouTube's MAIN world so Home/Search → watch SPA navigation remains covered. It copies responses only while the current route is `/watch`, keeps at most six valid player timed-text responses for two minutes, and never uses Chrome APIs or localhost.
- `page-bridge.js` receives one nonce and one expected video ID.
- It accepts only three exact YouTube hosts, `/api/timedtext`, 100 tracks, 8 KiB URLs, a 24-hour duration sanity bound, hard per-track deadlines, 5 MB streamed bytes and 50,000 caption events.
- It may use YouTube same-origin credentials for a fallback request but never reads cookie values; redirects are rejected.
- Production shadow mode is `closed`.
- Persistent-write/playback-control handlers require trusted input.
- Popup and content script have separate service-worker message allowlists.
- Content messages require top frame, current `documentId`, exact watch URL and matching message URL.
- Writer identity is per document in `chrome.storage.session`.
- Another tab's active session requires a trusted `接管学习` action.

## User-visible states

The authoritative matrix is [`docs/STATE-MATRIX.md`](docs/STATE-MATRIX.md).

Required behavior:

- status is visibly sized without hover and docks beside the video when space exists;
- `InFlow` surfaces hide when less than a usable portion of the player remains in the viewport and restore when the player returns;
- status, word and teaching panels are mutually exclusive in the side dock;
- `关闭本视频` and `开启本视频` are exact inverse actions in both page UI and popup;
- subtitle word-state saves remain visible, explain their scheduling effect and offer append-only undo to the exact prior server-owned state;
- ads show `等正片` or `已备好 · 等正片` and hide captions;
- missing captions terminate within five seconds and offer retry;
- disabling experimental learning preserves captions;
- disabling the current video prevents the global automatic-caption timer from reviving it;
- route generation invalidates late SPA work;
- only a plugin-owned pause may be resumed.

## Learning UI

Automatic learning is off by default. When explicitly enabled and READY:

```text
natural phrase audio
→ expression + current sense + the same bilingual sentence
→ natural phrase replay
→ wait for 看清了，继续
```

`再听一遍` is independent of familiarity. `以后跳过此义项` is an explicit known override. Subtitle word-state edits disclose their scheduling consequence, remain visible after save and can be undone. There is no mandatory known/familiar/unclear gate.

## Test seam

Production source must use a closed shadow root. Browser behavior tests copy the extension to a temporary directory and explicitly open only that temporary shadow for inspection. `tests/closed_shadow_security_browser.py` exercises the unmodified production boundary and proves that page scripts cannot read `host.shadowRoot` or activate the `S` shortcut synthetically.
