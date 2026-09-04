# Contributing

Thanks for helping improve InFlow English.

## Before opening a change

Read:

- [Product spine](PRODUCT-SPINE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security](SECURITY.md)
- [State matrix](docs/STATE-MATRIX.md)

The public product slice is standalone YouTube captions plus natural-sentence replay. Changes must not move localhost, media preparation or model work back onto the caption first-paint path.

## Development setup

```bash
python -m pip install -r requirements/ci.txt
python -m wn download oewn:2024
python -m playwright install chromium
```

On Linux, browser extension tests need a display such as `xvfb-run`.

## Required checks

```bash
python -m py_compile adaptive_core.py caption_preview.py launcher.py lexical_sense.py long_video.py progressive_video_pack.py rebuild_profile.py server.py video_pack.py
node --check extension/content-script.js
node --check extension/page-hook.js
node --check extension/page-bridge.js
node --check extension/popup.js
node --check extension/service-worker.js
python -m unittest discover -s tests -p "test_*.py" -q
python tests/standalone_caption_browser.py
python tests/closed_shadow_security_browser.py
python tests/native_caption_capture_browser.py
python tools/build_extension.py
python tools/build_extension.py --store-first-upload
```

Behavior tests use a temporary extension copy with an open shadow root. Production `extension/content-script.js` must remain `mode: "closed"`; `tests/closed_shadow_security_browser.py` verifies that boundary.

Release assets must be downloaded from the successful CI run for the exact release commit. Do not rebuild ZIPs after CI; compare the downloaded artifact hashes with `TEST-RESULTS.md`, then upload those same bytes. All source-alpha releases remain GitHub Pre-releases until a stable consumer installation exists.

## Pull requests

- Explain the user-visible problem before the implementation.
- Add the exact failure state to a regression test.
- Preserve formal user data; tests must use an isolated data directory.
- Do not include downloaded videos, captions, model files, runtime logs, learning records or credentials.
- Replace any secret or private identifier in logs and examples with `[REDACTED]`.
- Distinguish engineering evidence from learning-effect claims.

## Bug reports

Include version, browser/OS, a public reproducible URL if safe, the exact visible error code and whether native YouTube captions worked. Never post cookies, API keys, private videos or local learning records.
