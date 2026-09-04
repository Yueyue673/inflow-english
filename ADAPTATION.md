# InFlow English · 长期自适应模型 v2

## 目标

用户只调整 `低 / 中 / 高`。系统长期承担选词、时机、动作、会话降噪和复现窗口，但不把一次操作解释成稳定偏好，也不把教学完成冒充掌握。

## 当前实现

### 数据层

```text
data/live/events.jsonl       追加式事实账本
data/live/profile.json       可从事实派生的长期画像
data/live/sessions/*.json    单次观看状态与恢复点
data/live/transactions/*.json  写前事务；成功后自动清理，崩溃后自动重放
```

所有结构分别带 `schema_version` 与 `policy_version`。历史事件不因算法升级而改写；错误事实以后使用 correction 事件，而不是原地篡改。

一次状态变化可能同时涉及 session、profile 和 event。服务端先原子写入完整 transaction，再依次落盘三个目标；若任一步失败，transaction 保留，下一次健康检查或读取会用同一 event ID 幂等恢复，防止“画像领先事件”或“事件领先画像”。

### 词义状态

每个 `词义 × aural_form_to_sense` 单独维护：

```text
unknown
→ taught                 看过词形和当前义项，只是教学
  └─ recognition evidence  延迟新句三选一；只作为旁证，不改变 stage
→ verified_once          以后：一次独立、无选项自由听音成功
→ verified_delayed       以后：跨时间重复成功
→ stable_for_now         以后：多说话人/多语境仍成功
→ conflicted             以后：强证据与新失败冲突
```

当前切片会写 `unknown → taught`，并在 20–72 小时后低频加入一次新句听音辨认。三选一正确记 `WEAK_SUCCESS`，错误或“没听出来”记 `RECOGNITION_FAILURE`；两者都不会把 `aural_stage` 提升为掌握。`看清了，继续`仍只记录 TEACH、停留时间和声音是否完成。

### 复现窗口

完成教学后只打开未来自然机会窗口：

```text
next_window_start = +20h
next_window_end   = +72h
```

窗口只使词项有资格再次被考虑：

- 没遇到就过去；
- 不产生逾期红点；
- 不生成待清空队列；
- 不把错过记成失败。

### 延迟听音验证

窗口内若存在尚未用过的 followup 材料，下一次观看开始前最多抽 1 题：

```text
新句完整声音
→ 三个中文义项 / 没听出来
→ 正确映射与同一句反馈重放
→ 用户确认后进入视频
```

答题前不显示英文词形、完整句子或正确标记；音频必须完整播放（服务端按 presentation ID 确认）后才能作答。第一次完整播放后的作答才算辨认证据：正确累计 `recognition_success_count`；错误累计 `recognition_failure_count`；“没听出来”单独累计 `recognition_abstention_count`，不写入 `clean_failure_count`。刷新或接管后重听再答只累计 `assisted_probe_count`。跳过、离页、接管中断和声音故障是 `NOT_EVIDENCE`，不触碰 profile。每个 `probe_variant_id` 只在真实作答后消耗一次，被验证词会从同场教学候选中排除。当前只有 8 项通过题目级 QC 进入弱验证；其余照常教学但不验证。

完整交互与证据契约见 [`PROBE-CONTRACT.md`](PROBE-CONTRACT.md)。

## 强度模型

### 显式底座

| 档位 | 当前 2.5 分钟材料总介入上限 | 无验证题时教学 | 有 1 题时教学 |
|---|---:|---:|---:|
| 低 | 1 | 最多 1 | 0 |
| 中 | 2 | 最多 2 | 最多 1 |
| 高 | 4 | 最多 4 | 最多 3 |

验证与教学共享同一个预算，不把“增加证据”变成额外打扰。更长内容仍以中档约 2 次 / 10 分钟、高档约 4 次 / 10 分钟作为待真实校准的初始上限。

候选集合是嵌套的：低档选中的词必然属于中档，中档词也属于高档。调高只增加相邻机会，不更换一套完全不同的学习内容。

### 快速层

一次 seek、页面隐藏、播放器操作、跳过或技术失败只处理当下：

- 结束或避开当前机会；
- 不修改长期档位；
- 技术失败不训练任何用户偏好。

### 会话层

