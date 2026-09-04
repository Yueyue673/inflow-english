(() => {
  if (globalThis.__inflowEnglishLoaded) return;
  globalThis.__inflowEnglishLoaded = true;

  const RANK = { low: 0, medium: 1, high: 2 };
  const STAGE_COPY = {
    queued: "等待开始",
    validating: "检查视频",
    downloading: "获取视频和字幕",
    extracting_audio: "提取原声",
    transcribing: "识别英语",
    segmenting: "寻找自然句",
    translating: "翻译和选词",
    selecting: "核对词义",
    clipping: "生成原声片段",
    auditing: "校验材料",
    windowing: "准备播放头附近内容",
    partial_ready: "当前窗口已可用",
  };
  const IMPORT_ERROR_COPY = {
    source_duration_out_of_range: "当前学习稳定支持 20 秒–15 分钟；字幕仍可继续显示。",
    long_video_english_captions_required: "长视频需要可靠的英文时间轴字幕；普通字幕仍可继续显示。",
    progressive_learning_not_enabled: "长视频学习仍在真实质量验收中；字幕继续正常显示。",
    transcript_primary_language_not_english: "这条视频的主要语音不是英语。",
    transcriber_memory_unavailable: "电脑当前内存较紧，语音识别没有启动。",
    argos_translation_unavailable: "本地翻译组件未就绪。",
    argos_en_zh_model_missing: "本地英译中模型未就绪。",
    argos_memory_unavailable: "电脑当前内存较紧，本地翻译没有启动。",
    argos_translation_failed: "本地翻译暂时失败。",
    argos_runtime_runtimeerror: "本地翻译暂时失败。",
    insufficient_unprompted_asr_verified_candidates: "没有找到足够可靠的原声学习片段。",
    heuristic_candidate_pool_empty: "没有找到值得打断观看的表达。",
  };

  let enabled = false;
  let preparing = false;
  let message = "";
  let videoId = currentVideoId();
  let session = null;
  let profile = null;
  let captions = [];
  let captionBundles = [];
  let audioCache = new Map();
  let handledIds = new Set();
  let encounteredIds = new Set();
  let activeInteraction = null;
  let previousTime = 0;
  let monitorHandle = null;
  let monitorMode = null;
  let progressTimer = null;
  let progressiveSyncTimer = null;
  let focusEpoch = 0;
  let lastProgressiveFocusSec = null;
  let progressSeq = 0;
  let userGeneration = 0;
  let expectedPause = 0;
  let expectedPlay = 0;
  let manualSeeks = 0;
  let technicalFailures = 0;
  let watchStartedAt = 0;
  let importJob = null;
  let routeGeneration = 0;
  let panelOpen = false;
  let autoMode = true;
  let autoLearning = false;
  let displaySize = "medium";
  let autoEnableTimer = null;
  let continuousPlaybackStartedAt = 0;
  let manualDisabledVideoId = null;
  let subtitleActive = false;
  let subtitleState = "idle";
  let subtitleTaskVideoId = null;
  let subtitleTask = null;
  let nativeCaptionsOwned = false;
  let nativeCaptionBridgeAttemptedFor = null;
  let adObserver = null;
  let observedPlayer = null;
  let lastAdActive = false;
  let transcriptCues = [];
  let lexiconEntries = [];
  let currentCaptionCue = null;
  let currentCaptionRange = null;
  let suppressLearningUntilMediaTime = null;
  let selectedWord = null;
  let videoBoundsObserver = null;
  let statusError = false;
  let backendAvailable = false;
  let learningRetryBlocked = false;
  let sessionTakeoverRequired = false;
  let keyboardActivatedControl = null;

  const host = document.createElement("div");
  host.id = "inflow-extension-root";
  host.style.cssText = "all:initial;position:fixed;inset:0;z-index:2147483646;pointer-events:none;";
  const shadow = host.attachShadow({ mode: "closed" });
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      *, *::before, *::after { box-sizing: border-box; }
      .ui { font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif; color: #f7f8fa; }
      button { font: inherit; }
      #pill { pointer-events:auto; position:fixed; z-index:30; right:calc(var(--video-right,20px) + 12px); top:calc(var(--video-top,0px) + 12px); min-width:118px; min-height:32px; padding:0 11px; overflow:hidden; border:1px solid rgba(255,255,255,.20); border-radius:8px; color:#f7f8fa; background:rgba(24,29,37,.90); box-shadow:0 4px 16px rgba(0,0,0,.30); cursor:pointer; opacity:.92; font-size:12px; font-weight:650; line-height:1; white-space:nowrap; backdrop-filter:blur(12px); transition:opacity .14s,background .14s,border-color .14s; }
      #pill:hover, #pill:focus-visible, #pill[data-expanded="true"] { color:#fff; background:rgba(15,17,21,.97); border-color:rgba(255,255,255,.34); opacity:1; outline:none; }
      #pill:focus-visible { box-shadow:0 0 0 2px rgba(159,193,255,.75),0 4px 16px rgba(0,0,0,.30); }
      #pill[data-state="ready"] { background:rgba(50,76,108,.94); }
      #pill[data-state="captions"] { background:rgba(44,63,86,.92); }
      #pill[data-state="working"] { background:rgba(105,77,35,.94); animation:pulse 1.1s ease-in-out infinite; }
      #pill[data-state="error"] { color:#fff; background:rgba(103,48,48,.96); opacity:1; }
      #panel, #wordPanel { pointer-events:auto; position:fixed; z-index:25; right:calc(var(--video-right,20px) + 12px); width:min(340px,calc(var(--video-width,100vw) - 24px)); padding:14px; border:1px solid rgba(255,255,255,.15); border-radius:12px; background:rgba(15,17,21,.96); box-shadow:0 16px 48px rgba(0,0,0,.42); backdrop-filter:blur(18px); }
      #panel { top:calc(var(--video-top,0px) + 52px); }
      #wordPanel { bottom:calc(var(--video-bottom,22px) + 52px); }
      #panel[hidden], #wordPanel[hidden], #caption[hidden], #overlay[hidden] { display:none !important; }
      #panel strong, #wordPanel strong { display:block; font-size:14px; margin-bottom:6px; }
      #panel p, #wordPanel p { margin:0; color:#b9c1cd; font-size:12px; line-height:1.5; }
      .panel-actions, .word-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
      .panel-actions button, .word-actions button { min-height:34px; padding:0 11px; border-radius:8px; border:1px solid #3b4350; color:#e7ebf2; background:#20252d; cursor:pointer; }
      #caption { pointer-events:auto; position:fixed; z-index:20; left:var(--video-center-x,50vw); top:calc(var(--video-top,0px) + var(--video-height,100vh) - 68px); transform:translate(-50%,-100%); width:max-content; max-width:min(760px,calc(var(--video-width,100vw) - 64px)); padding:10px 34px 11px 16px; border:1px solid rgba(255,255,255,.10); border-radius:9px; color:#fff; background:rgba(5,7,10,.82); box-shadow:0 8px 24px rgba(0,0,0,.28); line-height:1.45; text-align:center; text-shadow:0 1px 2px rgba(0,0,0,.9); backdrop-filter:blur(8px); overflow-wrap:anywhere; }
      #captionEnglish { color:rgba(245,247,250,.78); font-size:var(--caption-en-size,18px); font-weight:540; line-height:1.42; text-wrap:pretty; transition:color .12s ease; }
      #captionChinese { margin-top:4px; color:#fff; font-size:var(--caption-zh-size,14px); font-weight:650; line-height:1.48; text-wrap:pretty; }
      #caption[data-languages="en"] #captionEnglish, #caption:hover #captionEnglish, #caption:focus-within #captionEnglish { color:#fff; }
      #captionReplay { position:absolute; top:7px; right:7px; min-width:26px; height:26px; padding:0 5px; border:0; border-radius:6px; color:#dbe2ec; background:rgba(255,255,255,.09); cursor:pointer; opacity:0; pointer-events:none; font-size:11px; transition:opacity .12s ease,background .12s ease; }
      #caption:hover #captionReplay, #caption:focus-within #captionReplay { opacity:1; pointer-events:auto; }
      #captionReplay:hover, #captionReplay:focus-visible { background:rgba(255,255,255,.18); outline:2px solid rgba(180,207,248,.75); }
      .token { display:inline; padding:0 1px; border:0; border-bottom:2px solid transparent; color:inherit; background:transparent; cursor:pointer; text-shadow:inherit; }
      .token:hover, .token:focus-visible { color:#fff; border-color:#fff; outline:none; }
      .token[data-status="known"] { color:inherit; border-color:rgba(120,178,133,.48); }
      .token[data-status="familiar"] { color:inherit; border-color:rgba(224,174,78,.72); }
      .token[data-status="unclear"] { color:inherit; border-color:rgba(142,184,255,.88); }
      #overlay { pointer-events:auto; position:fixed; z-index:40; left:var(--video-left,0px); top:var(--video-top,0px); width:var(--video-width,100vw); height:var(--video-height,100vh); display:grid; place-items:center; padding:24px; background:rgba(5,7,10,.80); opacity:1; transition:opacity 140ms ease; }
      #overlay.concealing { opacity:0; }
      .card { width:min(680px,calc(var(--video-width,100vw) - 40px)); max-height:calc(var(--video-height,100vh) - 36px); overflow:auto; padding:var(--card-padding,28px); border:1px solid rgba(255,255,255,.14); border-radius:16px; background:#11151b; box-shadow:0 30px 90px rgba(0,0,0,.55); }
      #phase { margin:0 0 8px; color:#9fc1ff; font-size:12px; }
      #title { margin:0; font-size:var(--title-size,25px); line-height:1.25; }
      #content { margin-top:18px; }
      .listening { display:flex; align-items:center; gap:10px; min-height:72px; color:#d7dce5; font-size:var(--phrase-size,17px); }
      .dot { width:9px; height:9px; border-radius:50%; background:#9fc1ff; animation:pulse 1.1s ease-in-out infinite; }
      .word { margin:0 0 4px; font-size:var(--word-size,34px); line-height:1.15; letter-spacing:-.02em; }
      .gloss { margin:0 0 22px; color:#c9dcff; font-size:var(--gloss-size,22px); }
      .label { margin:0 0 6px; color:#8993a2; font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
      .phrase, .translation { margin:0; font-size:var(--phrase-size,17px); line-height:1.55; }
      .translation { margin-top:7px; color:#b7c0cd; }
      mark { color:inherit; background:transparent; border-bottom:2px solid #7289aa; }
      mark.active { color:#fff; border-color:#9fc1ff; text-shadow:0 0 16px rgba(159,193,255,.65); }
      #audioState { min-height:18px; margin:12px 0 0; color:#e0ae78; font-size:12px; }
      #actions { display:flex; flex-wrap:wrap; gap:9px; margin-top:22px; }
      #actions button { min-height:42px; padding:0 14px; border-radius:9px; cursor:pointer; }
      .primary { border:1px solid #c9dcff; color:#0b1018; background:#c9dcff; font-weight:700; }
      .secondary { border:1px solid #3a4350; color:#edf0f5; background:#20262e; }
      .quiet { border:0; color:#aeb7c5; background:transparent; }
      button:disabled { opacity:.45; cursor:default; }
      kbd { margin-left:5px; padding:1px 5px; border:1px solid #4a5360; border-radius:4px; color:#aeb7c5; font-size:10px; }
      @keyframes pulse { 0%,100%{opacity:.35} 50%{opacity:1} }
      @media (max-width:600px) { .card{--card-padding:18px}#caption{top:calc(var(--video-top,0px) + var(--video-height,100vh) - 58px);max-width:calc(var(--video-width,100vw) - 28px);padding:8px 30px 9px 12px} }
      @media (prefers-reduced-motion:reduce) { #pill,#overlay,#captionEnglish,#captionReplay { transition:none !important; animation:none !important; } }
    </style>
    <div class="ui">
      <button id="pill" type="button">启用 InFlow</button>
      <section id="panel" hidden>
        <strong id="panelTitle">InFlow English</strong>
        <p id="panelMessage"></p>
        <div class="panel-actions"><button id="panelPrimary" type="button">启用当前视频</button><button id="panelClose" type="button">收起</button></div>
      </section>
      <section id="wordPanel" hidden aria-live="polite">
        <strong id="wordSurface"></strong>
        <p id="wordGloss">正在读取当前义项…</p>
        <div class="word-actions">
          <button data-word-feedback="known" type="button">听到就懂</button>
          <button data-word-feedback="familiar" type="button">有点熟</button>
          <button data-word-feedback="unclear" type="button">还不清楚</button>
          <button id="wordClose" type="button">收起</button>
        </div>
      </section>
      <div id="caption" hidden><div id="captionEnglish"></div><div id="captionChinese"></div><button id="captionReplay" type="button" aria-label="重听当前字幕" title="重听当前字幕（S）">↺ <kbd>S</kbd></button></div>
      <div id="overlay" hidden>
        <section class="card" role="dialog" aria-modal="true" aria-labelledby="title">
          <p id="phase"></p>
          <h2 id="title" tabindex="-1"></h2>
          <div id="content"></div>
          <div id="actions"></div>
        </section>
      </div>
    </div>
  `;
  const mountHost = () => {
    if (host.isConnected || !document.documentElement) return host.isConnected;
    document.documentElement.appendChild(host);
    return true;
  };
  if (!mountHost()) {
    const rootObserver = new MutationObserver(() => {
      if (mountHost()) rootObserver.disconnect();
    });
    rootObserver.observe(document, { childList: true });
  }

  const pill = shadow.querySelector("#pill");
  const panel = shadow.querySelector("#panel");
  const panelMessage = shadow.querySelector("#panelMessage");
  const panelPrimary = shadow.querySelector("#panelPrimary");
  const panelClose = shadow.querySelector("#panelClose");
  const wordPanel = shadow.querySelector("#wordPanel");
  const wordSurface = shadow.querySelector("#wordSurface");
  const wordGloss = shadow.querySelector("#wordGloss");
  const wordClose = shadow.querySelector("#wordClose");
  const caption = shadow.querySelector("#caption");
  const captionEnglish = shadow.querySelector("#captionEnglish");
  const captionChinese = shadow.querySelector("#captionChinese");
  const captionReplay = shadow.querySelector("#captionReplay");
  const overlay = shadow.querySelector("#overlay");
  const phase = shadow.querySelector("#phase");
  const title = shadow.querySelector("#title");
  const content = shadow.querySelector("#content");
  const actions = shadow.querySelector("#actions");

  function callWorker(payload) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(payload, (response) => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
        if (!response?.ok) return reject(new Error(response?.error || "本机服务没有响应"));
        resolve(response.data);
      });
    });
  }

  function currentVideoId() {
    try {
      const url = new URL(location.href);
      if (url.hostname !== "www.youtube.com" || url.pathname !== "/watch") return null;
      const id = url.searchParams.get("v") || "";
      return /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
    } catch { return null; }
  }

  function canonicalUrl(id = videoId) {
    return id ? `https://www.youtube.com/watch?v=${id}` : null;
  }

  function sourceVideo() {
    return document.querySelector("video.html5-main-video") || document.querySelector("video");
  }

  function applyDisplaySize() {
    const sizes = {
      small: { en: 16, zh: 17, title: 21, word: 29, gloss: 18, phrase: 15, padding: 22 },
      medium: { en: 20, zh: 21, title: 25, word: 36, gloss: 22, phrase: 17, padding: 28 },
      large: { en: 25, zh: 26, title: 30, word: 44, gloss: 27, phrase: 21, padding: 34 },
    };
    const value = sizes[displaySize] || sizes.medium;
    host.style.setProperty("--caption-en-size", `${value.en}px`);
    host.style.setProperty("--caption-zh-size", `${value.zh}px`);
    host.style.setProperty("--title-size", `${value.title}px`);
    host.style.setProperty("--word-size", `${value.word}px`);
    host.style.setProperty("--gloss-size", `${value.gloss}px`);
    host.style.setProperty("--phrase-size", `${value.phrase}px`);
    host.style.setProperty("--card-padding", `${value.padding}px`);
  }

  function syncVideoBounds() {
    const video = sourceVideo();
    if (!video) return;
    const rect = video.getBoundingClientRect();
    const left = Math.max(0, rect.left);
    const top = Math.max(0, rect.top);
    const right = Math.min(innerWidth, rect.right);
    const bottom = Math.min(innerHeight, rect.bottom);
    const width = Math.max(0, right - left);
    const height = Math.max(0, bottom - top);
    if (width < 160 || height < 90) return;
    host.style.setProperty("--video-left", `${left}px`);
    host.style.setProperty("--video-center-x", `${left + width / 2}px`);
    host.style.setProperty("--video-top", `${top}px`);
    host.style.setProperty("--video-width", `${width}px`);
    host.style.setProperty("--video-height", `${height}px`);
    host.style.setProperty("--video-right", `${Math.max(0, innerWidth - right)}px`);
    host.style.setProperty("--video-bottom", `${Math.max(0, innerHeight - bottom)}px`);
  }

  function observeVideoBounds(video) {
    videoBoundsObserver?.disconnect();
    videoBoundsObserver = new ResizeObserver(syncVideoBounds);
    videoBoundsObserver.observe(video);
    syncVideoBounds();
  }

  function inAd() {
    return Boolean(document.querySelector("#movie_player.ad-showing"));
  }

  function handleAdState(force = false) {
    const active = inAd();
    if (!force && active === lastAdActive) return;
    lastAdActive = active;
    continuousPlaybackStartedAt = 0;
    cancelAutoEnable();
    if (active) {
      caption.hidden = true;
      if (subtitleActive) subtitleState = "ready_waiting_ad";
      if (activeInteraction) finalizeInteraction("technical_failure", "ad_started");
    } else if (subtitleActive) {
      subtitleState = "ready";
      const video = sourceVideo();
      if (video) renderCaption(video.currentTime);
      setMessage(readyMessage());
    }
    renderStatus();
    manageAutoEnable();
  }

  function observeAdState() {
    const player = document.querySelector("#movie_player");
    if (player === observedPlayer) return;
    adObserver?.disconnect();
    observedPlayer = player;
    if (!player) return;
    adObserver = new MutationObserver(() => handleAdState());
    adObserver.observe(player, { attributes: true, attributeFilter: ["class"] });
    handleAdState(true);
  }

  function publicState() {
    return {
      supported: Boolean(videoId),
      videoId,
      enabled,
      preparing,
      subtitleActive,
      subtitleState,
      autoMode,
      autoLearning,
      displaySize,
      backendAvailable,
      learningRetryBlocked,
      sessionTakeoverRequired,
      message: message || (videoId ? waitingMessage() : "当前不是普通 YouTube 视频。"),
      candidatePool: Number(session?.candidate_pool_count || 0),
      interventionBudgets: session?.intervention_budgets || null,
    };
  }

  function renderStatus() {
    host.hidden = !videoId;
    if (!videoId) return;
    syncVideoBounds();
    if (statusError) {
      pill.dataset.state = "error";
      pill.textContent = "InFlow 需要查看";
    } else if (preparing) {
      pill.dataset.state = "working";
      pill.textContent = subtitleActive ? "InFlow 学习准备中" : "InFlow 字幕准备中";
    } else if (subtitleState === "waiting_ad") {
      pill.dataset.state = "idle";
      pill.textContent = "InFlow 等正片";
    } else if (subtitleState === "ready_waiting_ad") {
      pill.dataset.state = "captions";
      pill.textContent = "InFlow 已备好 · 等正片";
    } else if (subtitleState === "degraded") {
      pill.dataset.state = "error";
      pill.textContent = "InFlow 字幕未就绪";
    } else if (subtitleState === "loading") {
      pill.dataset.state = "working";
      pill.textContent = "InFlow 字幕准备中";
    } else if (enabled) {
      pill.dataset.state = "ready";
      pill.textContent = "InFlow 正在工作";
    } else if (subtitleActive) {
      pill.dataset.state = "captions";
      pill.textContent = "InFlow 字幕已就绪";
    } else {
      pill.dataset.state = "idle";
      const currentVideoPaused = manualDisabledVideoId === videoId;
      pill.textContent = autoMode && !currentVideoPaused ? "InFlow 自动待命" : "InFlow 已暂停";
    }
    pill.dataset.expanded = panelOpen || statusError ? "true" : "false";
    pill.setAttribute("aria-label", pill.textContent);
    panel.hidden = !panelOpen;
    panelMessage.textContent = message || (subtitleActive ? readyMessage() : waitingMessage());
    panelPrimary.textContent = sessionTakeoverRequired
      ? "接管学习"
      : learningRetryBlocked
        ? "重试学习"
        : !subtitleActive && ["failed", "degraded"].includes(subtitleState)
          ? "重试字幕"
          : (enabled || preparing || subtitleActive ? "暂停本视频" : "现在启用");
  }

  function setMessage(value, isError = false) {
    message = String(value || "");
    statusError = Boolean(isError);
    renderStatus();
  }

  function trustedActivation(event, control) {
    return Boolean(event?.isTrusted || keyboardActivatedControl === control);
  }

  function safeErrorCode(error) {
    const raw = String(error?.message || error || "unknown_error").trim();
    const match = raw.match(/^[A-Za-z0-9_:-]{1,180}$/);
    return match ? match[0] : "unexpected_error";
  }

  function waitingMessage() {
    if (!autoMode) return "自动字幕已关闭。";
    return autoLearning
      ? "正在优先准备字幕；连续观看 8 秒后再准备学习。"
      : "正在准备中英字幕；自动教学暂停未开启。";
  }

  function readyMessage() {
    if (!backendAvailable) return "字幕已就绪；本机学习服务未连接，当前句重听仍可使用。 ";
    return autoLearning
      ? "字幕已就绪；连续观看 8 秒后再准备学习。 "
      : "字幕已就绪；自动教学暂停未开启。";
  }

  function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

  function withTimeout(promise, ms, code) {
    let timer = null;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(code)), Math.max(1, Number(ms) || 1));
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }

  function cancelAutoEnable() {
    clearTimeout(autoEnableTimer);
    autoEnableTimer = null;
  }

  function nativeSubtitleButton() {
    return document.querySelector("button.ytp-subtitles-button");
  }

  function enableNativeCaptionBridge() {
    if (!autoMode || manualDisabledVideoId === videoId || !videoId || subtitleActive || nativeCaptionBridgeAttemptedFor === videoId) return;
    const button = nativeSubtitleButton();
    if (!button || button.disabled || button.getAttribute("aria-disabled") === "true") return;
    nativeCaptionBridgeAttemptedFor = videoId;
    if (button.getAttribute("aria-pressed") === "true") return;
    nativeCaptionsOwned = true;
    button.click();
    setTimeout(() => {
      if (nativeCaptionsOwned && nativeSubtitleButton()?.getAttribute("aria-pressed") !== "true") nativeCaptionsOwned = false;
    }, 250);
  }

  function releaseNativeCaptionBridge() {
    if (!nativeCaptionsOwned) return;
    const button = nativeSubtitleButton();
    if (button?.getAttribute("aria-pressed") === "true") button.click();
    nativeCaptionsOwned = false;
  }

  function parsePageCaptionTrack(payload, durationSec, requireCjk, idPrefix) {
    const duration = Number(durationSec);
    if (!payload || !Array.isArray(payload.events) || !Number.isFinite(duration) || duration <= 0) return [];
    const rows = payload.events.map((event) => {
      if (!event || !Array.isArray(event.segs)) return null;
      const text = event.segs.map((segment) => String(segment?.utf8 || "")).join("")
        .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, " ").replace(/\s+/g, " ").trim();
      if (!text || (requireCjk ? !/[\u3400-\u9fff]/.test(text) : !/[A-Za-z]/.test(text))) return null;
      const start = Number(event.tStartMs) / 1000;
      const itemDuration = event.dDurationMs == null ? null : Number(event.dDurationMs) / 1000;
      if (!Number.isFinite(start) || start < 0 || (itemDuration != null && (!Number.isFinite(itemDuration) || itemDuration <= 0))) return null;
      return { start, itemDuration, text };
    }).filter(Boolean).sort((left, right) => left.start - right.start);
    return rows.map((row, index) => {
      const nextStart = index + 1 < rows.length ? rows[index + 1].start : duration;
      const end = Math.min(duration, Math.max(row.start + 0.05, row.itemDuration == null ? nextStart : row.start + row.itemDuration));
      if (row.start >= duration) return null;
      return { id: `${idPrefix}-${String(index + 1).padStart(5, "0")}`, start: row.start, end, text: row.text };
    }).filter(Boolean);
  }

  function pageCaptionOverlap(start, end, row) {
    const overlap = Math.min(end, Number(row.end)) - Math.max(start, Number(row.start));
    if (overlap <= 0) return false;
    const sourceDuration = Math.max(0.001, end - start);
    const rowDuration = Math.max(0.001, Number(row.end) - Number(row.start));
    return overlap >= 0.35 || overlap / Math.min(sourceDuration, rowDuration) >= 0.35;
  }

  function mergePageCaptionTracks(english, chinese) {
    const primary = english.length ? english : chinese;
    return primary.map((row, index) => {
      const start = Number(row.start);
      const end = Number(row.end);
      const zh = english.length
        ? mergeCaptionFragments(chinese.filter((candidate) => pageCaptionOverlap(start, end, candidate)))
        : String(row.text || "");
      return {
        id: `page-caption-${String(index + 1).padStart(5, "0")}`,
        start,
        end,
        text_en: english.length ? String(row.text || "") : "",
        text_zh: zh,
      };
    }).filter((row) => row.text_en || row.text_zh);
  }

  function requestPageCaptionBridge(targetVideoId) {
    const nonce = typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : [...crypto.getRandomValues(new Uint8Array(16))].map((value) => value.toString(16).padStart(2, "0")).join("");
    return new Promise((resolve, reject) => {
      let settled = false;
      const script = document.createElement("script");
      const cleanup = () => {
        clearTimeout(timer);
        window.removeEventListener("message", onMessage);
        script.remove();
      };
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        callback(value);
      };
      const onMessage = (event) => {
        const data = event.data;
        if (event.source !== window || event.origin !== location.origin || !data || data.source !== "inflow-page-caption-bridge" || data.nonce !== nonce) return;
        if (!data.ok) {
          finish(reject, new Error(safeErrorCode(data.error)));
          return;
        }
        if (data.video_id !== targetVideoId) {
          finish(reject, new Error("youtube_page_bridge_video_mismatch"));
          return;
        }
        finish(resolve, data);
      };
      const timer = setTimeout(() => finish(reject, new Error("youtube_page_bridge_timeout")), 3500);
      script.src = chrome.runtime.getURL("page-bridge.js");
      script.dataset.inflowNonce = nonce;
      script.dataset.inflowVideoId = targetVideoId;
      script.addEventListener("error", () => finish(reject, new Error("youtube_page_bridge_load_failed")), { once: true });
      window.addEventListener("message", onMessage);
      document.documentElement.appendChild(script);
    });
  }

  async function loadPageCaptionPreview(targetVideoId, generation) {
    const startedDuringAd = inAd();
    subtitleState = startedDuringAd ? "waiting_ad" : "loading";
    renderStatus();
    const deadline = performance.now() + 4000;
    let lastError = null;
    while (generation === routeGeneration && targetVideoId === currentVideoId() && performance.now() < deadline) {
      if (subtitleActive) return true;
      const video = sourceVideo();
      if (!video) {
        await delay(250);
        continue;
      }
      try {
        const remaining = Math.max(100, deadline - performance.now());
        const payload = await withTimeout(
          requestPageCaptionBridge(targetVideoId),
          remaining,
          "youtube_page_caption_bridge_rpc_timeout",
        );
        if (generation !== routeGeneration || targetVideoId !== currentVideoId()) return false;
        if (subtitleActive) return true;
        let english;
        let chinese;
        try {
          english = parsePageCaptionTrack(payload.english, payload.duration_sec, false, "page-en");
          chinese = parsePageCaptionTrack(payload.chinese, payload.duration_sec, true, "page-zh");
        } catch {
          throw new Error("youtube_page_caption_parse_failed");
        }
        let segments;
        try {
          segments = mergePageCaptionTracks(english, chinese);
        } catch {
          throw new Error("youtube_page_caption_merge_failed");
        }
        if (!segments.length) throw new Error("youtube_page_captions_empty");
        try {
          applySubtitlePreview({ segments }, inAd() ? "ready_waiting_ad" : (chinese.length ? "ready" : "english_ready"));
        } catch {
          throw new Error("youtube_page_caption_apply_failed");
        }
        setMessage(inAd()
          ? "字幕已备好；正片开始后显示。"
          : (chinese.length ? "字幕已从当前 YouTube 页面载入。" : "英文字幕已载入；中文字幕暂不可用。 "));
        return true;
      } catch (error) {
        lastError = error;
        await delay(250);
      }
    }
    throw lastError || new Error("youtube_player_not_ready");
  }

  function applySubtitlePreview(document, state = "ready") {
    const rows = Array.isArray(document?.segments) ? document.segments : [];
    captionBundles = rows.map((row) => ({
      id: row.id,
      start: Number(row.start),
      end: Number(row.end),
      text_en: String(row.text_en || ""),
      text_zh: String(row.text_zh || ""),
    }));
    captions = rows
      .filter((row) => row?.text_zh)
      .map((row) => ({ id: row.id, start: Number(row.start), end: Number(row.end), text: String(row.text_zh) }));
    transcriptCues = rows
      .filter((row) => row?.text_en)
      .map((row) => ({ cue_id: row.id, start_sec: Number(row.start), end_sec: Number(row.end), text: String(row.text_en) }));
    subtitleActive = captions.length > 0 || transcriptCues.length > 0;
    subtitleState = subtitleActive ? state : "failed";
    if (subtitleActive) releaseNativeCaptionBridge();
    const video = sourceVideo();
    if (video && subtitleActive) {
      applyDisplaySize();
      observeVideoBounds(video);
      attachVideo(video);
      if (inAd()) caption.hidden = true;
      else renderCaption(video.currentTime);
      startMonitor();
    }
    renderStatus();
  }

  async function loadCachedPackSubtitles(pack, generation) {
    const [captionPayload, transcriptPayload, lexicalPayload] = await Promise.all([
      callWorker({ type: "captions", path: pack.captions }),
      pack.transcript ? callWorker({ type: "transcript", path: pack.transcript }) : Promise.resolve({ cues: [] }),
      callWorker({ type: "lexicon" }),
    ]);
    if (generation !== routeGeneration) return;
    captionBundles = [];
    captions = (Array.isArray(captionPayload) ? captionPayload : captionPayload.segments || []).map((row) => ({
      id: row.id,
      start: Number(row.start),
      end: Number(row.end),
      text: String(row.text || ""),
    }));
    transcriptCues = Array.isArray(transcriptPayload?.cues) ? transcriptPayload.cues : [];
    lexiconEntries = Array.isArray(lexicalPayload?.entries) ? lexicalPayload.entries : [];
    subtitleActive = captions.length > 0 || transcriptCues.length > 0;
    subtitleState = subtitleActive ? "ready" : "failed";
    if (subtitleActive) releaseNativeCaptionBridge();
    const video = sourceVideo();
    if (video && subtitleActive) {
      applyDisplaySize();
      observeVideoBounds(video);
      attachVideo(video);
      if (inAd()) caption.hidden = true;
      else renderCaption(video.currentTime);
      startMonitor();
    }
    renderStatus();
  }

  function ensureSubtitleFirst(force = false) {
    videoId = currentVideoId();
    if (((!autoMode || manualDisabledVideoId === videoId) && !force) || !videoId) return Promise.resolve();
    if (subtitleTaskVideoId === videoId && subtitleTask) return subtitleTask;
    if (subtitleTaskVideoId === videoId && (subtitleActive || ["failed", "degraded"].includes(subtitleState))) return Promise.resolve();
    const targetVideoId = videoId;
    const generation = routeGeneration;
    subtitleTaskVideoId = targetVideoId;
    subtitleState = "loading";
    renderStatus();
    subtitleTask = (async () => {
      try {
        let bootstrap = null;
        const bootstrapRequest = callWorker({ type: "bootstrap" });
        try {
          bootstrap = await withTimeout(bootstrapRequest, 250, "backend_bootstrap_timeout");
          backendAvailable = true;
          profile = bootstrap.profile;
        } catch {
          backendAvailable = false;
          profile = null;
          bootstrapRequest.then(async (lateBootstrap) => {
            if (generation !== routeGeneration || targetVideoId !== currentVideoId()) return;
            backendAvailable = true;
            profile = lateBootstrap.profile;
            const latePack = findPack(lateBootstrap.packs?.packs);
            if (!subtitleActive && latePack?.captions) {
              try { await loadCachedPackSubtitles(latePack, generation); }
              catch {}
            }
            if (generation === routeGeneration && targetVideoId === currentVideoId()) {
              setMessage(subtitleActive ? readyMessage() : waitingMessage());
              manageAutoEnable();
            }
          }).catch(() => {});
        }
        if (generation !== routeGeneration || targetVideoId !== currentVideoId()) return;
        const pack = findPack(bootstrap?.packs?.packs);
        if (pack?.captions) {
          await loadCachedPackSubtitles(pack, generation);
          return;
        }
        try {
          if (await loadPageCaptionPreview(targetVideoId, generation)) {
            if (backendAvailable) {
              callWorker({ type: "lexicon" }).then((payload) => {
                if (generation !== routeGeneration || targetVideoId !== currentVideoId()) return;
                lexiconEntries = Array.isArray(payload?.entries) ? payload.entries : [];
                if (currentCaptionCue) renderEnglishCue(currentCaptionCue);
              }).catch(() => {});
            }
            setMessage(readyMessage());
            return;
          }
        } catch (error) {
          if (generation !== routeGeneration || targetVideoId !== currentVideoId()) return;
          if (subtitleActive) return;
          subtitleState = "failed";
          setMessage(`页面字幕失败：${safeErrorCode(error)}。原视频和 YouTube 字幕继续播放。`, true);
          return;
        }
      } catch {
        if (generation === routeGeneration && targetVideoId === currentVideoId()) {
          subtitleState = subtitleActive ? "degraded" : "failed";
          if (!subtitleActive) setMessage("InFlow 没有取得这条视频的字幕；原视频继续播放。", true);
        }
      } finally {
        if (generation === routeGeneration) {
          if (targetVideoId === subtitleTaskVideoId) subtitleTask = null;
          renderStatus();
          manageAutoEnable();
        }
      }
    })();
    return subtitleTask;
  }

  function manageAutoEnable() {
    videoId = currentVideoId();
    observeAdState();
    if (subtitleState === "ready_waiting_ad" && !inAd()) {
      subtitleState = "ready";
      setMessage(readyMessage());
    }
    if (autoMode && videoId) {
      enableNativeCaptionBridge();
      ensureSubtitleFirst();
    }
    const video = sourceVideo();
    const playingContinuously = Boolean(videoId && video && !video.paused && !video.ended && !document.hidden && !inAd());
    if (!playingContinuously) {
      continuousPlaybackStartedAt = 0;
      cancelAutoEnable();
      return;
    }
    if (!continuousPlaybackStartedAt) continuousPlaybackStartedAt = performance.now();
    const eligible = Boolean(
      autoMode
      && autoLearning
      && backendAvailable
      && !learningRetryBlocked
      && !sessionTakeoverRequired
      && manualDisabledVideoId !== videoId
      && !enabled
      && !preparing
      && !activeInteraction
      && subtitleActive
    );
    if (!eligible) {
      cancelAutoEnable();
      return;
    }
    const remaining = Math.max(0, 8000 - (performance.now() - continuousPlaybackStartedAt));
    if (remaining <= 0) {
      cancelAutoEnable();
      activate({ quiet: true });
      return;
    }
    if (autoEnableTimer !== null) return;
    autoEnableTimer = setTimeout(() => {
      autoEnableTimer = null;
      manageAutoEnable();
    }, Math.min(1000, Math.max(100, remaining)));
  }

  function findPack(rows) {
    const expected = canonicalUrl();
    return (rows || []).find((row) => row.source_url === expected || String(row.pack_id || "").startsWith(`yt-${videoId}-`)) || null;
  }

  async function preparePack(generation, forceRetry = false) {
    let packsPayload = await callWorker({ type: "listPacks" });
    let pack = findPack(packsPayload.packs);
    if (pack) return pack;
    preparing = true;
    renderStatus();
    importJob = await callWorker({ type: "prepare", url: canonicalUrl(), playhead_sec: Math.max(0, Number(sourceVideo()?.currentTime || 0)), force_retry: forceRetry });
    const terminalStatuses = ["ready", "complete", "degraded", "failed", "cancelled"];
    while (generation === routeGeneration && importJob && !importJob.usable && !terminalStatuses.includes(importJob.status)) {
      const stage = STAGE_COPY[importJob.stage] || "正在准备";
      setMessage(`${stage} · ${Math.round((Number(importJob.fraction) || 0) * 100)}%`);
      await delay(1200);
      importJob = await callWorker({ type: "importStatus", job_id: importJob.job_id });
    }
    preparing = false;
    if (generation !== routeGeneration) throw new Error("页面已经切换到另一条视频");
    if (!importJob?.usable) {
      if (importJob?.status === "cancelled") throw new Error("准备已取消");
      learningRetryBlocked = true;
      throw new Error(IMPORT_ERROR_COPY[importJob?.error_code] || "这条视频的学习内容没有准备好；不会自动重试，字幕和原视频仍可继续。 ");
    }
    learningRetryBlocked = false;
    if (importJob.route === "progressive") {
      return {
        pack_id: importJob.pack_id,
        source_url: canonicalUrl(),
        progressive: true,
        progressive_revision: Number(importJob.revision || 0),
      };
    }
    packsPayload = await callWorker({ type: "listPacks" });
    pack = findPack(packsPayload.packs);
    if (!pack) throw new Error("准备完成，但视频包没有出现在本机库中");
    return pack;
  }

  async function preloadSessionAssets(generation) {
    if (session.progressive) {
      const [lexiconPayload, audioEntries] = await Promise.all([
        callWorker({ type: "lexicon" }),
        Promise.all(session.items.map(async (item) => {
          const payload = await callWorker({ type: "audio", path: item.phrase_audio });
          return [item.id, payload.data_url];
        })),
      ]);
      if (generation !== routeGeneration) throw new Error("页面已经切换");
      lexiconEntries = Array.isArray(lexiconPayload?.entries) ? lexiconPayload.entries : [];
      audioCache = new Map(audioEntries);
      return;
    }
    const captionPromise = callWorker({ type: "captions", path: session.video.captions });
    const transcriptPromise = session.video.transcript
      ? callWorker({ type: "transcript", path: session.video.transcript })
      : Promise.resolve({ cues: [] });
    const lexiconPromise = callWorker({ type: "lexicon" });
    const audioEntries = await Promise.all(session.items.map(async (item) => {
      const payload = await callWorker({ type: "audio", path: item.phrase_audio });
      return [item.id, payload.data_url];
    }));
    if (generation !== routeGeneration) throw new Error("页面已经切换");
    const [captionPayload, transcriptPayload, lexiconPayload] = await Promise.all([captionPromise, transcriptPromise, lexiconPromise]);
    captionBundles = [];
    captions = Array.isArray(captionPayload) ? captionPayload : captionPayload.segments || [];
    transcriptCues = Array.isArray(transcriptPayload?.cues) ? transcriptPayload.cues : [];
    subtitleActive = captions.length > 0 || transcriptCues.length > 0;
    subtitleState = subtitleActive ? "ready" : subtitleState;
    lexiconEntries = Array.isArray(lexiconPayload?.entries) ? lexiconPayload.entries : [];
    audioCache = new Map(audioEntries);
  }

  async function syncProgressiveAssets() {
    if (!enabled || !session?.progressive || activeInteraction) return;
    const video = sourceVideo();
    if (!video) return;
    const generation = routeGeneration;
    const currentPlayhead = Math.max(0, Number(video.currentTime || 0));
    if (lastProgressiveFocusSec === null || Math.abs(currentPlayhead - lastProgressiveFocusSec) >= 120) {
      focusProgressiveAt(currentPlayhead);
      return;
    }
    const knownIds = new Set(session.items.map((item) => item.id));
    try {
      const updated = await callWorker({
        type: "syncProgressive",
        session_id: session.session_id,
        owner_epoch: session.owner_epoch,
        playhead_sec: currentPlayhead,
      });
      if (generation !== routeGeneration || !enabled) return;
      const additions = updated.items.filter((item) => !knownIds.has(item.id));
      const loaded = await Promise.all(additions.map(async (item) => {
        const payload = await callWorker({ type: "audio", path: item.phrase_audio });
        return [item.id, payload.data_url];
      }));
      if (generation !== routeGeneration || !enabled) return;
      loaded.forEach(([id, source]) => audioCache.set(id, source));
      session = updated;
      profile = updated.profile;
    } catch (error) {
      if (/stale_owner_epoch|session_owned_by_another_client/.test(String(error?.message || error))) {
        clearInterval(progressiveSyncTimer);
        progressiveSyncTimer = null;
        enabled = false;
        session = null;
        setMessage("另一处页面已接管学习；本页继续保留字幕。 ");
      }
    }
  }

  function focusProgressiveAt(playheadSec) {
    if (!session?.progressive) return;
    const boundedPlayhead = Math.max(0, Number(playheadSec || 0));
    lastProgressiveFocusSec = boundedPlayhead;
    focusEpoch += 1;
    const generation = routeGeneration;
    callWorker({
      type: "progressiveFocus",
      pack_id: session.pack_id,
      playhead_sec: boundedPlayhead,
      focus_epoch: focusEpoch,
    }).then(() => {
      if (generation === routeGeneration) syncProgressiveAssets();
    }).catch(() => {});
  }

  async function activate({ quiet = false, forceRetry = false, forceClaim = false } = {}) {
    if (enabled || preparing) return publicState();
    if (!quiet) manualDisabledVideoId = null;
    cancelAutoEnable();
    videoId = currentVideoId();
    if (!videoId) {
      setMessage("打开一个普通 YouTube 视频后再启用。");
      return publicState();
    }
    const video = sourceVideo();
    if (!video) {
      setMessage("YouTube 播放器还没有准备好。");
      return publicState();
    }
    const generation = routeGeneration;
    preparing = true;
    if (!quiet) panelOpen = true;
    try {
      setMessage("优先加载字幕…");
      await ensureSubtitleFirst(true);
      if (generation !== routeGeneration) return publicState();
      setMessage("连接本机学习服务…");
      const bootstrap = await callWorker({ type: "bootstrap" });
      profile = bootstrap.profile;
      const pack = findPack(bootstrap.packs?.packs) || await preparePack(generation, forceRetry);
      setMessage("加载本次观看…");
      session = await callWorker({ type: "createSession", pack_id: pack.pack_id, playhead_sec: Math.max(0, Number(video.currentTime || 0)) });
      if (["watch_ready", "probe_ready"].includes(session.stage)) {
        session = await callWorker({ type: "startSession", session_id: session.session_id });
      } else if (["watch", "probe", "probe_feedback"].includes(session.stage)) {
        if (session.owner_conflict && !forceClaim) {
          sessionTakeoverRequired = true;
          throw new Error("此视频的学习正在另一个标签页运行；字幕保持可用。需要时可手动接管。");
        }
        if (forceClaim) session = await callWorker({ type: "claimSession", session_id: session.session_id });
      }
      sessionTakeoverRequired = false;
      if (session.stage !== "watch") throw new Error("这次观看包含尚未适配插件的听音验证，请先在本地播放器完成");
      profile = session.profile;
      setMessage("预加载字幕和原声片段…");
      await preloadSessionAssets(generation);
      handledIds = new Set(session.completed_ids || []);
      encounteredIds = new Set(session.encountered_ids || []);
      previousTime = video.currentTime;
      progressSeq = Number(session.progress_seq || 0);
      manualSeeks = 0;
      technicalFailures = 0;
      watchStartedAt = performance.now();
      enabled = true;
      preparing = false;
      focusEpoch = session.progressive ? 1 : 0;
      lastProgressiveFocusSec = session.progressive ? Math.max(0, Number(video.currentTime || 0)) : null;
      const frequency = profile.effective_frequency || profile.effective_intensity || "medium";
      const expected = Number(session.intervention_budgets?.[frequency] || 0);
      setMessage(`找到 ${session.candidate_pool_count} 个表达 · 当前可选 ${session.eligible_candidate_count} 个 · 预计介入 ${expected} 次`);
      applyDisplaySize();
      observeVideoBounds(video);
      attachVideo(video);
      startMonitor();
      progressTimer = setInterval(() => saveProgress(false), 5000);
      if (session.progressive) progressiveSyncTimer = setInterval(syncProgressiveAssets, 3000);
      panelOpen = false;
      renderStatus();
      return publicState();
    } catch (error) {
      preparing = false;
      enabled = false;
      setMessage(String(error?.message || error), true);
      return publicState();
    }
  }

  async function stopLearning(reason = "automatic_learning_off") {
    cancelAutoEnable();
    ++routeGeneration;
    if (importJob && preparing) {
      try { await callWorker({ type: "cancelImport", job_id: importJob.job_id }); } catch {}
    }
    preparing = false;
    if (activeInteraction) await finalizeInteraction("technical_failure", reason);
    await saveProgress(true);
    clearInterval(progressTimer);
    clearInterval(progressiveSyncTimer);
    progressTimer = null;
    progressiveSyncTimer = null;
    focusEpoch = 0;
    lastProgressiveFocusSec = null;
    enabled = false;
    session = null;
    importJob = null;
    continuousPlaybackStartedAt = 0;
    suppressLearningUntilMediaTime = null;
    audioCache.clear();
    hideOverlay();
    stopMonitor();
    startMonitor();
    setMessage(readyMessage());
    renderStatus();
    return publicState();
  }

  async function disable(reason = "user_disabled") {
    if (["user_disabled", "popup_disabled"].includes(reason)) manualDisabledVideoId = videoId;
    cancelAutoEnable();
    releaseNativeCaptionBridge();
    ++routeGeneration;
    if (importJob && preparing) {
      try { await callWorker({ type: "cancelImport", job_id: importJob.job_id }); } catch {}
    }
    preparing = false;
    if (activeInteraction) await finalizeInteraction("technical_failure", reason);
    await saveProgress(true);
    stopMonitor();
    clearInterval(progressTimer);
    clearInterval(progressiveSyncTimer);
    progressTimer = null;
    progressiveSyncTimer = null;
    focusEpoch = 0;
    lastProgressiveFocusSec = null;
    enabled = false;
    learningRetryBlocked = false;
    sessionTakeoverRequired = false;
    session = null;
    subtitleActive = false;
    subtitleState = "idle";
    subtitleTaskVideoId = null;
    subtitleTask = null;
    nativeCaptionsOwned = false;
    nativeCaptionBridgeAttemptedFor = null;
    adObserver?.disconnect();
    adObserver = null;
    observedPlayer = null;
    lastAdActive = false;
    continuousPlaybackStartedAt = 0;
    captions = [];
    captionBundles = [];
    transcriptCues = [];
    currentCaptionCue = null;
    currentCaptionRange = null;
    suppressLearningUntilMediaTime = null;
    selectedWord = null;
    wordPanel.hidden = true;
    videoBoundsObserver?.disconnect();
    videoBoundsObserver = null;
    audioCache.clear();
    caption.hidden = true;
    hideOverlay();
    setMessage("当前视频的 InFlow 已暂停；YouTube 保持原样。 ");
    renderStatus();
    return publicState();
  }

  function attachVideo(video) {
    if (video.dataset.inflowBound === "1") return;
    video.dataset.inflowBound = "1";
    video.addEventListener("seeking", () => {
      if (!enabled) return;
      userGeneration += 1;
      manualSeeks += 1;
      if (activeInteraction) finalizeInteraction("technical_failure", "user_seek");
    });
    video.addEventListener("seeked", () => {
      previousTime = video.currentTime;
      if (subtitleActive || enabled) renderCaption(video.currentTime);
      if (session?.progressive) focusProgressiveAt(video.currentTime);
    });
    video.addEventListener("pause", () => {
      if (expectedPause > 0) { expectedPause -= 1; return; }
      if (enabled && activeInteraction) {
        userGeneration += 1;
        activeInteraction.pauseOwned = false;
      }
    });
    video.addEventListener("play", () => {
      if (expectedPlay > 0) { expectedPlay -= 1; return; }
      if (enabled && activeInteraction) {
        userGeneration += 1;
        activeInteraction.pauseOwned = false;
      }
      startMonitor();
    });
    video.addEventListener("ended", finishSession);
  }

  function stopMonitor() {
    if (monitorHandle === null) return;
    const video = sourceVideo();
    if (monitorMode === "video" && video?.cancelVideoFrameCallback) video.cancelVideoFrameCallback(monitorHandle);
    else cancelAnimationFrame(monitorHandle);
    monitorHandle = null;
    monitorMode = null;
  }

  function startMonitor() {
    const video = sourceVideo();
    if ((!enabled && !subtitleActive) || !video || video.paused || video.ended || monitorHandle !== null) return;
    const tick = () => {
      monitorHandle = null;
      monitorMode = null;
      processTime();
      startMonitor();
    };
    if (video.requestVideoFrameCallback) {
      monitorMode = "video";
      monitorHandle = video.requestVideoFrameCallback(tick);
    } else {
      monitorMode = "raf";
      monitorHandle = requestAnimationFrame(tick);
    }
  }

  function bundleAt(time) {
    for (let index = captionBundles.length - 1; index >= 0; index -= 1) {
      const row = captionBundles[index];
      if (time >= Number(row.start) && time <= Number(row.end)) return row;
    }
    return null;
  }

  function mergeCaptionFragments(rows) {
    const merged = [];
    [...rows]
      .sort((left, right) => Number(left.start) - Number(right.start) || Number(left.end) - Number(right.end))
      .forEach((row) => {
        const text = String(row.text || "").trim();
        if (!text) return;
        const previous = merged.at(-1);
        if (!previous) { merged.push(text); return; }
        if (previous === text || previous.includes(text)) return;
        if (text.includes(previous)) { merged[merged.length - 1] = text; return; }
        merged.push(text);
      });
    return merged.reduce((output, text) => {
      if (!output) return text;
      const joinsChinese = /[\u3400-\u9fff]$/.test(output) && /^[\u3400-\u9fff]/.test(text);
      return output + (joinsChinese ? "" : " ") + text;
    }, "").replace(/\s+/g, " ").replace(/^[。！？!?，,、；;\s]+/, "").trim();
  }

  function captionAt(time, cue = null) {
    if (cue) {
      const cueStart = Number(cue.start_sec);
      const cueEnd = Number(cue.end_sec);
      const cueDuration = Math.max(0.001, cueEnd - cueStart);
      const overlaps = captions.filter((row) => {
        const rowStart = Number(row.start);
        const rowEnd = Number(row.end);
        const overlap = Math.min(cueEnd, rowEnd) - Math.max(cueStart, rowStart);
        const required = Math.max(0.08, Math.min(0.35, Math.min(cueDuration, Math.max(0.001, rowEnd - rowStart)) * 0.12));
        return overlap >= required;
      });
      if (overlaps.length) {
        const soundMatch = String(cue.text || "").match(/^\[(applause|music|laughter|cheering|silence)\]/i);
        const soundMap = { applause: "[掌声]", music: "[音乐]", laughter: "[笑声]", cheering: "[欢呼]", silence: "[无声]" };
        const expectedSound = soundMatch ? soundMap[soundMatch[1].toLocaleLowerCase()] : null;
        const filtered = overlaps.filter((row) => {
          const text = String(row.text || "").trim();
          const pureSound = Object.values(soundMap).includes(text);
          return !(expectedSound && pureSound && text !== expectedSound);
        });
        const selected = filtered.length ? filtered : overlaps;
        return {
          id: `merged:${cue.cue_id || cueStart}`,
          start: Math.min(...selected.map((row) => Number(row.start))),
          end: Math.max(...selected.map((row) => Number(row.end))),
          text: mergeCaptionFragments(selected),
        };
      }
      return null;
    }
    for (let index = captions.length - 1; index >= 0; index -= 1) {
      const row = captions[index];
      if (time >= Number(row.start) && time <= Number(row.end)) return row;
    }
    return null;
  }

  function transcriptCueAt(time) {
    for (let index = transcriptCues.length - 1; index >= 0; index -= 1) {
      const cue = transcriptCues[index];
      if (time >= Number(cue.start_sec) && time <= Number(cue.end_sec)) return cue;
    }
    return null;
  }

  function normalizedExpression(value) {
    return String(value || "").toLocaleLowerCase().replaceAll("’", "'").replace(/\s+/g, " ").replace(/^[\s.,!?;:'\"()[\]{}]+|[\s.,!?;:'\"()[\]{}]+$/g, "");
  }

  function contextFingerprint(value) {
    const normalized = String(value || "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
    let digest = 0xcbf29ce484222325n;
    for (const byte of new TextEncoder().encode(normalized)) {
      digest ^= BigInt(byte);
      digest = BigInt.asUintN(64, digest * 0x100000001b3n);
    }
    return `ctx1:${digest.toString(16).padStart(16, "0")}`;
  }

  function lexicalEntry(surface, sentence) {
    const normalized = normalizedExpression(surface);
    const contextId = contextFingerprint(sentence);
    const matches = lexiconEntries.filter((entry) =>
      normalizedExpression(entry.surface) === normalized
      && Array.isArray(entry.context_fingerprints)
      && entry.context_fingerprints.includes(contextId)
    );
    return matches.length === 1 ? matches[0] : null;
  }

  function tokenButton(surface, entry = null) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "token";
    button.textContent = surface;
    button.dataset.surface = surface;
    button.dataset.status = entry?.status || "unseen";
    if (entry?.knowledge_key) button.dataset.knowledgeKey = entry.knowledge_key;
    if (entry?.gloss_zh) button.dataset.glossZh = entry.gloss_zh;
    button.addEventListener("click", (event) => {
      if (!event.isTrusted) return;
      openWordPanel(button);
    });
    return button;
  }

  function appendWordTokens(container, text) {
    const parts = String(text || "").match(/[A-Za-z]+(?:['’][A-Za-z]+)*|[^A-Za-z]+/g) || [];
    parts.forEach((part) => {
      if (/^[A-Za-z]/.test(part)) container.appendChild(tokenButton(part, lexicalEntry(part, text)));
      else container.appendChild(document.createTextNode(part));
    });
  }

  function renderEnglishCue(cue) {
    captionEnglish.textContent = "";
    if (!cue?.text) return;
    const text = String(cue.text);
    const contextId = contextFingerprint(text);
    const ranges = [];
    [...lexiconEntries]
      .filter((entry) =>
        entry.surface && entry.knowledge_key
        && Array.isArray(entry.context_fingerprints)
        && entry.context_fingerprints.includes(contextId)
      )
      .sort((a, b) => String(b.surface).length - String(a.surface).length)
      .forEach((entry) => {
        const escaped = String(entry.surface).replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
        const pattern = new RegExp(`(^|[^A-Za-z])(${escaped})(?=$|[^A-Za-z])`, "ig");
        let match;
        while ((match = pattern.exec(text))) {
          const start = match.index + match[1].length;
          const end = start + match[2].length;
          if (!ranges.some((range) => start < range.end && end > range.start)) ranges.push({ start, end, entry });
          if (pattern.lastIndex === match.index) pattern.lastIndex += 1;
        }
      });
    ranges.sort((a, b) => a.start - b.start);
    let cursor = 0;
    ranges.forEach((range) => {
      appendWordTokens(captionEnglish, text.slice(cursor, range.start));
      captionEnglish.appendChild(tokenButton(text.slice(range.start, range.end), range.entry));
      cursor = range.end;
    });
    appendWordTokens(captionEnglish, text.slice(cursor));
  }

  function renderCaption(time) {
    if (inAd()) {
      caption.hidden = true;
      return;
    }
    const bundle = bundleAt(time);
    const cue = bundle
      ? (bundle.text_en ? { cue_id: bundle.id, start_sec: bundle.start, end_sec: bundle.end, text: bundle.text_en } : null)
      : transcriptCueAt(time);
    const chinese = bundle
      ? (bundle.text_zh ? { id: bundle.id, start: bundle.start, end: bundle.end, text: bundle.text_zh } : null)
      : captionAt(time, cue);
    currentCaptionCue = cue;
    if (!cue && !chinese) {
      currentCaptionRange = null;
      caption.hidden = true;
      captionEnglish.textContent = "";
      captionChinese.textContent = "";
      return;
    }
    renderEnglishCue(cue);
    captionChinese.textContent = chinese?.text || "";
    captionEnglish.hidden = !cue?.text;
    captionChinese.hidden = !chinese?.text;
    caption.dataset.languages = cue?.text && chinese?.text ? "bilingual" : cue?.text ? "en" : "zh";
    currentCaptionRange = {
      start: Math.max(0, Number(cue?.start_sec ?? chinese?.start ?? time)),
      end: Math.max(Number(cue?.end_sec ?? chinese?.end ?? time), Number(cue?.start_sec ?? chinese?.start ?? time)),
    };
    caption.setAttribute("aria-label", [cue?.text, chinese?.text].filter(Boolean).join("。"));
    caption.hidden = false;
  }

  function isTypingTarget(target) {
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest("input,textarea,select,[contenteditable='true'],[role='textbox'],[role='dialog'] input,[role='dialog'] textarea"));
  }

  function replayCurrentCaption() {
    if (activeInteraction || !currentCaptionRange || inAd() || document.hidden) return;
    const video = sourceVideo();
    if (!video) return;
    userGeneration += 1;
    suppressLearningUntilMediaTime = currentCaptionRange.end + 0.35;
    video.currentTime = Math.max(0, currentCaptionRange.start - 0.08);
    video.play().catch(() => {});
    startMonitor();
  }

  async function openWordPanel(button) {
    if (activeInteraction) return;
    const surface = String(button.dataset.surface || "").trim();
    if (!surface) return;
    selectedWord = {
      surface,
      sentence: String(currentCaptionCue?.text || surface),
      knowledge_key: button.dataset.knowledgeKey || "",
      gloss_zh: button.dataset.glossZh || "",
      status: button.dataset.status || "unseen",
    };
    wordSurface.textContent = surface;
    wordGloss.textContent = selectedWord.gloss_zh || "正在读取当前义项…";
    wordPanel.hidden = false;
    if (selectedWord.gloss_zh) return;
    const requestedSurface = surface;
    try {
      const result = await callWorker({ type: "lexiconLookup", surface, sentence: selectedWord.sentence });
      if (!selectedWord || selectedWord.surface !== requestedSurface) return;
      selectedWord = { ...selectedWord, ...result };
      wordGloss.textContent = result.gloss_zh;
    } catch {
      if (selectedWord?.surface === requestedSurface) wordGloss.textContent = "暂时没有可靠释义；这次不记录状态。";
    }
  }

  async function saveWordFeedback(feedback) {
    if (!selectedWord?.gloss_zh) return;
    const result = await callWorker({
      type: "lexiconFeedback",
      knowledge_key: selectedWord.knowledge_key,
      surface: selectedWord.surface,
      gloss_zh: selectedWord.gloss_zh,
      sentence: selectedWord.sentence,
      familiarity_feedback: feedback,
      source: "subtitle",
    });
    lexiconEntries = lexiconEntries.filter((entry) => entry.knowledge_key !== result.knowledge_key);
    lexiconEntries.push(result);
    selectedWord = { ...selectedWord, ...result };
    if (currentCaptionCue) renderEnglishCue(currentCaptionCue);
    wordPanel.hidden = true;
    selectedWord = null;
  }

  function processTime() {
    const video = sourceVideo();
    if (!video || video.paused || video.ended || document.hidden) return;
    if (inAd()) {
      caption.hidden = true;
      return;
    }
    const current = video.currentTime;
    syncVideoBounds();
    if (subtitleActive || enabled) renderCaption(current);
    if (suppressLearningUntilMediaTime !== null) {
      if (current <= suppressLearningUntilMediaTime) {
        previousTime = current;
        return;
      }
      suppressLearningUntilMediaTime = null;
    }
    if (!enabled || !session || session.stage !== "watch" || activeInteraction) {
      previousTime = current;
      return;
    }
    const delta = current - previousTime;
    const normalCrossing = delta >= -0.05 && delta <= 0.75;
    const crossedEncounters = (session.encounter_catalog || []).filter((item) => !encounteredIds.has(item.id) && Number(item.anchor_sec) > previousTime && Number(item.anchor_sec) <= current);
    const crossed = (session.items || []).filter((item) => !handledIds.has(item.id) && Number(item.anchor_sec) > previousTime && Number(item.anchor_sec) <= current).sort((a, b) => Number(a.anchor_sec) - Number(b.anchor_sec));
    previousTime = current;
    if (!normalCrossing) {
      crossed.forEach((item) => handledIds.add(item.id));
      return;
    }
    crossedEncounters.forEach((item) => {
      encounteredIds.add(item.id);
      callWorker({ type: "encounter", session_id: session.session_id, owner_epoch: session.owner_epoch, item_id: item.id }).catch(() => encounteredIds.delete(item.id));
    });
    const frequency = profile.effective_frequency || profile.effective_intensity || "medium";
    const candidate = crossed.find((item) => RANK[frequency] >= RANK[item.min_intensity]);
    crossed.filter((item) => item !== candidate).forEach((item) => handledIds.add(item.id));
    if (!candidate) return;
    const safeGap = Number(candidate.safe_pause_gap_sec);
    const pauseDelay = Number(candidate.pause_delay_sec || 0);
    const lateTolerance = Number.isFinite(safeGap) && safeGap > 0
      ? Math.max(0.05, Math.min(0.45, safeGap - pauseDelay - 0.03))
      : 0.18;
    if (current - Number(candidate.anchor_sec) > lateTolerance) return;
    beginInteraction(candidate);
  }

  function phraseMarkup(item) {
    const phrase = String(item.phrase_text || item.surface);
    const target = String(item.surface || "");
    const index = phrase.toLocaleLowerCase().indexOf(target.toLocaleLowerCase());
    if (index < 0) return escapeHtml(phrase);
    return `${escapeHtml(phrase.slice(0, index))}<mark id="targetMark">${escapeHtml(phrase.slice(index, index + target.length))}</mark>${escapeHtml(phrase.slice(index + target.length))}`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
  }

  function showListening() {
    captionEnglish.textContent = "";
    captionChinese.textContent = "";
    caption.hidden = true;
    overlay.hidden = false;
    overlay.classList.remove("concealing");
    phase.textContent = "原声片段";
    title.textContent = "先听一遍";
    content.innerHTML = `<div class="listening"><span class="dot"></span><span>只听声音</span></div>`;
    actions.innerHTML = `<button id="skip" class="quiet" type="button">跳过这次</button>`;
    shadow.querySelector("#skip").addEventListener("click", (event) => {
      if (!event.isTrusted) return;
      finalizeInteraction("skipped", "user_skip");
    });
  }

  function teachingTranslation(item) {
    const expected = normalizedExpression(item.phrase_text || "");
    const cue = transcriptCues.find((row) => normalizedExpression(row.text || "") === expected);
    if (cue) {
      const translated = captionAt((Number(cue.start_sec) + Number(cue.end_sec)) / 2, cue);
      if (translated?.text) return translated.text;
    }
    return String(item.phrase_zh || "");
  }

  function showMapping(item, note = "") {
    phase.textContent = "声音与意思";
    title.textContent = "看清这个表达";
    content.innerHTML = `<p class="word">${escapeHtml(item.surface)}</p><p class="gloss">${escapeHtml(item.gloss_zh)}</p><p class="label">原声片段</p><p class="phrase">${phraseMarkup(item)}</p><p class="translation">${escapeHtml(teachingTranslation(item))}</p><p id="audioState">${escapeHtml(note)}</p>`;
    actions.innerHTML = `<button id="continueLearning" class="primary" type="button" disabled>看清了，继续 <kbd>Enter</kbd></button><button id="replay" class="secondary" type="button" disabled>再听一遍 <kbd>R</kbd></button><button id="suppressSense" class="quiet" type="button" disabled>这个义项以后不用解释</button>`;
  }

  function hideOverlay() {
    overlay.classList.add("concealing");
    setTimeout(() => {
      overlay.hidden = true;
      overlay.classList.remove("concealing");
      content.innerHTML = "";
      actions.innerHTML = "";
      const video = sourceVideo();
      if (enabled && video) renderCaption(video.currentTime);
      else caption.hidden = true;
    }, 150);
  }

  function playPhrase(item, highlight = false) {
    return new Promise((resolve) => {
      const source = audioCache.get(item.id);
      if (!source) return resolve({ kind: "missing", confirmed: false });
      const audio = new Audio(source);
      let playing = false;
      let settled = false;
      const finish = (kind) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        audio.pause();
        shadow.querySelector("#targetMark")?.classList.remove("active");
        resolve({ kind, confirmed: playing && kind === "ended" });
      };
      audio.addEventListener("playing", () => { playing = true; });
      audio.addEventListener("ended", () => finish("ended"));
      audio.addEventListener("error", () => finish("error"));
      if (highlight) audio.addEventListener("timeupdate", () => {
        const mark = shadow.querySelector("#targetMark");
        if (!mark || item.alignment_quality !== "word_timestamp_high") return;
        mark.classList.toggle("active", audio.currentTime >= Number(item.highlight_start_sec) && audio.currentTime <= Number(item.highlight_end_sec));
      });
      const timer = setTimeout(() => finish("timeout"), 12000);
      audio.play().catch(() => finish("blocked"));
      if (activeInteraction) activeInteraction.audio = audio;
    });
  }

  async function beginInteraction(item) {
    const video = sourceVideo();
    if (!video || activeInteraction || document.hidden || inAd()) return;
    const generation = userGeneration;
    const videoAtStart = videoId;
    activeInteraction = { item, interactionId: null, pauseOwned: false, leaseGeneration: generation, firstConfirmed: false, secondConfirmed: false, replays: 0, mappingShownAt: 0, finalized: false, audio: null };
    try {
      const started = await callWorker({ type: "interactionStart", session_id: session.session_id, owner_epoch: session.owner_epoch, item_id: item.id });
      activeInteraction.interactionId = started.interaction.interaction_id;
      if (currentVideoId() !== videoAtStart || document.hidden || inAd() || video.paused || userGeneration !== generation || video.currentTime - Number(item.anchor_sec) > 0.18) {
        await finalizeInteraction("technical_failure", "safe_boundary_missed");
        return;
      }
      expectedPause += 1;
      const wasPlaying = !video.paused;
      video.pause();
      activeInteraction.pauseOwned = wasPlaying && video.paused;
      activeInteraction.leaseGeneration = userGeneration;
      showListening();
      activeInteraction.firstConfirmed = (await playPhrase(item, false)).confirmed;
      if (!activeInteraction || activeInteraction.finalized) return;
      activeInteraction.mappingShownAt = performance.now();
      showMapping(item, activeInteraction.firstConfirmed ? "" : "第一遍没有正常播放；意思仍然保留");
      const continueButton = shadow.querySelector("#continueLearning");
      const replayButton = shadow.querySelector("#replay");
      const suppressButton = shadow.querySelector("#suppressSense");
      const mappingControls = [continueButton, replayButton, suppressButton].filter(Boolean);
      const setControlsDisabled = (disabled) => {
        mappingControls.forEach((button) => { button.disabled = disabled; });
      };
      const replay = async (manual) => {
        if (!activeInteraction || activeInteraction.finalized) return;
        setControlsDisabled(true);
        if (manual) activeInteraction.replays += 1;
        const result = await playPhrase(item, true);
        if (!activeInteraction || activeInteraction.finalized) return;
        if (result.confirmed) activeInteraction.secondConfirmed = true;
        const audioState = shadow.querySelector("#audioState");
        if (audioState) audioState.textContent = result.confirmed ? "" : "声音没有正常播放，可以再听一次";
        setControlsDisabled(false);
        continueButton?.focus();
      };
      replayButton.addEventListener("click", (event) => {
        if (!trustedActivation(event, replayButton)) return;
        replay(true);
      });
      continueButton.addEventListener("click", (event) => {
        if (!trustedActivation(event, continueButton)) return;
        const completed = activeInteraction?.firstConfirmed && activeInteraction?.secondConfirmed;
        finalizeInteraction(completed ? "completed" : "technical_failure", completed ? "" : "phrase_audio_unconfirmed");
      });
      suppressButton.addEventListener("click", async (event) => {
        if (!event.isTrusted || !activeInteraction || activeInteraction.finalized) return;
        setControlsDisabled(true);
        try {
          const lexical = await callWorker({
            type: "lexiconFeedback",
            knowledge_key: item.knowledge_key,
            surface: item.surface,
            gloss_zh: item.gloss_zh,
            sentence: item.phrase_text,
            familiarity_feedback: "known",
            source: "explicit_no_more_explanations",
          });
          lexiconEntries = lexiconEntries.filter((entry) => entry.knowledge_key !== lexical.knowledge_key);
          lexiconEntries.push(lexical);
          const completed = activeInteraction?.firstConfirmed && activeInteraction?.secondConfirmed;
          await finalizeInteraction(completed ? "completed" : "technical_failure", completed ? "" : "phrase_audio_unconfirmed");
        } catch {
          const audioState = shadow.querySelector("#audioState");
          if (audioState) audioState.textContent = "偏好没有保存；可以重试或直接继续";
          setControlsDisabled(false);
        }
      });
      await replay(false);
    } catch (error) {
      if (!activeInteraction?.interactionId) {
        activeInteraction = null;
        hideOverlay();
        setMessage(`这次学习没有开始：${error.message}`);
        return;
      }
      technicalFailures += 1;
      showMapping(item, "声音暂时不可用；这次不会算成你不会");
      actions.innerHTML = `<button id="technicalContinue" class="primary" type="button">继续视频</button>`;
      shadow.querySelector("#technicalContinue").addEventListener("click", (event) => {
        if (!event.isTrusted) return;
        finalizeInteraction("technical_failure", "extension_audio_failure");
      });
    }
  }

  async function finalizeInteraction(outcome, reason = "", familiarityFeedback = null) {
    const interaction = activeInteraction;
    if (!interaction || interaction.finalized) return;
    interaction.finalized = true;
    interaction.audio?.pause();
    if (outcome === "technical_failure") technicalFailures += 1;
    const dwellMs = interaction.mappingShownAt ? performance.now() - interaction.mappingShownAt : 0;
    const shouldResume = interaction.pauseOwned && interaction.leaseGeneration === userGeneration && currentVideoId() === videoId && !document.hidden && !inAd();
    const save = callWorker({
      type: "interactionComplete",
      session_id: session.session_id,
      owner_epoch: session.owner_epoch,
      item_id: interaction.item.id,
      interaction_id: interaction.interactionId,
      outcome,
      dwell_ms: Math.round(dwellMs),
      phrase_confirmed: Boolean(interaction.firstConfirmed && interaction.secondConfirmed),
      replays: interaction.replays,
      familiarity_feedback: familiarityFeedback,
      failure_reason: reason,
    }).then((updated) => {
      session = updated;
      profile = updated.profile;
      handledIds.add(interaction.item.id);
      const currentItem = (updated.items || []).find((item) => item.id === interaction.item.id);
      if (currentItem?.knowledge_key && familiarityFeedback) {
        const previousEntry = lexiconEntries.find((entry) => entry.knowledge_key === currentItem.knowledge_key);
        const contexts = new Set(previousEntry?.context_fingerprints || []);
        contexts.add(contextFingerprint(currentItem.phrase_text));
        lexiconEntries = lexiconEntries.filter((entry) => entry.knowledge_key !== currentItem.knowledge_key);
        lexiconEntries.push({
          knowledge_key: currentItem.knowledge_key,
          surface: currentItem.surface,
          gloss_zh: currentItem.gloss_zh,
          status: familiarityFeedback,
          explicit_known: Boolean(currentItem.explicit_known),
          context_fingerprints: [...contexts],
        });
      }
    }).catch(() => handledIds.add(interaction.item.id));
    hideOverlay();
    const video = sourceVideo();
    if (shouldResume && video?.paused && !video.ended) {
      expectedPlay += 1;
      video.play().catch(() => { expectedPlay = Math.max(0, expectedPlay - 1); });
    }
    await save;
    if (activeInteraction === interaction) activeInteraction = null;
    startMonitor();
  }

  async function saveProgress(force) {
    const video = sourceVideo();
    if (!enabled || !session || session.stage !== "watch" || !video) return;
    if (!force && document.hidden) return;
    progressSeq += 1;
    try {
      await callWorker({ type: "progress", session_id: session.session_id, owner_epoch: session.owner_epoch, sequence: progressSeq, media_time: video.currentTime });
    } catch {}
  }

  async function finishSession() {
    if (!enabled || !session || session.stage !== "watch" || activeInteraction) return;
    try {
      session = await callWorker({ type: "completeSession", session_id: session.session_id, owner_epoch: session.owner_epoch, elapsed_ms: Math.round(performance.now() - watchStartedAt), manual_seeks: manualSeeks, technical_failures: technicalFailures });
      const learned = Number(session.interaction_summary?.completed || 0);
      setMessage(`这次处理了 ${learned} 个原声表达；记录已保存在本机。`);
    } catch (error) {
      setMessage(`观看记录没有完成：${error.message}`);
    }
    stopMonitor();
    clearInterval(progressTimer);
    clearInterval(progressiveSyncTimer);
    progressTimer = null;
    progressiveSyncTimer = null;
    focusEpoch = 0;
    lastProgressiveFocusSec = null;
    enabled = false;
    renderStatus();
  }

  async function routeChanged() {
    const next = currentVideoId();
    if (next === videoId) return;
    if (enabled || preparing || subtitleActive || subtitleTask) await disable("youtube_navigation");
    videoId = next;
    manualDisabledVideoId = null;
    session = null;
    importJob = null;
    panelOpen = false;
    setMessage(videoId ? waitingMessage() : "");
    renderStatus();
    manageAutoEnable();
  }

  function takeoverLearning() {
    sessionTakeoverRequired = false;
    statusError = false;
    panelOpen = true;
    setMessage("正在接管这个标签页的学习状态…");
    return activate({ quiet: false, forceClaim: true });
  }

  function retryLearning() {
    learningRetryBlocked = false;
    statusError = false;
    panelOpen = true;
    setMessage("正在重新准备学习内容…");
    return activate({ quiet: false, forceRetry: true });
  }

  function retrySubtitles() {
    routeGeneration += 1;
    manualDisabledVideoId = null;
    subtitleTaskVideoId = null;
    subtitleTask = null;
    subtitleState = "idle";
    statusError = false;
    panelOpen = false;
    setMessage("正在重新获取字幕…");
    return ensureSubtitleFirst(true).then(() => publicState());
  }

  pill.addEventListener("click", (event) => {
    if (!event.isTrusted) return;
    panelOpen = !panelOpen;
    renderStatus();
  });
  panelClose.addEventListener("click", (event) => {
    if (!event.isTrusted) return;
    panelOpen = false;
    renderStatus();
  });
  captionReplay.addEventListener("click", (event) => {
    if (!event.isTrusted) return;
    replayCurrentCaption();
  });
  wordClose.addEventListener("click", (event) => {
    if (!event.isTrusted) return;
    selectedWord = null;
    wordPanel.hidden = true;
  });
  shadow.querySelectorAll("[data-word-feedback]").forEach((button) => button.addEventListener("click", async (event) => {
    if (!event.isTrusted) return;
    const controls = [...shadow.querySelectorAll("[data-word-feedback]")];
    controls.forEach((control) => { control.disabled = true; });
    try { await saveWordFeedback(button.dataset.wordFeedback); }
    catch { wordGloss.textContent = "状态没有保存；原来的判断保持不变。"; }
    finally { controls.forEach((control) => { control.disabled = false; }); }
  }));
  panelPrimary.addEventListener("click", (event) => {
    if (!event.isTrusted) return;
    if (sessionTakeoverRequired) {
      takeoverLearning();
      return;
    }
    if (learningRetryBlocked) {
      retryLearning();
      return;
    }
    if (enabled || preparing || subtitleActive) {
      disable("user_disabled");
      return;
    }
    if (["failed", "degraded"].includes(subtitleState)) {
      retrySubtitles();
      return;
    }
    activate();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && activeInteraction) finalizeInteraction("technical_failure", "page_hidden");
  });
  document.addEventListener("keydown", (event) => {
    if (!event.isTrusted || isTypingTarget(event.target)) return;
    const key = event.key.toLocaleLowerCase();
    if (!activeInteraction) {
      if (key === "s" && !caption.hidden && currentCaptionRange) {
        event.preventDefault();
        event.stopPropagation();
        replayCurrentCaption();
      }
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      finalizeInteraction("technical_failure", "escape");
      return;
    }
    const target = event.key === "Enter"
      ? shadow.querySelector("#continueLearning")
      : key === "r"
        ? shadow.querySelector("#replay")
        : null;
    if (!target || target.disabled) return;
    event.preventDefault();
    event.stopPropagation();
    keyboardActivatedControl = target;
    try { target.click(); }
    finally { keyboardActivatedControl = null; }
  }, true);
  document.addEventListener("fullscreenchange", () => {
    const target = document.fullscreenElement || document.documentElement;
    if (host.parentElement !== target) target.appendChild(host);
    requestAnimationFrame(syncVideoBounds);
  });
  window.addEventListener("resize", syncVideoBounds, { passive: true });
  window.addEventListener("scroll", syncVideoBounds, { passive: true });
  window.addEventListener("yt-navigate-finish", routeChanged);
  window.addEventListener("popstate", routeChanged);
  setInterval(() => { routeChanged(); manageAutoEnable(); syncVideoBounds(); }, 1000);

  chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
    if (request?.type === "inflow:getState") { sendResponse(publicState()); return false; }
    if (request?.type === "inflow:activate") { activate().then(sendResponse); return true; }
    if (request?.type === "inflow:takeoverLearning") { takeoverLearning().then(sendResponse); return true; }
    if (request?.type === "inflow:retryLearning") { retryLearning().then(sendResponse); return true; }
    if (request?.type === "inflow:retrySubtitles") { retrySubtitles().then(sendResponse); return true; }
    if (request?.type === "inflow:disable") { disable("popup_disabled").then(sendResponse); return true; }
    if (request?.type === "inflow:settingsChanged") {
      const next = request.settings || {};
      const wasAutoLearning = autoLearning;
      autoMode = next.autoMode !== false;
      autoLearning = next.autoLearning === true;
      displaySize = ["small", "medium", "large"].includes(next.displaySize) ? next.displaySize : "medium";
      if (!wasAutoLearning && autoLearning) continuousPlaybackStartedAt = performance.now();
      applyDisplaySize();
      syncVideoBounds();
      if (!autoMode && (enabled || preparing || subtitleActive || subtitleTask)) disable("auto_mode_off").then(sendResponse);
      else if (!autoLearning && (enabled || preparing || activeInteraction)) stopLearning("automatic_learning_off").then(sendResponse);
      else { renderStatus(); manageAutoEnable(); sendResponse(publicState()); }
      return true;
    }
    if (request?.type === "inflow:refreshProfile") {
      callWorker({ type: "profile" }).then((next) => { profile = next; sendResponse(publicState()); }).catch(() => sendResponse(publicState()));
      return true;
    }
    return false;
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    const wasAutoLearning = autoLearning;
    if (changes.autoMode) autoMode = changes.autoMode.newValue !== false;
    if (changes.autoLearning) autoLearning = changes.autoLearning.newValue === true;
    if (!wasAutoLearning && autoLearning) continuousPlaybackStartedAt = performance.now();
    if (changes.displaySize && ["small", "medium", "large"].includes(changes.displaySize.newValue)) displaySize = changes.displaySize.newValue;
    applyDisplaySize();
    if (!autoMode && (enabled || preparing || subtitleActive || subtitleTask)) disable("auto_mode_off");
    else if (!autoLearning && (enabled || preparing || activeInteraction)) stopLearning("automatic_learning_off");
    else { renderStatus(); manageAutoEnable(); }
  });

  chrome.storage.local.get({ autoMode: true, autoLearning: false, displaySize: "medium" }).then((stored) => {
    autoMode = stored.autoMode !== false;
    autoLearning = stored.autoLearning === true;
    displaySize = ["small", "medium", "large"].includes(stored.displaySize) ? stored.displaySize : "medium";
    applyDisplaySize();
    syncVideoBounds();
    setMessage(videoId ? waitingMessage() : "");
    manageAutoEnable();
  });
})();
