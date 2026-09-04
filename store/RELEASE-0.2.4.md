# InFlow English 0.2.4 — public source alpha

This patch keeps the 0.2.3 standalone caption path and tightens the optional learning/backend boundaries.

## Changes since 0.2.3

- A long-video window that began before a seek can finish and be cached, but can no longer overwrite the newer focus epoch.
- Progressive prefetch is hard-limited to one adjacent window.
- The default VideoPack builder no longer invokes DSH/DeepSeek or any exact-sense model.
- Ambiguous senses remain occurrence-scoped provisional by default.
- An OpenAI-compatible GPT backend is available only through explicit endpoint/model configuration; it does not reuse Hermes or Codex credentials.
- Public CI now treats four undistributed human/audio audits as explicit private-fixture skips rather than missing files.
- Context, ProgressivePack and subtitle-first browser tests use the current page-caption architecture.

## Verified public boundary

A staged tracked-files-only export passed:

```text
156 unit tests
OK (skipped=4 private-media audits)
standalone caption browser contract: pass
closed-shadow security browser contract: pass
extension archive allowlist: pass
```

## Install status

The attached ZIP is still a developer-mode alpha, not a Chrome Web Store package. Extract it, choose **Load unpacked** in `chrome://extensions`, and select the extracted folder.

## Important remaining gates

- Anonymous YouTube currently responds with `LOGIN_REQUIRED / 请登录确认不是机器人` in clean browser profiles.
- A signed-in ordinary YouTube watch page still needs the final 0.2.4 timing check.
- Real long-video transport remains disabled pending a lawful non-cookie-dependent source path.
- Chrome Web Store review and a signed optional-backend installer are not complete.
- No retention or transfer learning-effect claim is made.
