# Installation

## Current status

The repository release is a **public source alpha**. It can be installed for testing, but it is not yet a one-click Chrome Web Store product.

The standalone caption/replay feature needs only the Chrome extension. The experimental local learning backend is optional.

## Alpha installation

1. Download `InFlow-English-Chrome-<version>.zip` and its `.sha256` file from the matching GitHub Release.
2. Verify the checksum.
3. Extract the ZIP into a stable folder that will not be renamed or removed.
4. Open `chrome://extensions`.
5. Turn on Developer mode.
6. Choose **Load unpacked** and select the extracted folder.
7. Open an ordinary English YouTube watch page and refresh it once.

Success signals:

- an InFlow status appears near the video within one second;
- a supported video shows bilingual captions;
- pressing `S` outside an input replays the current caption segment.

If those signals do not appear, see [Troubleshooting](TROUBLESHOOTING.md).

> This developer-mode path is not the intended final consumer installation. Chrome Web Store approval is required before InFlow can offer one-click installation and automatic extension updates.

## Update an alpha build

1. Download and extract the new version over a **new folder**.
2. In `chrome://extensions`, remove the old unpacked entry or point **Load unpacked** at the new folder.
3. Refresh already-open YouTube tabs so Chrome injects the new content script.
4. Open the extension popup and verify the version on the extension details page.

Chrome does not automatically update unpacked extensions from GitHub Releases.

## Remove the extension

Remove InFlow from `chrome://extensions`. Chrome removes the extension's settings under its normal uninstall behavior.

The optional local learning service stores its data separately and must never be silently deleted by an extension uninstall or update.

## Optional experimental local learning service

This layer is for developers and private testers. It is not required for captions.

Prerequisites:

- Windows 10 or 11;
- Python 3.11;
- dependencies from `requirements/backend.txt`;
- `yt-dlp`, FFmpeg and Node for media preparation;
- optional local translation/model resources.

Run:

```bash
python -m pip install -r requirements/backend.txt
python server.py
```

Then open the extension popup and enable **允许自动教学暂停**. Chrome asks for optional access to `http://127.0.0.1:8767/*` at that moment. The toggle remains off if permission is declined.

New installations store backend data under `%LOCALAPPDATA%\InFlow-English\`. Long-video active learning remains disabled unless an explicit development feature flag is set.

## Planned consumer path

```text
Chrome Web Store page
→ Add to Chrome
→ open a supported YouTube video
→ captions work without a separate application
```

The local learning companion needs a separately signed Windows installer and updater before it can be offered to ordinary users. That installer does not exist in the current release, so the experimental backend must not be advertised as one-click installable.
