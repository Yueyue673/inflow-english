# Privacy

_Last updated: 2026-09-05_

InFlow English does not operate a cloud account or analytics service.

## Standalone Chrome extension

The extension:

- declares packaged scripts only for the `https://www.youtube.com/*` origin so they survive YouTube's same-document navigation;
- shows UI and captures caption responses only while the current route is an ordinary `/watch?v=` page;
- reads the current watch URL and video ID in tab memory to bind every result to the right video; Chrome classifies that URL handling as Web history even though InFlow requests no browsing-history permission;
- reads the current video's caption-track metadata already available to the YouTube player;
- installs a packaged MAIN-world hook at `document_start` for the YouTube document lifecycle; the hook rejects all capture/cache work unless the current route is `/watch`;
- copies only successful YouTube `/api/timedtext` JSON3 responses that the player itself receives;
- holds at most six validated response copies, each at most 5 MB, for at most two minutes in the current tab's memory;
- may use a bounded same-origin timed-text fallback; YouTube may attach its own same-origin session, but InFlow never reads or exports cookie values;
- rejects redirects and accepts timed text only from `youtube.com`, `www.youtube.com` or `m.youtube.com`;
- keeps parsed caption payloads in the current tab's memory;
- stores `auto captions`, `experimental automatic learning`, caption size and per-document owner identifiers in Chrome extension storage;
- does not request the cookies, browsing history, downloads, clipboard, webRequest or `<all_urls>` permissions;
- does not send telemetry, crash reports, captions or vocabulary state to an InFlow cloud service.

Removing the extension removes its Chrome-managed extension storage according to Chrome's normal uninstall behavior.

## Optional local learning service

If the user separately runs the experimental local service and explicitly grants loopback access, the extension sends the current YouTube watch URL/video ID, playback position and learning actions to `http://127.0.0.1:8767` on the same computer. The service can store locally:

- source URL/video ID and per-video import/session state;
- an append-only learning event ledger, including replay, skip and explicit word-state actions;
- a derived vocabulary profile;
- import status and audited local phrase assets.

A new installation defaults to `%LOCALAPPDATA%\InFlow-English\`. Existing development installations that already contain the legacy local data directory continue using it in place; InFlow does not silently move or delete it.

The local service binds only to loopback. It rejects unknown Host headers, unknown browser origins, cross-site browser requests, non-JSON writes and JSON bodies over 32 KiB. It has no cloud synchronization.

## Third-party requests

- YouTube captions and optional source preparation contact YouTube's own endpoints and remain subject to YouTube's privacy policy.
- The default learning translation backend is local Argos (`INFLOW_TRANSLATION_BACKEND=argos`); it does not send caption text to a translation service.
- A service administrator may explicitly set `INFLOW_TRANSLATION_BACKEND=google`. In that non-default mode, English caption sentences and candidate expressions are sent to `translate.googleapis.com` for Chinese translation and are subject to Google's privacy terms.
- Explicitly configured OpenAI-compatible or DSH model backends have their own disclosed endpoints and network behavior. They are never required for standalone captions.
- InFlow does not sell user data, use it for advertising, or operate a cloud analytics service.

## Data control

- Turn off automatic captions or experimental automatic learning from the extension popup.
- Use `关闭本视频` to disable the current video.
- Remove the extension to clear Chrome extension settings.
- Optional local-service data remains on the computer until the user removes that local data directory. Upgrades must preserve it and must not delete it automatically.

## Security reports

Please report a vulnerability privately through GitHub Security Advisories once the public repository is available. Do not include private videos, cookies, credentials or learning records in a public issue.
