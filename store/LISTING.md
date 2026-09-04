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
- Reads the caption tracks already available to the current YouTube player.
- Displays Chinese as the primary line and English as the supporting line.
- Replays the current complete caption segment with `S` or the replay button.
- Hides its captions during ads and switches cleanly on YouTube SPA navigation.
- Stops waiting after a bounded timeout. If no supported track is available, YouTube keeps playing and InFlow offers a retry.
- Works without a Python backend for captions and replay.

An optional experimental local learning mode can be enabled separately. It requests access to `127.0.0.1:8767` only after the user explicitly enables it. Automatic teaching pauses are off by default.

## Category

Education

## Language

Primary: Chinese (Simplified)
Content support: English YouTube videos

## Graphic assets

- Store icon: `store/assets/icon-128.png` — 128×128
- Screenshot 1: `store/assets/01-bilingual-captions.png` — 1280×800
- Screenshot 2: `store/assets/02-learning-card.png` — 1280×800

The screenshots are direct product captures with neutral padding only; they are not feature mockups.

## Permission justifications

### `storage`

Stores the user's automatic-caption setting, optional automatic-learning opt-in, caption size and per-document session owner identifiers. No browsing history is stored.

### Required host: `https://www.youtube.com/*`

Runs the caption UI only on YouTube pages and requests the current video's YouTube timed-text tracks. Caption requests are restricted to three exact YouTube hosts and `/api/timedtext`.

### Optional host: `http://127.0.0.1:8767/*`

Used only for the separately enabled experimental local learning service. It is not requested during installation. Chrome asks for it after the user turns on automatic learning.

## Privacy practices draft

- Personally identifiable information: not collected
- Health information: not collected
- Financial/payment information: not collected
- Authentication information: not collected
- Personal communications: not collected
- Location: not collected
- Web history: not collected
- User activity: the extension processes the current YouTube page and caption timing locally to provide its single purpose; no analytics or cloud telemetry
- Website content: current caption text is processed in the current tab and not sent to an InFlow cloud service
- Remote code: none
- Ads: none
- Selling/transferring data: none

Affirmation: use of any page and caption data is limited to providing or improving the extension's disclosed single purpose and complies with Chrome Web Store Limited Use requirements.

Privacy policy URL after repository publication:

`https://github.com/Yueyue673/inflow-english/blob/main/PRIVACY.md`

Support URL after repository publication:

`https://github.com/Yueyue673/inflow-english/issues`

## Reviewer notes

1. Open an ordinary public English YouTube `/watch?v=` page with captions.
2. A visible InFlow status appears near the top-right of the video.
3. Bilingual captions appear when the player's caption tracks are ready.
4. Press `S` outside a text field to replay the current natural caption segment.
5. Test a video without supported caption tracks: within five seconds InFlow shows a recoverable error and does not pause or navigate YouTube.
6. Experimental local learning is off by default. The optional loopback permission is requested only from its explicit toggle.

The extension does not use cookies permission, browsing history, downloads, clipboard, webRequest, nativeMessaging, `<all_urls>` or remotely hosted code.

## External publication blockers

- A Chrome Web Store developer account must be registered.
- The developer agreement must be accepted and Google's one-time registration fee must be paid by the account owner.
- Contact email verification and privacy fields must be completed in the dashboard.
- The uploaded ZIP must pass Google review before a public install URL or automatic-update claim is made.
