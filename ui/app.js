const INTENSITY_RANK = { low: 0, medium: 1, high: 2 };
const SESSION_KEY = "inflow-adaptive-session";
const CLIENT_INSTANCE_KEY = "inflow-adaptive-client-instance";
const CLIENT_ID = sessionStorage.getItem(CLIENT_INSTANCE_KEY) || crypto.randomUUID().replaceAll("-", "");
sessionStorage.setItem(CLIENT_INSTANCE_KEY, CLIENT_ID);

const home = document.querySelector("#home");
const startButton = document.querySelector("#startButton");
const importForm = document.querySelector("#importForm");
const videoUrl = document.querySelector("#videoUrl");
const prepareButton = document.querySelector("#prepareButton");
const importStatus = document.querySelector("#importStatus");
const packList = document.querySelector("#packList");
const libraryCount = document.querySelector("#libraryCount");
const probePanel = document.querySelector("#probePanel");
const probePhase = document.querySelector("#probePhase");
const probeTitle = document.querySelector("#probeTitle");
const probeContent = document.querySelector("#probeContent");
const probeActions = document.querySelector("#probeActions");
const probeSkip = document.querySelector("#probeSkip");
const watchPanel = document.querySelector("#watchPanel");
const completePanel = document.querySelector("#completePanel");
const errorPanel = document.querySelector("#errorPanel");
const sourceVideo = document.querySelector("#sourceVideo");
const captionLine = document.querySelector("#captionLine");
const learningLayer = document.querySelector("#learningLayer");
const learningPhase = document.querySelector("#learningPhase");
const learningTitle = document.querySelector("#learningTitle");
const learningContent = document.querySelector("#learningContent");
const learningActions = document.querySelector("#learningActions");
const skipLearning = document.querySelector("#skipLearning");
const helperAudio = document.querySelector("#helperAudio");
const watchStatus = document.querySelector("#watchStatus");
const resumeButton = document.querySelector("#resumeButton");
const libraryButton = document.querySelector("#libraryButton");
const liveStatus = document.querySelector("#liveStatus");
const brandState = document.querySelector("#brandState");

