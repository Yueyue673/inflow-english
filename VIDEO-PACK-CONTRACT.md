# VideoPack 独立构建契约

> 对齐 `HOST-CONTRACT.md` 的内容准备切片。本文只定义同步构建模块，不定义服务器、API、作业队列或 UI。

## 1. 模块与入口

实现文件：`video_pack.py`

公开主入口：

```python
builder = VideoPackBuilder(
    packs_root,
    translator=translator,      # 可省略，默认 Argos offline；Google/模型必须显式配置
    transcriber=transcriber,    # 可省略，默认 large-v3 → tiny.en 自适应链
    media_tools=media_tools,    # 可省略，默认 SubprocessMediaTools
)

pack_path = builder.build(
    youtube_url,
    progress_callback=on_progress,
    cancel_callback=is_cancelled,
)
```

- `build()` 同步执行，成功返回最终 pack 目录 `Path`；
- 长任务宿主负责把同步调用放进自己的 worker；模块本身没有线程、服务器、数据库和 UI；
- `progress_callback(event)` 是只读观察器，收到不含原始模型输出和密钥的事件；
- `cancel_callback() -> bool` 是协作式取消，在网络/媒体/转录/分段/翻译/每次裁剪/审计/提交边界检查；
- 取消抛出 `BuildCancelled`；失败抛出 `VideoPackError` 的具体子类；
- progress 回调自身报错不会破坏构建或让已提交 pack 变成失败。

进度 `event`：

```json
{
  "stage": "transcribing",
  "fraction": 0.45,
  "pack_id": "yt-aQ89CG_Yu6I-…",
  "cached": false,
  "detail": null
}
```

阶段顺序：

```text
validating
→ downloading
→ extracting_audio
→ transcribing
→ segmenting
→ translating
→ selecting
→ clipping
→ auditing
→ ready
```

## 2. 输入边界

### 2.1 URL 语法（任何网络/文件动作之前）

只接受：

- `https://www.youtube.com/watch?v=<11-char-id>`（同时允许 `youtube.com`、`m.youtube.com`）；
- `https://youtu.be/<11-char-id>`；
- 复制链接上的普通分享参数可存在，但统一规范化为 `https://www.youtube.com/watch?v=<id>`。

拒绝：

- HTTP、非 YouTube host、userinfo、显式端口、fragment、控制字符和首尾空白；
- `/shorts/`、`/live/`、`/embed/`、播放列表及重复/畸形 video ID；
- `music.youtube.com`。

### 2.2 yt-dlp 元数据质量门

只接受：

- `_type` 为单个 video，ID 与 URL 一致；
- `availability == public`；
- `is_live == false`、`was_live == false`、`live_status == not_live`；
- 时长闭区间 20–900 秒；
- 无年龄限制；
- 非 Music 分类；
- 标题非空。

下载后再由 ffprobe 审计：

- 同时有视频流和音频流；
- 视频高度 `1..720`；当前格式 18 通常为 360p；
- 实际时长与元数据误差不超过 `max(2 秒, 2%)`。

转录后要求主要语言为英语（`en*` / `english`）；若 faster-whisper 提供语言概率，则不得低于 0.5。

能力不足时整包失败，不降级为半成品。

## 3. 默认媒体实现

`SubprocessMediaTools` 使用可注入 `runner`，生产默认是 `subprocess.run`：

1. yt-dlp `--dump-single-json --skip-download --no-playlist` 获取元数据；
2. yt-dlp `2026.08.19+` 固定使用 Node JS runtime 与 `android,web_embedded,-visionos` 客户端组合；
3. 首切片优先单文件格式 `18`（360p，含音频），避开已实测 403 的旧 `android_vr` 与组合流；
4. ffmpeg 提取 mono、16 kHz、PCM s16le WAV；
5. ffprobe 审计视频和每个 MP3；
6. ffmpeg 从原视频按完整自然 cue 的起止时间提取 MP3，不生成孤立词音频。

所有子进程共同约束：

- `shell=False`（从不传 shell）；
- 显式 `creationflags=CREATE_NO_WINDOW`（Windows 为 `subprocess.CREATE_NO_WINDOW`）；
- 有超时；
- 不把 stdout/stderr 写入 pack、日志或异常；
- 不读取、解析、复制或记录环境中的 API key、cookie、token；
- yt-dlp 不使用 cookie，不处理登录/会员/年龄限制内容。

## 4. faster-whisper 契约

默认 `FasterWhisperTranscriber`：

