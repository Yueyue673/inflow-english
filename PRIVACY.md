# Privacy

_Last updated: 2026-09-04_

InFlow English does not operate a cloud account or analytics service.

## Standalone Chrome extension

The extension:

- runs only on ordinary `https://www.youtube.com/watch` pages;
- reads the current video's caption-track metadata already available to the YouTube player;
- requests timed-text only from `youtube.com`, `www.youtube.com` or `m.youtube.com`;
- requests captions with `credentials: omit` and rejects redirects;
- keeps caption payloads in the current tab's memory;
- stores `auto captions`, `experimental automatic learning`, caption size and per-document owner identifiers in Chrome extension storage;
- does not request the cookies, browsing history, downloads, clipboard, webRequest or `<all_urls>` permissions;
- does not send telemetry, crash reports, captions or vocabulary state to an InFlow cloud service.

Removing the extension removes its Chrome-managed extension storage according to Chrome's normal uninstall behavior.

## Optional local learning service

If the user separately runs the experimental local service, the extension may connect to `http://127.0.0.1:8767` on the same computer. The service can store locally:

- an append-only learning event ledger;
- a derived vocabulary profile;
- per-video session state;
- import status and audited local phrase assets.

A new installation defaults to `%LOCALAPPDATA%\InFlow-English\`. Existing development installations that already contain the legacy local data directory continue using it in place; InFlow does not silently move or delete it.

The local service binds only to loopback. It rejects unknown Host headers, unknown browser origins, cross-site browser requests, non-JSON writes and JSON bodies over 32 KiB. It has no cloud synchronization.

## Third-party requests

- YouTube captions come from YouTube's own timed-text endpoint and remain subject to YouTube's privacy policy.
- The optional learning backend may use `yt-dlp` and user-configured translation/model tools when the user explicitly enables experimental learning. Those tools have their own network behavior and are not required for the standalone caption feature.
- InFlow does not sell or share user data.

## Data control

- Turn off automatic captions or experimental automatic learning from the extension popup.
- Use `本视频暂停 InFlow` to disable the current video.
- Remove the extension to clear Chrome extension settings.
- Optional local-service data remains on the computer until the user removes that local data directory. Upgrades must preserve it and must not delete it automatically.

## Security reports

Please report a vulnerability privately through GitHub Security Advisories once the public repository is available. Do not include private videos, cookies, credentials or learning records in a public issue.
