# Chrome extension

This directory is the standalone Manifest V3 extension for InFlow English `0.2.7`.

## Runtime path

```text
YouTube native timed-text response (captured at document start)
→ page-bridge.js (MAIN world, bounded cache read/fallback)
→ content-script.js (isolated world)
→ closed Shadow DOM captions and replay
```

Captions do not require localhost. The extension processes the current watch URL/video ID in tab memory to bind results to the right video. The optional local learning service is requested only after the user enables experimental automatic learning; the current URL, playback timing and explicit learning actions then go to that loopback service. Translation defaults to local Argos.

## Permissions

```json
{
  "permissions": ["storage"],
  "host_permissions": ["https://www.youtube.com/*"],
  "optional_host_permissions": ["http://127.0.0.1:8767/*"]
}
```

No cookies or browsing-history permission, downloads, clipboard, webRequest, nativeMessaging, `<all_urls>` or remote code.

## Development install

Load this directory from `chrome://extensions` using **Load unpacked**, then refresh existing YouTube tabs once. This is an alpha development path; one-click installation requires Chrome Web Store approval.

## Build

Run from the repository root:

```bash
python tools/build_extension.py
python tools/build_extension.py --store-first-upload
```

The first archive keeps the fixed development `key` for the local test ID. The second strips that field and is the only package suitable for a brand-new Chrome Web Store item. Both use the same exact file allowlist and produce a verified ZIP plus SHA-256 file under `dist/`.

See the root [README](../README.md), [extension contract](../EXTENSION-CONTRACT.md), [privacy policy](../PRIVACY.md) and [security model](../SECURITY.md).