- 默认身份是一条稳定降级链：`~/k4b_work/whisper-large-v3` / CUDA / float16 → `tiny.en` / CPU / int8 / 单线程；真正开始转录时若可用内存低于 10GB 直接走 tiny，large 实际加载失败时也在同一任务内释放并降级；环境变量可显式锁定模型；
- 超过约 56 秒的 PCM WAV 物理切成 45 秒临时块逐段转录，再由代码恢复全局时间戳；临时块随任务清理，内存峰值不随视频总长度线性增长；
- 轻量模型只降低资源占用，不绕过英文字幕交叉句界、surface grounding 和成品短片段无提示 ASR 复核；
- 延迟 import `faster_whisper`，便于纯离线单测；
- 固定 `language="en"`、`task="transcribe"`；
- 必须 `word_timestamps=True`；
- 开启 VAD；
- 每个 segment 消费时检查取消；
- 模型/设备/计算类型进入公开版本标识和 pack cache 指纹，不接触凭据。

可注入的 `Transcriber` 协议：

```python
class Transcriber(Protocol):
    @property
    def identity(self) -> str: ...

    def transcribe(
        self,
        wav_path: Path,
        *,
        cancel_callback: CancelCallback | None = None,
    ) -> Mapping[str, Any]: ...
```

## 5. 自然 cue

`build_natural_cues()` 优先仅使用词时间戳和以下自然边界：

- 句末标点；
- 从句标点；
- 词间停顿；
- faster-whisper segment 边界；
- 转录终点。

算法先尝试动态规划覆盖可靠词序列；完整覆盖失败时，改为从 Whisper segment 中只保留 2–8 秒、完整句末、开头不悬空且 5 秒内有 ≥200ms 安全空隙的**稀疏可靠 cue**，不要求整条电影素材每一秒都可教学。若有 YouTube 英文字幕，稀疏 cue 还必须在相同时间范围内达到至少 96% 的词序一致；`proceeds / receipts` 这类内容词冲突会被淘汰。稀疏路径仍不足时，再用英文字幕标点句界与 Whisper 有效词序交叉对齐。局部零时长或错序 token 只隔离所在 segment，不按全片坏词比例一票否决。最终只需至少 1 条通过所有质量门的候选；短视频不得为凑 4 条降低质量。

每个 cue 由代码生成：

```json
{
  "cue_id": "cue-0001",
  "start_sec": 0.4,
  "end_sec": 2.9,
  "duration_sec": 2.5,
  "text": "Curious explorers study distant worlds.",
  "boundary_kind": "terminal_punctuation",
  "words": [
    {
      "index": 0,
      "text": "Curious",
      "start_sec": 0.4,
      "end_sec": 0.85,
      "char_start": 0,
      "char_end": 7,
      "lexical_start": 0,
      "lexical_end": 7
    }
  ]
}
```

字符区间与词时间共同用于 surface 的逐字、整词和 highlight 审计。

## 6. Translator 与 DshTranslator

### 6.1 协议

```python
class Translator(Protocol):
    @property
    def identity(self) -> str: ...

    def translate(
        self,
        cues: Sequence[{"cue_id": str, "text": str}],
    ) -> Mapping[str, Any]: ...
```

模型工作分为三条窄路径：

1. 从最多 24 个、分布在全视频且有安全停顿的 cue 中，返回完整中文和动态候选池：至少 1 个，schema 最多 16 个；当前 prompt 上限随安全 cue 数增长，短视频不强凑，15 分钟范围内最多请求约 10 个；
2. 对最终候选单独复核当前义项与自然中文，禁止改 cue ID、surface、英文、时间或音频；
3. 正常中文字幕优先使用 YouTube `zh-Hans json3`；失败时把全部 cue 按 28 条一批、最多 3 路并行翻译。字幕失败不再使整个导入单点失效。

候选调用返回：
```json
{
  "translations": [
    {"cue_id": "cue-0001", "text_zh": "好奇的探索者研究遥远世界。"}
  ],
  "candidates": [
    {
      "cue_id": "cue-0001",
      "surface": "distant worlds",
      "gloss_zh": "遥远的世界",
      "value_score": 4.2
    }
  ]
}
```

说明：`translations` 覆盖本次 24 条候选短名单；正常中文字幕走独立的 YouTube 或批量翻译通道。候选模型对象严格只能有四个字段：

```text
cue_id / surface / gloss_zh / value_score
```

候选模型对象禁止返回：时间、phrase text、phrase zh、anchor、highlight、音频路径、hash、occurrence ID 或其他字段。

### 6.2 严格验证

