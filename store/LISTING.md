# Chrome Web Store listing draft

## Product name

InFlow English · YouTube 双语字幕

## Short description

在英语 YouTube 视频里快速显示中英字幕，按 S 重听当前完整句；字幕失败时原视频照常播放。

## Single purpose

Help users follow spoken English on ordinary YouTube watch pages through readable bilingual captions and replay of the current natural caption segment.

## Detailed description

InFlow English works inside ordinary YouTube watch pages.

- Shows a visible status as the page starts.
- Reads the caption tracks already available to the current YouTube player and copies the player's own successful timed-text responses in bounded tab memory.
- Displays Chinese as the primary line and English as the supporting line.
- Replays the current complete caption segment with `S` or the replay button.
- Hides its captions during ads and switches cleanly on YouTube SPA navigation.
- Stops waiting after a bounded timeout. If no supported track is available, YouTube keeps playing and InFlow offers a retry.
- Works without a Python backend for captions and replay.

An optional experimental local learning mode can be enabled separately. It requests access to `127.0.0.1:8767` only after the user explicitly enables it; the current watch URL, playback timing and learning actions then go to that local service. Translation defaults to local Argos. A service administrator can explicitly configure a network translation provider, in which case disclosed English caption text is sent to that provider. Automatic teaching pauses are off by default.

## Category

Education

## Language

Primary: Chinese (Simplified)
Content support: English YouTube videos

## Graphic assets

- Store icon: `store/assets/icon-128.png` — 128×128
- Screenshot 1: `store/assets/01-bilingual-captions.png` — 1280×800
- Screenshot 2: `store/assets/02-learning-card.png` — 1280×800

The screenshots are direct 1280×800 full-viewport product captures with square corners; they are not padded, cropped, stretched or mocked up.

## Permission justifications

### `storage`

Stores the user's automatic-caption setting, optional automatic-learning opt-in, caption size and per-document session owner identifiers. No browsing history is stored.

### Required host: `https://www.youtube.com/*`

Runs the caption UI only on YouTube pages and requests the current video's YouTube timed-text tracks. Caption requests are restricted to three exact YouTube hosts and `/api/timedtext`.

### Optional host: `http://127.0.0.1:8767/*`

Used only for the separately enabled experimental local learning service. It is not requested during installation. Chrome asks for it after the user turns on automatic learning; the current watch URL/video ID, playback timing and explicit learning actions are then sent to this loopback endpoint.

## Privacy practices draft

- Personally identifiable information: not collected
- Health information: not collected
- Financial/payment information: not collected
- Authentication information: not collected
- Personal communications: not collected
- Location: not collected
- Web history: collected for core functionality — the current YouTube watch URL/video ID is processed in tab memory; after explicit learning opt-in it is sent to the user's loopback service and may be stored in local pack/session metadata
- User activity: collected for core functionality — current playback timing, replay, skip and explicit learning/word-state actions are processed locally; no analytics or cloud telemetry
- Website content: collected for core functionality — current caption text is processed in the tab and, after explicit learning opt-in, by the user's loopback service
- Default third-party translation: none — the learning service defaults to local Argos
- Optional administrator-configured sharing: if `INFLOW_TRANSLATION_BACKEND=google` or an OpenAI-compatible backend is explicitly configured, disclosed English caption text is sent to that configured provider
- Remote code: none
- Ads: none
- Selling data: none

Affirmation: use of any page and caption data is limited to providing or improving the extension's disclosed single purpose and complies with Chrome Web Store Limited Use requirements.

Privacy policy URL after repository publication:

`https://github.com/Yueyue673/inflow-english/blob/main/PRIVACY.md`

Support URL after repository publication:

`https://github.com/Yueyue673/inflow-english/issues`

## Reviewer notes

1. Open an ordinary public English YouTube `/watch?v=` page with captions.
2. When horizontal space is available, the InFlow status appears beside the video; theater/fullscreen uses a compact in-video fallback.
3. Bilingual captions appear when the player's caption tracks are ready.
4. Press `S` outside a text field to replay the current natural caption segment.
5. Test a video without supported caption tracks: within five seconds InFlow shows a recoverable error and does not pause or navigate YouTube.
6. Experimental local learning is off by default. The optional loopback permission is requested only from its explicit toggle.
7. For a brand-new Store item, upload the `-CWS-first-upload.zip` asset. It is identical to the development package except that Chrome's first-upload-forbidden development `key` is absent.

The extension does not use cookies permission, browsing-history permission, downloads, clipboard, webRequest, nativeMessaging, `<all_urls>` or remotely hosted code.

## External publication blockers

- A Chrome Web Store developer account must be registered.
- The developer agreement must be accepted and Google's one-time registration fee must be paid by the account owner.
- Contact email verification and privacy fields must be completed in the dashboard.
- The uploaded ZIP must pass Google review before a public install URL or automatic-update claim is made.
