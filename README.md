# InFlow English

[![CI](https://github.com/Yueyue673/inflow-english/actions/workflows/ci.yml/badge.svg)](https://github.com/Yueyue673/inflow-english/actions/workflows/ci.yml)
![Chrome MV3](https://img.shields.io/badge/Chrome-MV3-4285F4)
![License: MIT](https://img.shields.io/badge/License-MIT-7f8c8d)

InFlow English 是一个 Chrome 扩展。看英文 YouTube 视频时，它会读取当前播放器里已有的字幕轨道，先显示中文、再显示英文；按 `S` 可以重放当前这一整句字幕。

它不会先等 Python、`yt-dlp`、语音识别或模型跑完才出字幕。如果视频没有可用的字幕轨道，YouTube 会继续播放，InFlow 只给出一次有时限、可以重试的提示。

![YouTube 视频上的 InFlow 中英字幕](docs/images/youtube-subtitles.png)

## 安装

当前发布是公开源码的 **alpha**，在 GitHub 上标为 **Pre-release**，还没有上架 Chrome 网上应用店。

1. 在对应的 [GitHub Release](https://github.com/Yueyue673/inflow-english/releases) 下载 `InFlow-English-Chrome-0.2.7.zip` 和它的校验文件。
2. 解压到一个固定目录。
3. 打开 `chrome://extensions`，打开开发者模式，选择 **加载已解压的扩展程序**，选中这个目录。
4. 打开普通的英文 YouTube 观看页；已经打开的标签页请刷新一次。

大约一秒内，视频附近应出现 InFlow 的状态。完整步骤见[安装和更新说明](docs/INSTALL.md)。

> 一键安装和自动更新需要通过 Chrome 网上应用店审核。上架用的材料在 [`store/`](store/)，账号负责人还要走的步骤写在 [`store/SUBMISSION.md`](store/SUBMISSION.md)；目前还没有通过审核。

## 单独使用扩展时，它会做什么

- 一开始就会显示状态，不是一个藏起来的加载点。
- 主行是中文字幕，下面一行是英文。
- 按 `S`，或点安静的重放控件，会重放当前这一整句字幕。
- 广告期间字幕会收起，正片回来后再显示。
- 在同一页里切到下一支视频时，会换掉上一支的字幕状态，不会带到下一支。
- 没有字幕轨道或网络失败时，会在固定时限内结束处理；字幕出问题不会暂停原视频。
- 不需要在本机再跑一个后端。

用发布版在隔离的浏览器里测过的结果：

```text
status visible        63–146 ms
bilingual captions    92–634 ms
missing-track error   about 4.1–4.3 s
retry after recovery  20–439 ms
same-document switch  13–264 ms
```

这是工程检查，不是保证每一种 YouTube 或网络环境都是这个速度。

## 可选的实验性学习

自动教学暂停默认是关的。要开的话，需要在弹窗里单独打开；只有这时才会申请访问本机服务。

已经有核验过的本机学习包时，流程是：

```text
explicit opt-in
→ 8 seconds of continuous, visible, non-ad playback
→ preloaded natural phrase at a safe pause boundary
→ natural audio → expression + current sense → natural audio
→ 看清了，继续
```

![InFlow 的自然短语学习卡片](docs/images/learning-card.png)

教学卡片不会强迫你选「认识 / 熟悉 / 不清楚」。`再听一遍` 和熟悉程度是分开计数的。`以后跳过此义项` 是明确、可以撤销的跳过。在字幕里改词语状态后，结果会马上看见，并提供 `撤销`。

这一层仍是实验：完成一次互动，并不能证明长期有效。长视频的主动学习在正式版里用功能开关关掉了，相关的质量工作还在做。翻译默认走本机 Argos；要用 Google 或其他模型接口，需要单独配置服务，并遵守[隐私说明](PRIVACY.md)里的告知。

## 目前能用的范围

目前面向：

- Chrome 120 或更新版本；
- 普通的 `https://www.youtube.com/watch?v=...` 页面；
- 播放器能提供英文字幕轨道或中文字幕轨道的视频；
- 单独出字幕这条路径，视频长度最多三小时。

目前不承诺：

- Shorts、直播、嵌入播放、DRM，以及会员或年龄限制内容；
- 没有可用 YouTube 字幕轨道的视频；
- Firefox / Safari；
- 已上架 Chrome 网上应用店；
- 能提高学习效果、记忆或迁移能力；
- 自动装好实验用的 Python 后端。

## 隐私和安全

公开扩展只申请一项扩展权限：`storage`。

必需的网站访问仅限 YouTube。本机地址（localhost）是可选项，只有打开实验性学习后才会申请。没有 cookies 权限、浏览记录权限、`<all_urls>`，也没有远程代码、广告或 InFlow 的云端分析。

正式界面放在封闭的 Shadow DOM 里。网页脚本模拟的点击和键盘事件，不能写入学习状态。对本机服务的请求会限制来源、Host、Origin、Fetch Metadata、请求体大小和允许的资源。

详见[隐私说明](PRIVACY.md)和[安全说明](SECURITY.md)。

## 结构

```text
YouTube player caption tracks
→ bounded page bridge
→ isolated content script
→ closed Shadow DOM captions + replay

optional explicit opt-in
→ allowlisted MV3 service worker
→ 127.0.0.1 local learning service
→ replayable event/profile state
```

这两条路径是分开的：本机学习不可用时，不会拖慢字幕。

更多说明见[结构说明](docs/ARCHITECTURE.md)、[状态矩阵](docs/STATE-MATRIX.md)和 [Product spine](PRODUCT-SPINE.md)。

## 开发

```bash
python -m pip install -r requirements/ci.txt
python -m wn download oewn:2024
python -m playwright install chromium
python -m unittest discover -s tests -p "test_*.py" -q
python tools/build_extension.py
python tools/build_extension.py --store-first-upload
```

浏览器契约覆盖：单独/离线字幕、广告、站内跳转、有时限的失败、重试、封闭 Shadow 的隐私、学习功能需明确打开、8 秒门槛、播放控制权，以及事件回放。测试使用隔离的数据目录。

见[贡献说明](CONTRIBUTING.md)和[排障说明](docs/TROUBLESHOOTING.md)。

## 版本

- 扩展：`0.2.7`
- Adaptive policy：`adaptive-v5`
- Profile schema：`3`
- Reducer：`rules-v6`
- VideoPack builder：`1.9.2`
- 许可：MIT

仓库故意不放入下载的媒体、模型、运行日志、学习台账、个人配置和私下测试材料。

<details>
<summary>English</summary>

InFlow English is a Chrome extension for ordinary English YouTube watch pages. It reads caption tracks already available to the current player, shows Chinese first and English second, and replays the current complete caption segment with `S`. It does not wait for Python, `yt-dlp`, ASR, or a model before showing captions. If a video has no supported track, YouTube keeps playing and InFlow shows a bounded, retryable error.

This is a public source **alpha**, marked **Pre-release** on GitHub, and is not in the Chrome Web Store. Download `InFlow-English-Chrome-0.2.7.zip` and its checksum from the matching [GitHub Release](https://github.com/Yueyue673/inflow-english/releases), extract it to a stable folder, load that folder as an unpacked extension in `chrome://extensions`, then open a watch page and refresh already-open tabs. A status should appear near the video within one second. Full steps: [installation guide](docs/INSTALL.md).

Standalone captions work without a local backend, hide during ads, and do not pause the video when a caption request fails. Automatic teaching pauses are off by default; enabling them in the popup is a separate action and only then requests optional access to a local service. Long-video active learning is feature-flagged off in the production build. Chrome 120+; videos up to three hours on the standalone caption path. Not currently promised: Shorts, live streams, embeds, DRM, member or age-restricted content, Firefox/Safari, or learning-effect claims. The public extension requests `storage` only; required host access is limited to YouTube. [Privacy](PRIVACY.md) · [Security](SECURITY.md).

</details>
