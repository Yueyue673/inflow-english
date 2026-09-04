# InFlow English 0.2.5 — signed-in YouTube caption fix

This patch replaces fragile re-fetching of YouTube's one-time/PO-bound timed-text URLs with a bounded copy of the response the current player already received.

## What changed

- Added a packaged MAIN-world `page-hook.js` on ordinary `/watch` pages only.
- Captures only successful exact YouTube `/api/timedtext` JSON3 responses.
- Cache boundary: six entries, at most 5 MB each, two-minute TTL, current-tab memory only.
- Missing English/Chinese tracks are requested through the current player; the prior track is restored.
- A hard Promise deadline prevents one hung stream from suppressing another usable track.
- English cues map to one closest Chinese cue instead of concatenating overlapping rolling captions.
- A paused/background page retries once automatically when playback first starts.
- Expected play/pause ownership counters reset across navigation.
- Added per-route and per-attempt in-memory latency metrics to the status tooltip and extension state.

## Real Chrome evidence

The unpacked extension card was read back as `0.2.5` before testing in the signed-in Chrome profile.

- `arj7oStGLkU`: visible two-line InFlow caption and `InFlow 字幕已就绪`.
- `iG9CE55wbtY`: visible two-line InFlow caption; verified sentence pair:
  - `despite all the expertise that's been on parade for the past four days,`
  - `儘管我們在過去四天中探討了各種專業知識—`

The videos were muted during QA. The isolated anonymous browser remained blocked by YouTube `LOGIN_REQUIRED`, so it is not presented as real-site evidence.

## Install status

This remains a **public source alpha**. The ZIP is reproducible and appropriate for Chrome Web Store upload, but Google has not reviewed or approved it. Ordinary one-click install and automatic updates do not exist until that external review succeeds.

## Learning boundary

Automatic learning remains off by default and still waits for eight seconds of continuous normal playback when explicitly enabled. Real learning benefit still requires delayed same-modality evidence; caption transport success is not a learning-effect claim.
