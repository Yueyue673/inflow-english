# Security

## Supported surface

The current public security target is the standalone Chrome extension in `extension/`. The optional local learning backend is experimental.

## Extension boundary

- Manifest V3; no remotely hosted code.
- One extension permission: `storage`.
- Host access is limited to YouTube and the optional loopback service.
- A packaged MAIN-world hook runs only on ordinary `/watch` pages. It observes only exact YouTube `/api/timedtext` responses, leaves the player's original fetch/XHR result unchanged, and holds at most six validated 5 MB response copies for two minutes in tab memory.
- The page bridge accepts one current video ID, only three exact YouTube hosts, only `/api/timedtext`, an 8 KiB URL limit, 100 caption tracks, a hard per-track timeout and a streaming 5 MB response limit.
- Fallback caption requests use only YouTube same-origin credentials and reject redirects; extension code never reads cookie values.
- The production UI uses a closed shadow root. Private caption and knowledge state is not exposed through `host.shadowRoot`.
- Persistent-write and playback-control UI handlers require trusted user input. A closure-scoped token permits the extension's own trusted keyboard shortcuts without accepting page-script `.click()` calls.
- Service-worker messages are split between popup and YouTube-content allowlists. Content messages require a top-level `/watch` frame, a current `documentId`, and a matching video URL.
- Session owner IDs live in `chrome.storage.session` per document, not in one global owner shared by every tab.

## Loopback service boundary

- Binds to `127.0.0.1` only.
- Accepts only the configured loopback Host.
- Accepts browser Origin only from its own local UI or the fixed extension ID.
- Rejects cross-site browser requests except the allowlisted extension origin.
- Write payloads require `application/json` and are streamed with a 32 KiB limit.
- Dynamic identifiers, pack paths and asset paths use explicit formats and resolved-path containment checks.
- `/media` is an explicit allowlist generated from the fixed fixture contract; the containing directory is not mounted.
- Expensive caption, import and offline-translation work shares one worker with at most four pending tasks. Overload returns HTTP 429 and leaves no orphan job.
- A failed import is circuit-broken per canonical video URL until an explicit retry.

## Data and logs

- Runtime data is excluded from source and release archives.
- The release builder uses an exact file allowlist and verifies the resulting ZIP.
- Launcher logs are local, mode-restricted where the OS supports it, and reset at 512 KiB.
- CI and browser tests use isolated data roots and verify that the formal data tree is unchanged.

## Known limitations

- A process already running as the same operating-system user can access that user's loopback services and local files. InFlow's browser-origin checks are not an OS sandbox.
- YouTube player and timed-text metadata are internal web interfaces and can change without notice.
- The optional learning backend invokes external media/model tools and has a larger attack and dependency surface than the standalone extension.
- Chrome Web Store review has not yet been completed.

## Reporting

Use GitHub Security Advisories for vulnerability reports. Include the version, a minimal reproduction and the affected boundary. Replace credentials, cookies, private URLs and local paths with `[REDACTED]`.
