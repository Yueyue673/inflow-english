# InFlow English · Product Spine

## Product identity

**Public value slice:** a standalone Chrome extension that adds readable bilingual captions and sentence replay to ordinary English YouTube videos without waiting for a local backend.

**Optional experimental layer:** a local learning service that can prepare a small number of natural-phrase interventions. It is off by default and is not part of the public learning-effect claim.

## Real baseline

Before 0.2.3, a new video could wait 15–34 seconds for `yt-dlp`, fail with no caption revision, and expose only a 10×10-pixel status dot. The user reasonably experienced that as “it never loaded.”

## Core promise

When someone opens a supported English YouTube watch page, InFlow makes its state visible immediately, then uses the caption tracks already available to that page to show Chinese first and English second. The video keeps playing if captions cannot be read.

## Golden moment

A sentence that was hard to catch becomes readable in the flow of the video, and pressing `S` replays that complete natural caption segment without opening another app.

## Priorities

1. **Visible immediately.** The user must know within one second whether InFlow started.
2. **Captions never wait for learning.** Page captions are the primary path; localhost, `yt-dlp`, ASR, WordNet and translation are outside first paint.
3. **Fail soft.** Ads, missing tracks, network failures and an offline local service never pause or break YouTube.
4. **User owns playback.** Automatic teaching pause is off by default. User play, pause, seek, navigation, page hiding or ads revoke any old resume lease.
5. **Actions show consequences.** Open/close labels must match their exact inverse action; word-state saves remain visible and undoable. No control may rely on a database write the user cannot perceive.
6. **Evidence stays honest.** Teaching, replay, familiarity self-report and explicit “do not explain again” are separate events. None means mastery.

## Current public slice

```text
YouTube /watch page
→ visible InFlow status
→ page-owned English/Chinese timed-text tracks
→ bilingual caption near the video lower edge
→ current natural sentence replay
→ clear error + retry when tracks are unavailable
```

The extension stores only its settings in Chrome. It does not require Python or the local learning service for this slice.

## Optional learning slice

```text
User explicitly enables experimental automatic learning
→ 8 seconds of continuous non-ad playback
→ local service prepares audited natural phrases in the background
→ only READY material may pause at a safe boundary
→ natural audio → phrase + current sense → natural audio
→ “看清了，继续” resumes the video
```

`再听一遍` is behavior, not familiarity. `以后跳过此义项` is a reversible explicit known override. The teaching card does not require a three-state self-rating. Subtitle word-state edits state their future effect, remain visible after save and support one-step undo to the exact preceding server-owned state.

## No-gos

- No invisible or indefinite loading.
- No automatic learning pause on install.
- No cookies permission, browsing-history permission, `<all_urls>`, remote code or analytics.
- No full-video download for long-video progressive work.
- No automatic claim of a session owned by another tab.
- No full-video panel or teaching card over the player when a usable side rail exists.
- No stale fixed surface left floating over comments after the player leaves the viewport.
- No state-edit control that disappears before showing its effect and undo path.
- No claim that one teaching interaction proves learning or mastery.
- No claim of Chrome Web Store availability before the listing is actually approved and readable.

## Verified facts

- Standalone fixture, localhost offline: status visible in 94–146 ms across recent runs; bilingual captions in 343–634 ms.
- Missing tracks: visible terminal error in about 4.3 seconds; retry after tracks appear in 0.27–0.44 seconds.
- Same-document SPA navigation: new captions in about 0.26 seconds; old caption text did not survive.
- Ads: captions hide immediately; caption replay is blocked; prepared captions return after the ad.
- Production shadow root is closed; page scripts cannot read private caption state or use a synthetic `S` key.
- Experimental learning requires explicit opt-in; the observed opt-in-to-working path was about 8.1–8.5 seconds with a cached pack.
- Unit suite and browser contracts run against isolated data; formal user data is guarded separately.

These are engineering measurements, not learning-effect evidence.

## Still unproven

- Reliability distribution across a broad, representative set of real YouTube videos.
- Chrome Stable/Beta and Windows 10/11 matrix outside the current machine.
- 24-hour, 7-day or transfer gains from the experimental learning intervention.
- Chrome Web Store policy approval and public auto-update delivery.
- Long-video active learning quality; it remains feature-flagged off in production.

## Next adjacent bet

Ship and dogfood the standalone caption/replay slice first. Collect failure categories and repeated voluntary use. Expand the learning layer only after it improves delayed auditory recognition without causing a larger flow cost.
