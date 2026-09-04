# InFlow English · Chrome extension contract

## Identity

- Manifest V3
- Version `0.2.4`
- Minimum Chrome `120`
- Fixed development ID `hfkkhkdpakcmpokgbihceoppleeokifd`
- Required permission: `storage`
- Required host: `https://www.youtube.com/*`
- Optional host: `http://127.0.0.1:8767/*`

## Public path

```text
document_start visible status
→ MAIN-world player response bridge
→ exact YouTube timed-text endpoint
→ isolated content validation
→ closed Shadow DOM captions and replay
```

The extension must remain useful when localhost is absent. Page-caption first paint cannot depend on pack enumeration, `yt-dlp`, ASR, WordNet, translation or learning-session state.

## Security boundary

- `page-bridge.js` receives one nonce and one expected video ID.
- It accepts only three exact YouTube hosts, `/api/timedtext`, 100 tracks, 8 KiB URLs, 3.2 seconds and 5 MB streamed bytes.
- It omits credentials and rejects redirects.
- Production shadow mode is `closed`.
- Persistent-write/playback-control handlers require trusted input.
- Popup and content script have separate service-worker message allowlists.
- Content messages require top frame, current `documentId`, exact watch URL and matching message URL.
- Writer identity is per document in `chrome.storage.session`.
- Another tab's active session requires a trusted `接管学习` action.

## User-visible states

The authoritative matrix is [`docs/STATE-MATRIX.md`](docs/STATE-MATRIX.md).

Required behavior:

- status is visibly sized without hover;
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

`再听一遍` is independent of familiarity. The optional no-more-explanations action is an explicit known override. There is no mandatory known/familiar/unclear gate.

## Test seam

Production source must use a closed shadow root. Browser behavior tests copy the extension to a temporary directory and explicitly open only that temporary shadow for inspection. `tests/closed_shadow_security_browser.py` exercises the unmodified production boundary and proves that page scripts cannot read `host.shadowRoot` or activate the `S` shortcut synthetically.
