# Third-party notices

The Chrome extension archive produced by `tools/build_extension.py` contains only InFlow source/assets. It does **not** bundle Python packages, browsers, FFmpeg, `yt-dlp`, speech models, translation models, downloaded captions or videos.

The optional development/test environment installs third-party projects under their own licenses, including:

| Project | Tested line | License / project |
|---|---:|---|
| FastAPI | 0.133.x | [Project](https://github.com/fastapi/fastapi) |
| Uvicorn | 0.41.x | [Project](https://github.com/encode/uvicorn) |
| psutil | 7.2.x | BSD-3-Clause |
| wordfreq | 3.1.x | Apache-2.0 |
| Wn | 1.1.x | MIT |
| faster-whisper | 1.2.x | MIT |
| HTTPX | 0.28.x | BSD-3-Clause |
| Playwright Python | 1.62.x | [Project](https://github.com/microsoft/playwright-python) |
| Open English WordNet | 2024 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0), [source](https://github.com/globalwordnet/english-wordnet) |

Exact transitive dependency notices are produced by the corresponding package installers and should be regenerated for any future bundled desktop installer.

FFmpeg, `yt-dlp`, Node, Argos packages, Whisper/CTranslate2 models and browsers are external prerequisites for parts of the experimental local-learning or test toolchain. They are not redistributed by the current GitHub/Chrome extension release.

Product screenshots contain direct InFlow UI captures over a public test video frame. The repository does not include the source video or its audio.