let profile = null;
let session = null;
let packs = [];
let selectedPackId = null;
let currentImportJob = null;
let importPollTimer = null;
let captions = [];
let activeInteraction = null;
let interactionEpoch = 0;
let mappingResolve = null;
let previousTime = 0;
let seeking = false;
let handledIds = new Set();
let encounteredIds = new Set();
let progressSeq = 0;
let lastProgressAt = 0;
let expectedSystemPlay = 0;
let expectedSystemPause = 0;
let expectedSystemSeek = 0;
let pauseOwned = false;
let userGeneration = 0;
let leaseGeneration = 0;
let watchStartedPerf = 0;
let manualSeeks = 0;
let technicalFailures = 0;
let sessionSkipCount = 0;
let probeAudioConfirmed = false;
let probeAudioFailed = false;
let probeChoiceShownAt = 0;
let probePlaybackEpoch = 0;
let probeSubmitting = false;
let videoFrameHandle = null;
let videoMonitorMode = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function assetUrl(path) {
  const value = String(path || "");
  return value.startsWith("/") ? value : `/${value}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const error = new Error(payload.detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function announce(text) {
  liveStatus.textContent = "";
  requestAnimationFrame(() => { liveStatus.textContent = text; });
}

function focusSoon(selector) {
  requestAnimationFrame(() => document.querySelector(selector)?.focus({ preventScroll: true }));
}

function showOnly(target) {
  for (const section of [home, probePanel, watchPanel, completePanel, errorPanel]) section.hidden = section !== target;
}

function intensityAllows(current, minimum) {
  return INTENSITY_RANK[current] >= INTENSITY_RANK[minimum];
}

function ownerFields() {
  return { client_id: CLIENT_ID, owner_epoch: Number(session?.owner_epoch || 0) };
}

async function playSourceAsSystem() {
  if (!sourceVideo.paused) return;
  expectedSystemPlay += 1;
  try {
    await sourceVideo.play();
  } catch (error) {
    expectedSystemPlay = Math.max(0, expectedSystemPlay - 1);
    throw error;
  }
}

function pauseSourceAsSystem() {
  if (sourceVideo.paused) return;
  expectedSystemPause += 1;
  sourceVideo.pause();
}

function seekSourceAsSystem(time) {
  if (Math.abs(sourceVideo.currentTime - time) < 0.01) return;
  expectedSystemSeek += 1;
  sourceVideo.currentTime = time;
}

function transitionDuration() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 140;
}

async function revealLearningLayer() {
  learningLayer.classList.add("is-faded");
  learningLayer.hidden = false;
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  learningLayer.classList.remove("is-faded");
  const duration = transitionDuration();
  if (duration) await new Promise((resolve) => setTimeout(resolve, duration));
}

async function concealLearningLayer() {
  if (learningLayer.hidden) return;
  learningLayer.classList.add("is-faded");
  const duration = transitionDuration();
  if (duration) await new Promise((resolve) => setTimeout(resolve, duration));
  learningLayer.hidden = true;
  learningLayer.classList.remove("is-faded");
}

function renderIntensity() {
  if (!profile) return;
  document.querySelectorAll('input[name="intensity"]').forEach((input) => {
    input.checked = input.value === profile.explicit_intensity;
  });
  brandState.textContent = profile.adaptive_reduced
    ? `保持${{ low: "低", medium: "中", high: "高" }[profile.explicit_intensity]}档，当前自动少打扰`
    : "边看边听懂";
}

async function changeIntensity(event) {
  if (!event.target.matches('input[name="intensity"]')) return;
  const previous = profile.explicit_intensity;
  try {
    profile = await api("/api/profile/intensity", {
      method: "PUT",
      body: { intensity: event.target.value },
    });
    if (session) session.profile = profile;
    sessionSkipCount = 0;
    renderIntensity();
    if (packs.length) renderPackList();
  } catch (error) {
    profile.explicit_intensity = previous;
    renderIntensity();
    showError(`介入频率没有保存：${error.message}`, false);
  }
}

document.querySelector(".intensity")?.addEventListener("change", changeIntensity);

function showError(message, replacePage = true) {
  if (!replacePage) {
    brandState.textContent = message;
    announce(message);
    return;
  }
  showOnly(errorPanel);
  errorPanel.innerHTML = `
    <p class="eyebrow">暂时没有丢失记录</p>
    <h1>当前页面没有继续</h1>
    <p class="lead error-copy">${escapeHtml(message)}</p>
    <button id="retryButton" class="primary-button" type="button">重试恢复</button>
  `;
  document.querySelector("#retryButton")?.addEventListener("click", () => location.reload());
  focusSoon("#retryButton");
}

function renderProbeFrame(phase, title, content, actions = "", allowSkip = true) {
  showOnly(probePanel);
  probePhase.textContent = phase;
  probeTitle.textContent = title;
  probeContent.innerHTML = content;
  probeActions.innerHTML = actions;
  probeSkip.hidden = !allowSkip;
  probeSkip.disabled = false;
}

function renderFollowupSentence(feedback) {
  const sentence = String(feedback.followup_sentence || "");
  const target = String(feedback.surface || "");
  const index = sentence.toLocaleLowerCase().indexOf(target.toLocaleLowerCase());
  if (index < 0) return `<p class="probe-sentence">${escapeHtml(sentence)}</p>`;
  return `<p class="probe-sentence">${escapeHtml(sentence.slice(0, index))}<mark>${escapeHtml(sentence.slice(index, index + target.length))}</mark>${escapeHtml(sentence.slice(index + target.length))}</p>`;
}

function renderProbeChoices() {
  const probe = session?.probe;
  if (!probe || session.stage !== "probe") return;
  probeChoiceShownAt = performance.now();
  const buttons = probe.choices.map((choice) => (
    `<button class="probe-choice" type="button" data-choice-id="${escapeHtml(choice.choice_id)}">${escapeHtml(choice.label)}</button>`
  )).join("");
  renderProbeFrame(
    "延迟听音 · 本次最多一题",
    "刚才那句话里，哪个意思对得上？",
    `<p class="probe-note">只按刚才听到的声音选。现在仍不显示英文。</p>`,
    `<div class="probe-choice-grid" role="group" aria-label="选择听到的意思">${buttons}</div>
     <button id="probeDontKnow" class="secondary-button" type="button">没听出来</button>`,
  );
  document.querySelectorAll(".probe-choice").forEach((button) => {
    button.addEventListener("click", () => submitProbe("selected", button.dataset.choiceId), { once: true });
  });
  document.querySelector("#probeDontKnow")?.addEventListener("click", () => submitProbe("dont_know"), { once: true });
  focusSoon("#probeTitle");
  announce("声音播放完成。请选择最接近的中文意思，或者选择没听出来。");
}

function renderProbeInterrupted() {
  probeAudioConfirmed = false;
  probeAudioFailed = false;
  probeChoiceShownAt = 0;
  renderProbeFrame(
    "听音已暂停",
    "回到页面后重新听",
    `<p class="probe-note">离开页面不会被记成错误；重新播放后才会出现选项。</p>`,
    `<button id="probeRestartAudio" class="primary-button" type="button">重新播放</button>`,
  );
  document.querySelector("#probeRestartAudio")?.addEventListener("click", () => beginProbePlayback(), { once: true });
  focusSoon("#probeRestartAudio");
}

function renderProbeAudioFailure() {
  probeAudioFailed = true;
  renderProbeFrame(
    "声音没有正常播放",
    "这次不会记成对或错",
    `<p class="probe-note error-copy">可以重新播放；如果先看视频，这次只记技术故障。</p>`,
    `<button id="probeRetryAudio" class="primary-button" type="button">重新播放</button>`,
  );
  document.querySelector("#probeRetryAudio")?.addEventListener("click", () => beginProbePlayback(), { once: true });
  focusSoon("#probeRetryAudio");
  announce("声音没有正常播放。这次不会记成你不会。");
}

async function beginProbePlayback() {
  const probe = session?.probe;
  if (!probe || session.stage !== "probe") return;
  const epoch = ++probePlaybackEpoch;
  const presentationId = crypto.randomUUID().replaceAll("-", "");
  probeAudioConfirmed = false;
  probeAudioFailed = false;
  probeChoiceShownAt = 0;
  renderProbeFrame(
    "延迟听音 · 本次最多一题",
    "先听一句",
    `<div class="probe-listening"><span class="listening-dot" aria-hidden="true"></span><p>先只听声音，英文和答案会暂时隐藏。</p></div>`,
    "",
  );
  announce("先听一句。声音结束前不会显示英文或答案。");
  const played = await playAudioUrl(probe.audio_url);
  if (epoch !== probePlaybackEpoch || session?.stage !== "probe") return;
  if (played.kind === "ended" && played.confirmed) {
    try {
      session = await api(`/api/sessions/${session.session_id}/probe/presentation`, {
        method: "POST",
        body: { ...ownerFields(), probe_id: probe.probe_id, presentation_id: presentationId },
      });
      profile = session.profile;
      probeAudioConfirmed = true;
      renderProbeChoices();
    } catch (error) {
      console.error(error);
      renderProbeAudioFailure();
    }
  } else {
    renderProbeAudioFailure();
  }
}

async function submitProbe(outcome, selectedChoiceId = null) {
  if (probeSubmitting || !session?.probe || session.stage !== "probe") return;
  probeSubmitting = true;
  probeSkip.disabled = true;
  document.querySelectorAll("#probePanel button").forEach((button) => { button.disabled = true; });
  ++probePlaybackEpoch;
  stopHelper("probe_submitted");
  try {
    session = await api(`/api/sessions/${session.session_id}/probe/complete`, {
      method: "POST",
      body: {
        ...ownerFields(),
        probe_id: session.probe.probe_id,
        outcome,
        selected_choice_id: selectedChoiceId,
        audio_confirmed: probeAudioConfirmed,
        response_ms: probeChoiceShownAt ? Math.round(performance.now() - probeChoiceShownAt) : 0,
      },
    });
    profile = session.profile;
    renderIntensity();
    if (outcome === "skipped" || outcome === "technical_failure") {
      await continueAfterProbe();
    } else {
      renderProbeFeedback(true);
    }
  } catch (error) {
    if (error.status === 409) {
      try {
        const fresh = await api(`/api/sessions/${session.session_id}`);
        session = fresh;
        profile = fresh.profile;
      } catch {
        // keep the local session state when even the read fails
      }
      renderOwnershipConflict();
      return;
    }
    showError(`这次听音记录没有保存：${error.message}`);
  } finally {
    probeSubmitting = false;
  }
}

function renderProbeFeedback(autoReplay = false) {
  if (session?.owner_client_id && session.owner_client_id !== CLIENT_ID) {
    renderOwnershipConflict();
    return;
  }
  const probe = session?.probe;
  const result = probe?.result;
  const feedback = probe?.feedback;
  if (!probe || !result) return;
  probeSkip.hidden = true;
  if (!feedback) {
    renderProbeFrame(
      "这次没有评分",
      "没有记成对或错",
      `<p class="probe-note">跳过和技术故障都不会改变你的听力画像。</p>`,
      `<button id="probeContinueToVideo" class="primary-button" type="button">开始观看</button>`,
      false,
    );
  } else {
    const correct = result.outcome === "correct";
    const title = correct ? "这次声音和意思对上了" : "这次还没对上";
    const note = result.evidence_quality === "ASSISTED_PRACTICE"
      ? "因为作答前重新听过，这次只记作辅助练习。"
      : correct
      ? "这次只留下一条听音记录，不会直接算作掌握。"
      : "现在显示的是正确映射；刚才的结果只描述这一次。";
    renderProbeFrame(
      "声音和正确意思",
      title,
      `<div class="probe-feedback">
         <p class="mapping-word">${escapeHtml(feedback.surface)}</p>
         <p class="mapping-gloss">${escapeHtml(feedback.gloss_zh)}</p>
         ${renderFollowupSentence(feedback)}
         <p class="probe-note">${escapeHtml(note)}</p>
       </div>`,
      `<button id="probeContinueToVideo" class="primary-button" type="button">开始观看</button>
       <button id="probeFeedbackReplay" class="replay-button" type="button">再听一遍</button>`,
      false,
    );
    document.querySelector("#probeFeedbackReplay")?.addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      await playAudioUrl(feedback.audio_url);
      event.currentTarget.disabled = false;
    });
    if (autoReplay) playAudioUrl(feedback.audio_url);
  }
  document.querySelector("#probeContinueToVideo")?.addEventListener("click", () => continueAfterProbe(), { once: true });
  focusSoon("#probeContinueToVideo");
  announce(feedback ? `${feedback.surface}，${feedback.gloss_zh}。反馈会保留到你开始观看。` : "这次没有评分，可以开始观看。");
}

async function continueAfterProbe() {
  if (!session || session.stage !== "probe_feedback") return;
  try {
    ++probePlaybackEpoch;
    stopHelper("probe_feedback_done");
    session = await api(`/api/sessions/${session.session_id}/start`, {
      method: "POST",
      body: ownerFields(),
    });
    profile = session.profile;
    if (!(await prepareWatch())) return;
    watchStartedPerf = performance.now();
    await playSourceAsSystem();
    resumeButton.hidden = true;
  } catch (error) {
    showError(`没有进入视频：${error.message}`);
  }
}

async function renderProbe() {
  if (session?.owner_client_id && session.owner_client_id !== CLIENT_ID) {
    renderOwnershipConflict();
    return;
  }
  if (!session?.probe) {
    showError("听音验证材料没有准备好。");
    return;
  }
  if (session.stage === "probe_feedback" || session.probe.result) {
    renderProbeFeedback(false);
    return;
  }
  await beginProbePlayback();
}

function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function selectedPack() {
  return packs.find((pack) => pack.pack_id === selectedPackId) || null;
}

function renderPackList() {
  libraryCount.textContent = `${packs.filter((pack) => !pack.fixture).length} 个真实视频`;
  const frequency = profile?.effective_frequency || profile?.effective_intensity || "medium";
  const frequencyLabel = { low: "低频", medium: "中频", high: "高频" }[frequency] || "当前";
  packList.innerHTML = packs.map((pack) => {
    const active = Boolean(pack.active_session_id);
    const state = pack.fixture ? "回归样片" : active ? "有观看进度" : "已准备";
    const candidateCount = Number(pack.candidate_count || 0);
    const eligibleCount = Number(pack.eligible_candidate_count ?? candidateCount);
    const expected = Number(pack.intervention_budgets?.[frequency] ?? pack.expected_interventions ?? 0);
    return `
      <button class="pack-option" type="button" role="option" data-pack-id="${escapeHtml(pack.pack_id)}" aria-selected="${pack.pack_id === selectedPackId}">
        <span>
          <span class="pack-title">${escapeHtml(pack.title)}</span>
          <span class="pack-meta">${formatDuration(pack.duration_sec)} · 发现 ${candidateCount} 个 · 当前可选 ${eligibleCount} 个 · ${frequencyLabel}预计介入 ${expected} 次 · ${state}</span>
        </span>
        <span class="pack-marker" aria-hidden="true"></span>
      </button>
    `;
  }).join("");
  packList.querySelectorAll(".pack-option").forEach((button) => {
    button.addEventListener("click", () => {
      selectedPackId = button.dataset.packId;
      renderPackList();
      updateStartButton();
    });
  });
  updateStartButton();
}

function updateStartButton() {
  const pack = selectedPack();
  startButton.disabled = !pack;
  if (!pack) {
    startButton.textContent = "还没有可看的视频";
    return;
  }
  const continuing = Boolean(pack.active_session_id || (session && session.pack_id === pack.pack_id));
  startButton.textContent = continuing ? `继续《${pack.title}》` : `观看《${pack.title}》`;
}

async function refreshPacks(preferredPackId = null) {
  const payload = await api("/api/packs");
  packs = payload.packs || [];
  const availableIds = new Set(packs.map((pack) => pack.pack_id));
  if (preferredPackId && availableIds.has(preferredPackId)) selectedPackId = preferredPackId;
  else if (session?.pack_id && availableIds.has(session.pack_id)) selectedPackId = session.pack_id;
  else if (!selectedPackId || !availableIds.has(selectedPackId)) selectedPackId = packs.find((pack) => !pack.fixture)?.pack_id || packs[0]?.pack_id || null;
  renderPackList();
}

const IMPORT_STAGE_COPY = {
  queued: "等待开始",
  validating: "检查视频是否支持",
  downloading: "获取视频和中文字幕",
  extracting_audio: "提取原声",
  transcribing: "识别英语和词时间",
  segmenting: "寻找自然分句与安全停顿",
  translating: "选择少量值得学的表达",
  selecting: "核对词义与原句",
  clipping: "生成对应的原声片段",
  auditing: "最后校验声音、文字和文件",
};

const IMPORT_ERROR_COPY = {
  interrupted_import: "上次准备被程序重启中断，可以重新开始。",
  chinese_auto_captions_missing: "这个视频暂时拿不到中文字幕。",
  transcript_primary_language_not_english: "这个视频的主要语音不是英语。",
  transcript_invalid_word_timing_ratio_too_high: "这个视频有局部语音时间无法可靠对齐。",
  transcriber_memory_unavailable: "电脑当前内存较紧，轻量转录也没能启动；稍后会从这里重试。",
  transcriber_runtime_failed: "语音识别暂时失败，可以稍后从原链接重试。",
  google_translation_failed: "在线备用翻译暂时不可用，系统会尝试本地翻译。",
  argos_translation_unavailable: "本地翻译组件未就绪，可以稍后从原链接重试。",
  argos_en_zh_model_missing: "本地英译中模型未就绪，可以稍后从原链接重试。",
  argos_memory_unavailable: "电脑当前内存较紧，本地翻译没有启动；稍后可从原链接重试。",
  argos_translation_failed: "本地翻译暂时失败，可以稍后从原链接重试。",
  argos_translation_runtime_failed: "本地翻译暂时失败，可以稍后从原链接重试。",
  argos_runtime_runtimeerror: "本地翻译暂时失败，可以稍后从原链接重试。",
  source_duration_out_of_range: "当前支持 20 秒到 15 分钟的视频。",
  no_natural_2_to_8_second_partition: "没有找到足够可靠的自然分句。",
  yt_dlp_download_failed: "视频获取失败，网络或 YouTube 暂时拒绝了请求。",
  yt_dlp_chinese_captions_failed: "中文字幕获取失败，可以稍后重试。",
  dsh_translation_failed_after_retry: "词义准备没有完成，可以重试。",
};

function renderImportJob(job) {
  currentImportJob = job;
  importStatus.hidden = false;
  importStatus.classList.toggle("is-error", job.status === "failed");
  if (job.status === "ready") {
    importStatus.innerHTML = `<strong>《${escapeHtml(job.title || "这个视频")}》准备好了。</strong>`;
    refreshPacks(job.pack_id).then(() => {
      if (!home.hidden) renderHome();
    });
    return;
  }
  if (job.status === "cancelled") {
    importStatus.innerHTML = "已取消准备；没有写入学习记录。";
    return;
  }
  if (job.status === "failed") {
    const message = IMPORT_ERROR_COPY[job.error_code] || "准备过程中遇到未预料的问题；原链接和失败记录已保留，可以重试。";
    importStatus.innerHTML = `${message}<div class="import-status-actions"><button id="retryImport" class="text-button" type="button">用原链接重试</button></div>`;
    document.querySelector("#retryImport")?.addEventListener("click", () => importForm.requestSubmit());
    return;
  }
  const copy = IMPORT_STAGE_COPY[job.stage] || "正在准备";
  const percent = Math.round(Math.max(0, Math.min(1, Number(job.fraction) || 0)) * 100);
  importStatus.innerHTML = `<strong>${copy}</strong><div class="import-progress" aria-hidden="true"><span style="width:${percent}%"></span></div><div class="import-status-actions"><button id="cancelImport" class="text-button" type="button">取消准备</button></div>`;
  document.querySelector("#cancelImport")?.addEventListener("click", async () => {
    const cancelled = await api(`/api/imports/${job.job_id}/cancel`, { method: "POST" });
    renderImportJob(cancelled);
  });
}

async function pollImport(jobId) {
  clearTimeout(importPollTimer);
  try {
    const job = await api(`/api/imports/${jobId}`);
    renderImportJob(job);
    if (job.status === "queued" || job.status === "running") {
      importPollTimer = setTimeout(() => pollImport(jobId), 1000);
    }
  } catch (error) {
    importStatus.hidden = false;
    importStatus.classList.add("is-error");
    importStatus.textContent = `准备状态暂时读不到：${error.message}`;
  }
}

async function submitImport(event) {
  event.preventDefault();
  prepareButton.disabled = true;
  importStatus.classList.remove("is-error");
  try {
    const job = await api("/api/imports", { method: "POST", body: { url: videoUrl.value.trim() } });
    renderImportJob(job);
    if (job.status === "queued" || job.status === "running") await pollImport(job.job_id);
  } catch (error) {
    importStatus.hidden = false;
    importStatus.classList.add("is-error");
    importStatus.textContent = error.status === 422 ? "请粘贴一个普通的 YouTube 视频链接。" : `没有开始准备：${error.message}`;
  } finally {
    prepareButton.disabled = false;
  }
}

function renderHome() {
  showOnly(home);
  renderPackList();
  renderIntensity();
  brandState.textContent = "选择或准备视频";
  focusSoon(packs.some((pack) => !pack.fixture) ? "#startButton" : "#videoUrl");
}

function captionAt(time) {
  const sourceTime = time + Number(session?.video?.source_offset_sec || 0);
  for (let index = captions.length - 1; index >= 0; index -= 1) {
    const segment = captions[index];
    if (sourceTime >= Number(segment.start) && sourceTime <= Number(segment.end)) return segment;
  }
  return null;
}

async function loadCaptions() {
  const response = await fetch(assetUrl(session.video.captions), { cache: "no-store" });
  if (!response.ok) throw new Error("中文字幕没有准备好");
  const payload = await response.json();
  captions = Array.isArray(payload) ? payload : payload.segments || [];
}

async function ensureVideoMetadata() {
  if (sourceVideo.readyState >= 1) return;
  await new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("视频信息加载超时")), 8000);
    const finish = (callback) => {
      clearTimeout(timer);
      sourceVideo.removeEventListener("loadedmetadata", onLoaded);
      sourceVideo.removeEventListener("error", onError);
      callback();
    };
    const onLoaded = () => finish(resolve);
    const onError = () => finish(() => reject(new Error("视频信息没有加载")));
    sourceVideo.addEventListener("loadedmetadata", onLoaded, { once: true });
    sourceVideo.addEventListener("error", onError, { once: true });
  });
}

function renderPhrase(item, active = false) {
  const phrase = String(item.phrase_text || item.surface);
  const target = String(item.surface);
  const index = phrase.toLocaleLowerCase().indexOf(target.toLocaleLowerCase());
  if (index < 0) return `<p class="mapping-context">${escapeHtml(phrase)}</p>`;
  const before = phrase.slice(0, index);
  const match = phrase.slice(index, index + target.length);
  const after = phrase.slice(index + target.length);
  const markClasses = [active ? "active" : "", item.alignment_quality === "manifest_fallback_wide" ? "approximate" : ""]
    .filter(Boolean)
    .join(" ");
  return `<p class="mapping-context">${escapeHtml(before)}<mark id="targetMark" class="${markClasses}">${escapeHtml(match)}</mark>${escapeHtml(after)}</p>`;
}

function setLearningView(phase, title, content, actions = "") {
  learningPhase.textContent = phase;
  learningTitle.textContent = title;
  learningContent.innerHTML = content;
  learningActions.innerHTML = actions;
}

function stopHelper(reason = "stopped") {
  helperAudio.pause();
  helperAudio.removeAttribute("src");
  helperAudio.load();
  helperAudio.dataset.stopReason = reason;
}

function playAudioUrl(url, onTime = null) {
  stopHelper("new_audio");
  return new Promise((resolve) => {
    let settled = false;
    let playing = false;
    let timeoutHandle = null;
    const finish = (kind) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutHandle);
      helperAudio.removeEventListener("playing", onPlaying);
      helperAudio.removeEventListener("ended", onEnded);
      helperAudio.removeEventListener("error", onError);
      helperAudio.removeEventListener("timeupdate", onTick);
      resolve({ kind, confirmed: playing && kind === "ended" });
    };
    const onPlaying = () => { playing = true; };
    const onEnded = () => finish("ended");
    const onError = () => finish("error");
    const onTick = () => onTime?.(helperAudio.currentTime);
    helperAudio.src = url;
    helperAudio.currentTime = 0;
    helperAudio.playbackRate = new URLSearchParams(location.search).get("qa") === "1" ? 4 : 1;
    helperAudio.muted = new URLSearchParams(location.search).get("qa") === "1";
    helperAudio.addEventListener("playing", onPlaying);
    helperAudio.addEventListener("ended", onEnded);
    helperAudio.addEventListener("error", onError);
    helperAudio.addEventListener("timeupdate", onTick);
    timeoutHandle = window.setTimeout(() => finish("timeout"), 12_000);
    const promise = helperAudio.play();
    promise?.catch?.(() => finish("error"));
  });
}

function playPhrase(item, onTime = null) {
  return playAudioUrl(assetUrl(item.phrase_audio), onTime);
}

function mappingMarkup(item, note = "") {
  const alignmentCopy = item.alignment_quality === "word_timestamp_high"
    ? ""
    : `<p class="alignment-copy">声音位置为大概范围</p>`;
  return `
    <div class="mapping">
      <p class="mapping-word">${escapeHtml(item.surface)}</p>
      <p class="mapping-gloss">${escapeHtml(item.gloss_zh)}</p>
      <p class="excerpt-label">原声片段</p>
      ${renderPhrase(item, false)}
      <p class="mapping-translation">${escapeHtml(item.phrase_zh || "")}</p>
      ${alignmentCopy}
      <p id="mappedAudioState" class="audio-state" role="status">${escapeHtml(note)}</p>
    </div>
  `;
}

function playMappedPhrase(item, epoch) {
  const state = document.querySelector("#mappedAudioState");
  if (state) state.textContent = "正在播放原声片段";
  return playPhrase(item, (time) => {
    const mark = document.querySelector("#targetMark");
    if (!mark || item.alignment_quality !== "word_timestamp_high") return;
    mark.classList.toggle("active", time >= Number(item.highlight_start_sec) && time <= Number(item.highlight_end_sec));
  }).then((result) => {
    if (!activeInteraction || activeInteraction.epoch !== epoch || activeInteraction.finalized) return result;
    document.querySelector("#targetMark")?.classList.remove("active");
    const currentState = document.querySelector("#mappedAudioState");
    if (currentState) currentState.textContent = result.kind === "ended" && result.confirmed ? "" : "声音没有正常播放，可以再听一次";
    return result;
  });
}

function waitForStableMapping(item, epoch, firstAudioConfirmed) {
  return new Promise((resolve) => {
    mappingResolve = resolve;
    setLearningView(
      "这次感觉",
      "这个表达对你来说？",
      mappingMarkup(item, firstAudioConfirmed ? "" : "第一遍没有正常播放，正在重新播放"),
      `<button id="mappingKnown" data-feedback="known" class="primary-button" type="button" disabled>听到就懂 <kbd>1</kbd></button>
       <button id="mappingContinue" data-feedback="familiar" class="replay-button" type="button" disabled>有点熟 <kbd>2</kbd></button>
       <button id="mappingUnclear" data-feedback="unclear" class="replay-button" type="button" disabled>还不清楚 <kbd>3</kbd></button>
       <button id="mappingReplay" class="text-button" type="button" disabled>再听一遍 <kbd>R</kbd></button>`,
    );
    skipLearning.hidden = true;
    const feedbackButtons = [...document.querySelectorAll("[data-feedback]")];
    const continueButton = document.querySelector("#mappingContinue");
    const replayButton = document.querySelector("#mappingReplay");
    feedbackButtons.forEach((button) => button.addEventListener("click", () => resolve(button.dataset.feedback), { once: true }));
    const setControlsDisabled = (disabled) => {
      feedbackButtons.forEach((button) => { button.disabled = disabled; });
      replayButton.disabled = disabled;
    };
    const runReplay = async (manual) => {
      if (!activeInteraction || activeInteraction.epoch !== epoch || activeInteraction.finalized) return;
      setControlsDisabled(true);
      if (manual) activeInteraction.replays += 1;
      const result = await playMappedPhrase(item, epoch);
      if (!activeInteraction || activeInteraction.epoch !== epoch || activeInteraction.finalized) return;
      if (result.kind === "ended" && result.confirmed) activeInteraction.secondConfirmed = true;
      setControlsDisabled(false);
      focusSoon("#mappingContinue");
    };
    replayButton.addEventListener("click", () => runReplay(true));
    announce(`${item.surface}，${item.gloss_zh}。选择听到就懂、有点熟或还不清楚后继续；R 可以重听。`);
    runReplay(false);
  });
}

async function saveInteraction(item, interactionId, outcome, dwellMs, phraseConfirmed, replays, familiarityFeedback = null, failureReason = null) {
  try {
    session = await api(`/api/sessions/${session.session_id}/interactions/${item.id}/complete`, {
      method: "POST",
      body: {
        ...ownerFields(),
        interaction_id: interactionId,
        outcome,
        dwell_ms: Math.round(dwellMs),
        phrase_confirmed: phraseConfirmed,
        replays,
        familiarity_feedback: familiarityFeedback,
        failure_reason: failureReason,
      },
    });
    profile = session.profile;
    sessionSkipCount = Number(session.current_epoch_skipped || 0);
    handledIds.add(item.id);
    renderIntensity();
    return true;
  } catch (error) {
    showError(`这次学习记录没有保存：${error.message}`, false);
    return false;
  }
}

async function resumeAfterInteraction(shouldResume, generation) {
  const concealPromise = concealLearningLayer();
  skipLearning.hidden = false;
  skipLearning.disabled = false;
  sourceVideo.inert = false;
  captionLine.textContent = captionAt(sourceVideo.currentTime)?.text || "";
  if (shouldResume && generation === userGeneration && !sourceVideo.ended) {
    try {
      await playSourceAsSystem();
      resumeButton.hidden = true;
    } catch {
      resumeButton.hidden = false;
    }
  } else if (sourceVideo.paused && !sourceVideo.ended) {
    resumeButton.hidden = false;
  }
  await concealPromise;
}

async function finalizeLearning(outcome, reason = null, familiarityFeedback = null) {
  const interaction = activeInteraction;
  if (!interaction || interaction.finalized) return;
  interaction.finalized = true;
  mappingResolve?.("aborted");
  mappingResolve = null;
  stopHelper(reason || outcome);
  if (outcome === "technical_failure") technicalFailures += 1;
  const dwellMs = interaction.mappingShownAt ? performance.now() - interaction.mappingShownAt : 0;
  const phraseConfirmed = Boolean(interaction.firstConfirmed && interaction.secondConfirmed);
  const savePromise = saveInteraction(
    interaction.item,
    interaction.interactionId,
    outcome,
    dwellMs,
    phraseConfirmed,
    interaction.replays,
    familiarityFeedback,
    reason,
  );
  const resumePromise = resumeAfterInteraction(interaction.pauseOwned, interaction.leaseGeneration);
  const [saved] = await Promise.all([savePromise, resumePromise]);
  if (activeInteraction === interaction) activeInteraction = null;
  if (!saved) announce("视频已经继续，但这次学习记录需要刷新后恢复。");
}

async function beginLearning(item) {
  if (activeInteraction || handledIds.has(item.id) || document.hidden) return;
  if (sessionSkipCount >= 3) {
    handledIds.add(item.id);
    return;
  }
  const baselineRank = INTENSITY_RANK[profile.effective_intensity];
  const sessionReduction = sessionSkipCount >= 2 ? 1 : 0;
  const currentIntensity = Object.keys(INTENSITY_RANK).find(
    (name) => INTENSITY_RANK[name] === Math.max(0, baselineRank - sessionReduction),
  );
  if (!intensityAllows(currentIntensity, item.min_intensity)) {
    handledIds.add(item.id);
    return;
  }

  const startGeneration = userGeneration;
  const epoch = ++interactionEpoch;
  activeInteraction = {
    epoch,
    item,
    interactionId: null,
    pauseOwned: false,
    leaseGeneration: startGeneration,
    replays: 0,
    firstConfirmed: false,
    secondConfirmed: false,
    mappingShownAt: 0,
    finalized: false,
  };

  try {
    const started = await api(`/api/sessions/${session.session_id}/interactions/start`, {
      method: "POST",
      body: { ...ownerFields(), item_id: item.id },
    });
    if (activeInteraction?.epoch !== epoch) return;
    activeInteraction.interactionId = started.interaction.interaction_id;
    const lateBy = sourceVideo.currentTime - Number(item.anchor_sec);
    if (document.hidden || seeking || userGeneration !== startGeneration || sourceVideo.paused || lateBy > 0.18) {
      await finalizeLearning("technical_failure", lateBy > 0.18 ? "safe_boundary_missed" : "user_state_changed_before_pause");
      return;
    }
    pauseOwned = true;
    leaseGeneration = userGeneration;
    activeInteraction.pauseOwned = true;
    activeInteraction.leaseGeneration = leaseGeneration;
    pauseSourceAsSystem();
    sourceVideo.inert = true;
    captionLine.textContent = "";
    setLearningView(
      "原声片段",
      "先听一遍",
      `<div class="listening-state"><span class="listening-dot" aria-hidden="true"></span><span>只听声音</span></div>`,
    );
    skipLearning.hidden = false;
    skipLearning.disabled = false;
    resumeButton.hidden = true;
    const revealPromise = revealLearningLayer();
    focusSoon("#learningTitle");
    const firstPromise = playPhrase(item);
    await revealPromise;
    const first = await firstPromise;
    if (!activeInteraction || activeInteraction.epoch !== epoch || activeInteraction.finalized) return;
    activeInteraction.firstConfirmed = first.kind === "ended" && first.confirmed;
    activeInteraction.mappingShownAt = performance.now();

    const choice = await waitForStableMapping(item, epoch, activeInteraction.firstConfirmed);
    mappingResolve = null;
    if (!activeInteraction || activeInteraction.epoch !== epoch || activeInteraction.finalized || !["known", "familiar", "unclear"].includes(choice)) return;
    const bothConfirmed = activeInteraction.firstConfirmed && activeInteraction.secondConfirmed;
    await finalizeLearning(bothConfirmed ? "completed" : "technical_failure", bothConfirmed ? null : "phrase_audio_unconfirmed", bothConfirmed ? choice : null);
  } catch (error) {
    if (!activeInteraction || activeInteraction.epoch !== epoch || activeInteraction.finalized) return;
    console.error(error);
    if (!activeInteraction.interactionId) {
      activeInteraction = null;
      await concealLearningLayer();
      sourceVideo.inert = false;
      skipLearning.disabled = false;
      showError(`这次学习没有开始：${error.message}`, false);
      return;
    }
    setLearningView(
      "声音暂时没有播放",
      "意思仍然保留",
      mappingMarkup(item, "这次故障不会算成你不会"),
      `<button id="technicalContinue" class="primary-button" type="button">继续视频</button>`,
    );
    document.querySelector("#technicalContinue")?.addEventListener("click", () => finalizeLearning("technical_failure", String(error.message || error)), { once: true });
    focusSoon("#technicalContinue");
  }
}

async function recordEncounter(itemId) {
  if (encounteredIds.has(itemId)) return;
  encounteredIds.add(itemId);
  try {
    await api(`/api/sessions/${session.session_id}/encounters/${itemId}`, {
      method: "POST",
      body: ownerFields(),
    });
  } catch {
    encounteredIds.delete(itemId);
  }
}

function cancelVideoMonitor() {
  if (videoFrameHandle === null) return;
  if (videoMonitorMode === "video" && sourceVideo.cancelVideoFrameCallback) {
    sourceVideo.cancelVideoFrameCallback(videoFrameHandle);
  } else {
    cancelAnimationFrame(videoFrameHandle);
  }
  videoFrameHandle = null;
  videoMonitorMode = null;
}

function scheduleVideoMonitor() {
  if (sourceVideo.paused || sourceVideo.ended || videoFrameHandle !== null) return;
  const tick = () => {
    videoFrameHandle = null;
    videoMonitorMode = null;
    processVideoTime();
    scheduleVideoMonitor();
  };
  if (sourceVideo.requestVideoFrameCallback) {
    videoMonitorMode = "video";
    videoFrameHandle = sourceVideo.requestVideoFrameCallback(tick);
  } else {
    videoMonitorMode = "raf";
    videoFrameHandle = requestAnimationFrame(tick);
  }
}

function processVideoTime() {
  if (!session || session.stage !== "watch" || activeInteraction || seeking || document.hidden) return;
  const current = sourceVideo.currentTime;
  const delta = current - previousTime;
  const normalCrossing = delta >= -0.05 && delta <= 0.75;
  const crossedEncounters = (session.encounter_catalog || [])
    .filter((item) => !encounteredIds.has(item.id) && Number(item.anchor_sec) > previousTime && Number(item.anchor_sec) <= current);
  const crossed = session.items
    .filter((item) => !handledIds.has(item.id) && Number(item.anchor_sec) > previousTime && Number(item.anchor_sec) <= current)
    .sort((a, b) => Number(a.anchor_sec) - Number(b.anchor_sec));
  previousTime = current;
  if (!normalCrossing) {
    crossed.forEach((item) => handledIds.add(item.id));
    return;
  }
  crossedEncounters.forEach((item) => recordEncounter(item.id));
  const candidate = crossed[0];
  if (!candidate) return;
  const lateBy = current - Number(candidate.anchor_sec);
  if (lateBy > 0.18) {
    crossed.forEach((item) => handledIds.add(item.id));
    return;
  }
  crossed.slice(1).forEach((item) => handledIds.add(item.id));
  beginLearning(candidate);
}

async function saveProgress(force = false) {
  if (!session || session.stage !== "watch") return;
  const now = performance.now();
  if (!force && now - lastProgressAt < 5000) return;
  lastProgressAt = now;
  progressSeq += 1;
  try {
    const updated = await api(`/api/sessions/${session.session_id}/progress`, {
      method: "POST",
      body: { ...ownerFields(), sequence: progressSeq, media_time: sourceVideo.currentTime },
    });
    if (updated?.session_id === session.session_id) {
      session.last_media_time = updated.last_media_time;
      session.progress_seq = Math.max(Number(session.progress_seq || 0), Number(updated.progress_seq || 0));
      session.owner_client_id = updated.owner_client_id;
      session.owner_epoch = updated.owner_epoch;
    }
  } catch {
    // The next checkpoint has a higher sequence and can recover this write.
  }
}

async function finishSession() {
  if (!session || session.stage !== "watch" || activeInteraction) return;
  try {
    session = await api(`/api/sessions/${session.session_id}/complete`, {
      method: "POST",
      body: {
        ...ownerFields(),
        source_ended: true,
        elapsed_ms: Math.round(performance.now() - watchStartedPerf),
        manual_seeks: manualSeeks,
        technical_failures: technicalFailures,
      },
    });
    profile = session.profile;
    localStorage.removeItem(SESSION_KEY);
    renderComplete();
  } catch (error) {
    showError(`观看记录没有完成：${error.message}`);
  }
}

function renderComplete() {
  showOnly(completePanel);
  const summary = session.interaction_summary || { completed: 0, skipped: 0, technical_failure: 0 };
  const skipped = Number(summary.skipped || 0);
  const failures = Number(summary.technical_failure || 0);
  const exceptionLine = skipped || failures
    ? `<p class="summary-line">跳过 ${skipped} 次　·　技术故障 ${failures} 次</p>`
    : "";
  completePanel.innerHTML = `
    <p class="eyebrow">这次观看结束</p>
    <h1>这次学了 ${Number(summary.completed || 0)} 个原声表达</h1>
    <p class="lead">记录已保存在本机。以后选择新视频时会继续累积，不要求重看这一条。</p>
    ${exceptionLine}
    <button id="homeButton" class="primary-button" type="button">选择下一个视频</button>
  `;
  document.querySelector("#homeButton")?.addEventListener("click", async () => {
    session = null;
    await refreshPacks();
    renderHome();
  });
  renderIntensity();
  focusSoon("#homeButton");
}

function renderOwnershipConflict() {
  showOnly(errorPanel);
  const finished = session?.stage === "complete";
  const paragraph = finished ? "这次观看已经结束" : "同一次观看只由一个页面控制";
  const heading = finished ? "观看已由另一个页面完成" : "这次观看已在另一个页面打开";
  const copy = finished ? "旧页面无法继续写入，记录保持原样。" : "原页面不会被这个页面静默打断。若原页面已经关闭，可以明确接管。";
  const claimButton = finished ? "" : `<button id="claimSession" class="primary-button" type="button">在这里继续</button>`;
  errorPanel.innerHTML = `
    <p class="eyebrow">${paragraph}</p>
    <h1>${heading}</h1>
    <p class="lead">${copy}</p>
    ${claimButton}
  `;
  if (finished) {
    errorPanel.querySelector("h1")?.focus();
    return;
  }
  document.querySelector("#claimSession")?.addEventListener("click", async () => {
    try {
      session = await api(`/api/sessions/${session.session_id}/claim`, {
        method: "POST",
        body: { client_id: CLIENT_ID },
      });
      profile = session.profile;
      if (session.stage === "probe") await renderProbe();
      else if (session.stage === "probe_feedback") renderProbeFeedback(false);
      else await prepareWatch();
    } catch (error) {
      showError(`没有接管这次观看：${error.message}`);
    }
  });
  focusSoon("#claimSession");
}

function ensureSessionVideoSource() {
  const desired = assetUrl(session?.video?.source);
  if (!desired || sourceVideo.getAttribute("src") === desired) return;
  sourceVideo.pause();
  sourceVideo.src = desired;
  sourceVideo.load();
}

async function prepareWatch() {
  if (session?.stage === "watch" && session.owner_client_id && session.owner_client_id !== CLIENT_ID) {
    renderOwnershipConflict();
    return false;
  }
  showOnly(watchPanel);
  try {
    ensureSessionVideoSource();
    await Promise.all([loadCaptions(), ensureVideoMetadata()]);
    if (session.open_interaction) {
      await api(`/api/sessions/${session.session_id}/interactions/${session.open_interaction.item_id}/complete`, {
        method: "POST",
        body: {
          ...ownerFields(),
          interaction_id: session.open_interaction.interaction_id,
          outcome: "technical_failure",
          dwell_ms: 0,
          phrase_confirmed: false,
          replays: 0,
          failure_reason: "reload_recovered",
        },
      });
      session = await api(`/api/sessions/${session.session_id}`);
      profile = session.profile;
    }
    handledIds = new Set(session.completed_ids || []);
    encounteredIds = new Set(session.encountered_ids || []);
    sessionSkipCount = Number(session.current_epoch_skipped || 0);
    progressSeq = Number(session.progress_seq || 0);
    previousTime = Number(session.last_media_time || 0);
    seekSourceAsSystem(previousTime);
    watchStatus.textContent = previousTime > 0 ? "从上次位置继续" : "正常观看";
    resumeButton.hidden = false;
    renderIntensity();
    brandState.textContent = session.video?.title || "正常观看";
    focusSoon("#resumeButton");
    return true;
  } catch (error) {
    showError(`已有观看记录暂时无法恢复：${error.message}`);
    return false;
  }
}

async function startOrResume() {
  startButton.disabled = true;
  try {
    const pack = selectedPack();
    if (!pack) throw new Error("请先选择一个视频");
    if (session && session.pack_id !== pack.pack_id) session = null;
    if (!session && pack.active_session_id) {
      session = await api(`/api/sessions/${pack.active_session_id}`);
    }
    if (!session) {
      session = await api("/api/sessions", {
        method: "POST",
        body: { qa: new URLSearchParams(location.search).get("qa") === "1", pack_id: pack.pack_id },
      });
    }
    localStorage.setItem(SESSION_KEY, session.session_id);
    if (session.stage === "probe_ready" || session.stage === "watch_ready") {
      session = await api(`/api/sessions/${session.session_id}/start`, {
        method: "POST",
        body: { client_id: CLIENT_ID },
      });
    }
    profile = session.profile;
    if (session.stage === "probe") {
      await renderProbe();
      return;
    }
    if (session.stage === "probe_feedback") {
      renderProbeFeedback(false);
      return;
    }
    if (!(await prepareWatch())) return;
    watchStartedPerf = performance.now();
    await playSourceAsSystem();
    resumeButton.hidden = true;
  } catch (error) {
    showError(`没有开始播放：${error.message}`);
  } finally {
    startButton.disabled = false;
  }
}

async function boot() {
  try {
    profile = await api("/api/profile");
    renderIntensity();
    const stored = localStorage.getItem(SESSION_KEY) || profile.active_session_id;
    if (stored) {
      try {
        session = await api(`/api/sessions/${stored}`);
        profile = session.profile;
        localStorage.setItem(SESSION_KEY, stored);
      } catch (error) {
        if (error.status === 404) localStorage.removeItem(SESSION_KEY);
        else throw error;
      }
    }
    await refreshPacks(session?.pack_id || null);
    if (session?.stage === "complete") {
      localStorage.removeItem(SESSION_KEY);
      session = null;
      renderHome();
    } else if (session?.stage === "probe") {
      await renderProbe();
    } else if (session?.stage === "probe_feedback") {
      renderProbeFeedback(false);
    } else if (session?.stage === "watch" && session.open_interaction) {
      await prepareWatch();
    } else {
      renderHome();
    }
  } catch (error) {
    showError(`本地服务暂时不可用：${error.message}`);
  }
}

importForm.addEventListener("submit", submitImport);
startButton.addEventListener("click", startOrResume);
libraryButton.addEventListener("click", async () => {
  if (!sourceVideo.paused) sourceVideo.pause();
  await saveProgress(true);
  await refreshPacks(session?.pack_id || null);
  renderHome();
});
probeSkip.addEventListener("click", () => submitProbe(probeAudioFailed ? "technical_failure" : "skipped"));
resumeButton.addEventListener("click", async () => {
  try {
    await playSourceAsSystem();
    resumeButton.hidden = true;
    watchStartedPerf ||= performance.now();
  } catch {
    resumeButton.hidden = false;
  }
});
skipLearning.addEventListener("click", () => finalizeLearning("skipped", "user_skip"));

document.addEventListener("keydown", (event) => {
  if (!activeInteraction) return;
  if (event.key === "Escape") {
    event.preventDefault();
    finalizeLearning("skipped", "escape");
    return;
  }
  const key = event.key.toLocaleLowerCase();
  const feedback = { "1": "known", "2": "familiar", "3": "unclear" }[key];
  const action = feedback ? document.querySelector(`[data-feedback="${feedback}"]`) : key === "r" ? document.querySelector("#mappingReplay") : null;
  if (action && !action.disabled) {
    event.preventDefault();
    action.click();
    return;
  }
  if (event.key === "Tab") {
    const focusable = [...learningLayer.querySelectorAll("button:not([disabled])")];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});

sourceVideo.addEventListener("timeupdate", () => {
  if (!activeInteraction) captionLine.textContent = captionAt(sourceVideo.currentTime)?.text || "";
  saveProgress();
});
sourceVideo.addEventListener("seeking", () => {
  if (expectedSystemSeek > 0) {
    expectedSystemSeek -= 1;
    return;
  }
  seeking = true;
  userGeneration += 1;
  manualSeeks += 1;
});
sourceVideo.addEventListener("seeked", () => {
  const current = sourceVideo.currentTime;
  session?.items?.filter((item) => Number(item.anchor_sec) <= current).forEach((item) => handledIds.add(item.id));
  previousTime = current;
  seeking = false;
  saveProgress(true);
});
sourceVideo.addEventListener("play", () => {
  resumeButton.hidden = true;
  if (expectedSystemPlay > 0) expectedSystemPlay -= 1;
  else userGeneration += 1;
  scheduleVideoMonitor();
});
sourceVideo.addEventListener("pause", () => {
  cancelVideoMonitor();
  const systemPause = expectedSystemPause > 0;
  if (systemPause) expectedSystemPause -= 1;
  if (!systemPause && !sourceVideo.ended && !activeInteraction) resumeButton.hidden = false;
  saveProgress(true);
});
sourceVideo.addEventListener("ended", () => {
  cancelVideoMonitor();
  finishSession();
});
sourceVideo.addEventListener("error", () => showError("视频没有正常加载。"));

document.addEventListener("visibilitychange", () => {
  if (document.hidden && session?.stage === "probe") {
    ++probePlaybackEpoch;
    stopHelper("page_hidden");
    renderProbeInterrupted();
    return;
  }
  if (document.hidden && !sourceVideo.paused) {
    pauseSourceAsSystem();
    saveProgress(true);
  }
  if (!document.hidden && sourceVideo.paused && session?.stage === "watch" && !activeInteraction) resumeButton.hidden = false;
});

window.addEventListener("beforeunload", () => {
  if (!session || session.stage !== "watch") return;
  navigator.sendBeacon(
    `/api/sessions/${session.session_id}/progress`,
    new Blob([JSON.stringify({ ...ownerFields(), sequence: progressSeq + 1, media_time: sourceVideo.currentTime })], { type: "application/json" }),
  );
});

boot();