- 第 1 次可比较跳过：只跳过当前项；
- 同一会话累计 2 次：剩余候选临时降低一档；
- 累计 3 次：本视频保持沉默；
- 用户主动改强度时立即清除这层临时推断。

新会话不会继承偶然的坏状态。

### 长期层

自动化只允许降低一档或恢复，不允许高于用户明确选择。

下调至少要求：

- 3 个合格会话；
- 10 个可比较机会；
- 最近三会话总跳过率至少 60%；
- 至少两个会话呈明显负趋势；
- 用户最近一次主动改档后至少 7 天。

恢复要求更慢：调整后至少 5 个合格会话、15 个机会，整体跳过率不高于 20%，最近三会话均稳定。

用户主动改档会开启新的 `preference_epoch`，立即覆盖旧推断。旧会话保留在总历史中供审计，但新 epoch 的 `completed_sessions / comparable_opportunities` 从 0 重新累计，旧档位的负趋势不能参与新档位的自动降级。同一观看中发生改档时，每条 interaction 保存自己的 epoch；session summary 只把当前 epoch 的可比较行为用于新策略。

每个观看 session 同时持有 `owner_client_id + owner_epoch`。新标签页只能看到“另一个页面正在使用”，不能取消原提示、提交其 interaction 或用更高 progress sequence 回写旧位置；用户明确点击“在这里继续”后才转移 owner，旧页面随后失去所有写权限。

## 候选选择

当前使用可解释规则，不训练黑盒模型：

```text
优先级 =
  当前听觉状态需要
+ 词汇通用价值先验
+ 状态不确定性
+ 自然复现窗口匹配
- 最近教学次数惩罚
- 尚未到窗口的硬拒绝
```

随后把候选分散到视频前、中、后，避免短时间连续打断。每个安全点最多一个；页面隐藏、用户 seek、音频/释义未准备好时保持沉默。

## 个人节奏

每次映射从“英文词形和中文义项完全可见”开始计时，到用户点击 `看清了，继续` 为止。

- 只保存 1.5–30 秒内的有效样本；
- 首次样本成为个人基线；
- 后续使用保守 EMA 更新；
- 当前版本不再根据计时自动关掉提示，因此这个值只积累证据，不夺走用户控制权。

## 声音边界策略

当前产品：

- 只播放自然短语；
- 0 个孤立硬切词音被启用；
- Faster Whisper 词时间戳只用于宽容的视觉高亮；
- 14/16 为高置信视觉窗口，2/16 使用更宽的人工时间码后备；后备项只显示静态“大概位置”，不运行逐帧 active 高亮；
- 词级偏差实测最大为起点 120 ms、终点 140 ms。

以后要重新启用孤立词音，必须满足：逐字稿一致、MFA 音素级 forced alignment、第二套边界交叉验证、人耳盲听无截头/截尾/邻词泄漏。Whisper 的 token probability 不能被当作边界置信度。

## 何时升级复杂模型

- **现在：**事件账本＋规则状态机＋迟滞。
- **HLR / FSRS：**有跨周/月、题型稳定、低泄露的真实听音成败序列，并在时间切分留出集上改善校准后，只替换复现窗口估计。
- **BKT：**出现高频、边界明确的微技能序列后再考虑，不直接承担稀疏词义长期模型。
- **IRT：**有跨用户稳定题库和足够题目反应后，才用于冷启动能力与题目校准。
- **Contextual bandit：**至少两种教学动作已分别证明有效，并记录随机 propensity、稳定上下文和延迟学习奖励后，才在安全候选内排序。

任何模型都不能绕过页面可见、音频可靠、句末安全点、打断预算和用户显式强度。

## 主要依据

- Corbett & Anderson, Knowledge Tracing — https://doi.org/10.1007/BF01099821
- Settles & Meeder, Half-Life Regression — https://aclanthology.org/P16-1174/
- Amershi et al., Guidelines for Human-AI Interaction — https://doi.org/10.1145/3290605.3300233
- McAuliffe et al., Montreal Forced Aligner — https://www.isca-archive.org/interspeech_2017/mcauliffe17_interspeech.html
- Bain et al., WhisperX — https://www.isca-archive.org/interspeech_2023/bain23_interspeech.pdf
- Schneider et al., Signaling meta-analysis — https://doi.org/10.1016/j.edurev.2017.11.001
- Rey et al., Segmentation meta-analysis — https://doi.org/10.1007/s10648-018-9456-4
