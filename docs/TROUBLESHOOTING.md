# Troubleshooting

## No InFlow status appears

1. Confirm the URL is an ordinary `https://www.youtube.com/watch?v=...` page. Shorts, live pages and embedded players are outside the current scope.
2. Open `chrome://extensions` and confirm InFlow is enabled.
3. If the extension was just installed or updated, refresh the existing YouTube tab once. Chrome cannot inject a newly loaded content script into an already-open page retroactively.
4. Confirm Chrome is version 120 or newer.

The visible status should appear within one second after the content document starts. If it does not, report the Chrome version, InFlow version and the public video URL. Do not attach cookies or private browsing data.

## `InFlow 需要查看`

Click the status to read the bounded error.

- `youtube_caption_tracks_missing`: the player has not exposed a supported caption track. If YouTube captions appear later, choose `重试字幕`.
- `youtube_supported_caption_track_missing`: tracks exist, but no English or Chinese track is available.
- `youtube_caption_fetch_timeout`: YouTube's timed-text request exceeded the 3.2-second network limit.
- `youtube_caption_http_<code>`: YouTube returned an HTTP error.
- `youtube_page_bridge_timeout`: the player did not produce a valid response within the page budget.

In every case the original video and YouTube's own captions continue. InFlow does not start the experimental learning backend when its captions are unavailable.

## Captions are duplicated

InFlow temporarily uses YouTube captions only while preparing its own display. It closes only the native caption state that it opened itself. If duplicate captions remain, toggle YouTube captions off manually and report the video URL and language setting.

## `InFlow 等正片`

YouTube is showing an advertisement. InFlow hides its captions and resets the continuous-playback gate. Prepared captions return after the ad.

## `实验学习未授权`

Captions do not need localhost access. To use the experimental local service, enable **允许自动教学暂停** in the popup and accept Chrome's optional loopback permission. Declining leaves captions active and learning off.

## `学习服务未连接`

The optional local backend is not running or did not answer within three seconds. Captions and sentence replay continue. Developers can check:

```text
http://127.0.0.1:8767/api/health
```

Do not expose that port to the network. The service is designed for loopback only.

## `heavy_queue_full`

The local service already has one heavy task running and four waiting. It returns HTTP 429 with `Retry-After: 5`. Wait and retry; InFlow does not leave an orphan job.

## Learning failed once

Automatic retries are circuit-broken for that canonical video URL. InFlow keeps captions active and offers `重试学习`. Only that explicit action sends `force_retry:true`.

## Another tab owns learning

InFlow will not silently steal a session from another document. Use `接管学习` only if you want the current tab to become the writer. The previous tab becomes stale through the owner-epoch check.

## Reporting a bug

Include:

- InFlow version;
- Chrome version and operating system;
- public video URL, if safe to share;
- the exact visible error code;
- whether YouTube's own captions worked;
- whether the optional local learning service was enabled.

Never post cookies, API keys, authorization headers, private video URLs or local learning data. Replace sensitive values with `[REDACTED]`.