- shortlist translations 必须逐一覆盖输入的候选 cue；批量字幕翻译必须覆盖全部 cue；
- 中文与 gloss 必须非空且含 CJK 字符；
- candidates 原始数量 1–16；每个 cue 最多保留一个候选；短视频允许只有一个，候选不足不以低质量项填充；
- `surface` 为 1–4 个词，必须逐字符、大小写不变地连续存在于指定 cue；五词以上普通片段过滤；
- 必须对齐完整词边界，禁止半个词；
- 同一 cue 中若 surface 出现多次而模型无法指明 occurrence，则拒绝，不猜第一次；
- 重复 `cue_id + surface` 拒绝；
- 任何额外字段拒绝；
- 任何幻觉 surface 使整包失败。

### 6.3 DshTranslator

命令固定形态：

```text
dsh --profile headless <纯文本任务>
```

- prompt 只含 cue ID、英文 cue text 和严格 JSON schema；
- 指示 dsh 不使用工具，只输出 JSON；
- 使用 `json.loads(stdout)`，Markdown fence、解释文字或尾随内容均拒绝；
- 每次调用有超时；
- timeout、启动失败、非 0 返回、非法 JSON 或契约失败时只重试一次（总计最多两次）；
- runner、可执行文件和 timeout 均可依赖注入；
- Windows 子进程始终带 `CREATE_NO_WINDOW`；
- 原始 stdout/stderr 不记录、不写盘、不进入异常。

### 6.4 翻译降级链

默认 translator identity 只有本地层：

```text
Argos Translate en→zh offline
```

通过 `tools/argos_translate_worker.py` 的受限 JSON 子进程调用：最多 256 条、单条 1500 字符、总量 80,000 字符；`shell=False`、CPU 单线程、无控制台。模型缺失或输入错误立即停止，运行时瞬态只局部重试一次。模型与 Python 路径由本机环境变量配置，不写入公共仓库。翻译前释放 Whisper 模型，phrase ASR 时再按需加载，避免 Chrome、Whisper 与 Argos 同时形成内存峰值。

只有服务管理员显式设置 `INFLOW_TRANSLATION_BACKEND=google` 时，链才变为：

```text
Google Translate batch
→ timeout / 429 / marker failure
→ Argos offline
```

Google 路径只发送当前待翻译的英文 cue/surface，不发送视频文件、学习账本或凭据；该模式没有 SLA，必须按隐私政策披露。批量标记必须逐条、按序完整返回，否则整批切到离线层。显式 OpenAI/DSH 模型同样位于默认翻译层之前，失败后只回到管理员选择的翻译后端。

所有启发式路径复用代码侧 wordfreq 候选规则，每 cue 最多一个，并继续经过 surface grounding、句界、字幕交叉事实、MP3 时长与无提示 ASR 门。manifest 分别记录 `translator_chain` 与实际 `translator` runtime；启发式结果是低置信候选，不能宣称与 LLM 选词质量等价。经 Agent 逐项复核的正式样片必须生成新的 reviewed pack，不原地修改自动包。

Argos 当前是本机已安装依赖，不等于公开发行包已经包含模型。

## 7. 候选物化（全部由可信代码生成）

通过模型契约后，代码用 `cue_id` 找回原 cue，并生成：

- 仅 `following_gap_sec >= 0.20` 的 cue 可成为教学候选；没有安全停顿就保持沉默；
- phrase 音频最多借用 100ms 前空隙和 120ms 后空隙，padding 不得越过相邻语音；
- anchor 位于 spoken cue 结束后的真实空隙内，不能固定切进下一句；
- `phrase_text = cue.text`；
- `phrase_zh = translations[cue_id].text_zh`；
- `spoken_start_sec / spoken_end_sec = cue` 的真实语音起止；
- `phrase_start_sec / phrase_end_sec` 只在相邻无语音空隙内加入上述 padding；
- `target_start_sec / target_end_sec = surface` 对齐词的时间；
- `highlight_start_sec / highlight_end_sec = target - phrase_start`；
- `anchor_sec` 位于 cue 之后最近的 ≥200ms 安全空隙，最多延后 5 秒；`content_end` 与 `safe_pause_anchor` 分开；
- `phrase_audio = phrases/<ordinal>-<occurrence-hash>.mp3`；
- `isolated_word_audio_enabled = false`；
- `alignment_quality = word_timestamp_high`。

occurrence ID 固定为：

```text
<video_id>:<cue_id>:<surface>
```

因此同形词在不同视频中一定是不同 occurrence；本模块没有跨视频合并、掌握度推断或学习画像写入。

每个 MP3 使用完整 cue，并只在无语音空隙内加入边界 padding。ffprobe 实测时长与目标片段时长误差必须不超过 `max(250ms, 5%)`。默认 faster-whisper 还会对每个成品短片段做一次无提示复核：显示英文必须与复核词序完全一致且目标 surface 仍存在；不合格只淘汰该候选，全部被淘汰时才拒绝整包。

