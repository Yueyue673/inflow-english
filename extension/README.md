# Chrome extension

This directory is the standalone Manifest V3 extension for InFlow English `0.2.4`.

## Runtime path

```text
YouTube player caption tracks
→ page-bridge.js (MAIN world, bounded timed-text fetch)
→ content-script.js (isolated world)
→ closed Shadow DOM captions and replay
```

Captions do not require localhost. The optional local learning service is requested only after the user enables experimental automatic learning.

## Permissions

```json
{
  "permissions": ["storage"],
  "host_permissions": ["https://www.youtube.com/*"],
  "optional_host_permissions": ["http://127.0.0.1:8767/*"]
}
```

No cookies, history, downloads, clipboard, webRequest, nativeMessaging, `<all_urls>` or remote code.

## Development install

Load this directory from `chrome://extensions` using **Load unpacked**, then refresh existing YouTube tabs once. This is an alpha development path; one-click installation requires Chrome Web Store approval.

## Build

Run from the repository root:

```bash
python tools/build_extension.py
```

The builder uses an exact file allowlist and writes a verified ZIP plus SHA-256 file under `dist/`.

See the root [README](../README.md), [extension contract](../EXTENSION-CONTRACT.md), [privacy policy](../PRIVACY.md) and [security model](../SECURITY.md).