## 8. 不可变 pack 与 schema

最终目录只能是：

```text
packs/<pack_id>/
  manifest.json
  source.json
  transcript.json
  captions-zh.json
  audit.json
  video.mp4
  phrases/*.mp3
```

根目录或 phrases 中出现未知文件、目录或 symlink 均审计失败。

`pack_id`：

```text
yt-<video_id>-<12-char-config-fingerprint>
```

指纹包含 schema、builder、prompt、translator identity、transcriber identity 和 media identity。相同视频 + 相同生产配置得到同一不可变目录；配置变化不会覆盖旧 pack。

### manifest.json 最低字段

```json
{
  "schema_version": "inflow.video-pack/1",
  "builder_version": "video-pack-builder/1.0.0",
  "pack_id": "yt-aQ89CG_Yu6I-…",
  "status": "ready",
  "video_id": "aQ89CG_Yu6I",
  "source_url": "https://www.youtube.com/watch?v=aQ89CG_Yu6I",
  "title": "…",
  "duration": 120.0,
  "duration_sec": 120.0,
  "effective_speech_sec": 93.4,
  "transcription_model": "…",
  "translator": "…",
  "prompt_version": "…",
  "versions": {
    "schema": "…",
    "builder": "…",
    "prompt": "…",
    "translator": "…",
    "transcriber": "…",
    "media": "…"
  },
  "hashes": {
    "algorithm": "sha256",
    "files": {
      "video.mp4": "…",
      "source.json": "…",
      "transcript.json": "…",
      "captions-zh.json": "…",
      "phrases/01-….mp3": "…"
    }
  },
  "candidates": [],
  "quality_gates": {"all_passed": true},
  "unverified_boundaries": []
}
```

`source.json` 只保存 allow-list 元数据与下载/ffprobe 质量结果，不保存 yt-dlp 原始格式 URL、cookie、header 或 token。

`transcript.json` 保存英语语言判断、模型标识、所有规范化词时间、有效英语语音秒数与 2–8 秒 cues。`effective_speech_sec` 由可靠词时间区间合并计算，只服务于介入频率预算，不代表学习时长。

`captions-zh.json` 逐 cue 保存同一 `cue_id/start/end` 的中文。

`audit.json` 只在全部检查通过后生成，`passed` 必须为 true，并记录模型字段门、surface grounding、音频对齐、hash 覆盖和 occurrence namespace 检查。

## 9. 原子性、失败、取消和缓存

构建过程：

```text
packs/.<pack_id>.staging-<uuid>/
→ 下载、转录、翻译、裁剪
→ 写 manifest
→ 全量只读审计
→ 写 audit.json
→ 再次全量只读审计
→ 最后一次取消检查
→ os.replace(staging, packs/<pack_id>)
```

规则：

- 最终目录在最后一次审计前绝不存在；
- 取消、网络失败、模型失败、schema 失败、surface 幻觉、ffprobe 失败、hash 失败或提交失败都会清理 staging；
- 失败/取消不创建 final pack，也不写 `data/live`；
- builder 主动拒绝把 `packs_root` 指向任何 `data/live` 子树；
- 已存在的 final pack 从不修改或覆盖；
- 命中缓存时先验证 layout、schema、identity、所有 SHA-256、候选/字幕/时间/occurrence 和 audit；
- 缓存损坏抛出 `CacheIntegrityError`，不会静默重建并覆盖；
- 同 URL + 同配置命中有效缓存时，不运行 yt-dlp、ffmpeg、ffprobe、faster-whisper 或 Translator。

## 10. 离线验证

`tests/test_video_pack.py` 不访问网络、不运行真实 yt-dlp/ffmpeg/ffprobe/dsh/faster-whisper；所有外部边界均为 mock/fake。覆盖：

- URL 与私有/直播/时长/Music 元数据拒绝；
- 自然 cue 边界、短尾合并、无自然分区时拒绝硬切；
- 随机幻觉 surface、半词、模型额外 timing 字段拒绝；
- 取消无 final；翻译失败无 final；
- staging 经一次 `os.replace` 原子提交；
- 同 URL 缓存复用且外部依赖调用数不增加；
- 完整目录、JSON schema、SHA-256、1–16 候选和视频命名空间 occurrence；
- 损坏缓存只拒绝、不覆盖；
- Dsh timeout 一次重试、严格纯 JSON；
- 所有媒体与 Dsh 子进程都显式使用 Windows `CREATE_NO_WINDOW`；
- yt-dlp 720p selector 与 ffmpeg 16 kHz PCM 参数。

建议运行（禁止生成测试缓存/字节码）：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_video_pack.py' -v
```
