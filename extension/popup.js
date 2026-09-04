const connection = document.querySelector("#connection");
const status = document.querySelector("#status");
const primary = document.querySelector("#primary");
const library = document.querySelector("#library");
const autoMode = document.querySelector("#autoMode");
const autoLearning = document.querySelector("#autoLearning");
const vocabularySummary = document.querySelector("#vocabularySummary");
const frequencySettings = document.querySelector("#frequencySettings");
const LOCAL_SERVICE_PERMISSION = "http://127.0.0.1:8767/*";
let tabId = null;
let pageState = null;
let settings = { autoMode: true, autoLearning: false, displaySize: "medium" };

function worker(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (!response?.ok) return reject(new Error(response?.error || "扩展后台没有响应"));
      resolve(response.data);
    });
  });
}

function tab(message) {
  return new Promise((resolve, reject) => {
    if (!tabId) return resolve({});
    chrome.tabs.sendMessage(tabId, message, (response) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve(response || {});
    });
  });
}

function setLearningServiceAvailable(available) {
  library.disabled = !available;
  frequencySettings.querySelectorAll("input").forEach((input) => { input.disabled = !available; });
  frequencySettings.dataset.available = available ? "true" : "false";
}

async function localPermissionGranted() {
  return chrome.permissions.contains({ origins: [LOCAL_SERVICE_PERMISSION] });
}

function renderState(state) {
  pageState = state;
  if (!state?.supported) {
    status.textContent = "打开普通 YouTube 视频后，自动模式会在你连续观看时工作。";
    primary.disabled = true;
    return;
  }
  status.textContent = state.message || (state.subtitleActive ? "字幕已就绪；学习正在后台准备。" : settings.autoMode ? "正在优先准备字幕。" : `当前视频：${state.videoId}`);
  primary.disabled = false;
  primary.textContent = state.sessionTakeoverRequired
    ? "接管学习"
    : state.learningRetryBlocked
      ? "重试学习"
    : ["failed", "degraded"].includes(state.subtitleState)
      ? "重试字幕"
      : state.enabled || state.subtitleActive
        ? "关闭本视频"
        : state.preparing
          ? "取消当前准备"
          : "开启本视频";
}

async function broadcastSettings() {
  try { renderState(await tab({ type: "inflow:settingsChanged", settings })); } catch {}
}

async function refresh() {
  const stored = await chrome.storage.local.get({ autoMode: true, autoLearning: false, displaySize: "medium" });
  settings = {
    autoMode: stored.autoMode !== false,
    autoLearning: stored.autoLearning === true,
    displaySize: ["small", "medium", "large"].includes(stored.displaySize) ? stored.displaySize : "medium",
  };
  autoMode.checked = settings.autoMode;
  autoLearning.checked = settings.autoLearning;
  document.querySelector(`input[name="displaySize"][value="${settings.displaySize}"]`).checked = true;

  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  tabId = active?.id ?? null;
  if (!tabId) renderState({ supported: false });
  else {
    try { renderState(await tab({ type: "inflow:getState" })); }
    catch { renderState({ supported: false }); }
  }

  const permissionGranted = await localPermissionGranted();
  if (!permissionGranted) {
    if (settings.autoLearning) {
      settings.autoLearning = false;
      autoLearning.checked = false;
      await chrome.storage.local.set({ autoLearning: false });
      await broadcastSettings();
    }
    connection.textContent = "字幕可用 · 实验学习未授权";
    setLearningServiceAvailable(false);
    vocabularySummary.textContent = "学习画像：需授权本机服务";
    return;
  }

  let bootstrap = null;
  try {
    bootstrap = await worker({ type: "bootstrap" });
  } catch {
    connection.textContent = "字幕可用 · 学习服务未连接";
    setLearningServiceAvailable(false);
    vocabularySummary.textContent = "学习画像：本机服务未连接";
    return;
  }

  connection.textContent = bootstrap.health?.ok ? "字幕可用 · 学习服务已连接" : "字幕可用 · 学习服务未连接";
  setLearningServiceAvailable(Boolean(bootstrap.health?.ok));
  const frequency = bootstrap.profile?.effective_frequency || bootstrap.profile?.effective_intensity || "medium";
  document.querySelector(`input[name="frequency"][value="${frequency}"]`).checked = true;
  const counts = bootstrap.profile?.vocabulary_counts || {};
  vocabularySummary.textContent = `听到就懂 ${Number(counts.known || 0)} · 有点熟 ${Number(counts.familiar || 0)} · 还不清楚 ${Number(counts.unclear || 0)}`;
}

autoMode.addEventListener("change", async () => {
  settings.autoMode = autoMode.checked;
  await chrome.storage.local.set({ autoMode: settings.autoMode });
  await broadcastSettings();
  await refresh();
});

autoLearning.addEventListener("change", async () => {
  autoLearning.disabled = true;
  try {
    if (autoLearning.checked) {
      const granted = await chrome.permissions.request({ origins: [LOCAL_SERVICE_PERMISSION] });
      if (!granted) {
        autoLearning.checked = false;
        settings.autoLearning = false;
        status.textContent = "未授予本机服务权限；字幕保持可用，自动学习仍关闭。";
        return;
      }
    }
    settings.autoLearning = autoLearning.checked;
    await chrome.storage.local.set({ autoLearning: settings.autoLearning });
    await broadcastSettings();
    await refresh();
  } catch (error) {
    autoLearning.checked = false;
    settings.autoLearning = false;
    await chrome.storage.local.set({ autoLearning: false });
    status.textContent = `自动学习没有开启：${error.message}`;
  } finally {
    autoLearning.disabled = false;
  }
});

primary.addEventListener("click", async () => {
  if (!tabId || !pageState?.supported) return;
  primary.disabled = true;
  try {
    const action = pageState.sessionTakeoverRequired
      ? "inflow:takeoverLearning"
      : pageState.learningRetryBlocked
        ? "inflow:retryLearning"
      : ["failed", "degraded"].includes(pageState.subtitleState)
        ? "inflow:retrySubtitles"
        : pageState.enabled || pageState.preparing || pageState.subtitleActive
          ? "inflow:disable"
          : "inflow:retrySubtitles";
    renderState(await tab({ type: action }));
  } catch (error) {
    status.textContent = `当前视频没有改变：${error.message}`;
  } finally {
    primary.disabled = false;
  }
});

document.querySelectorAll('input[name="frequency"]').forEach((input) => {
  input.addEventListener("change", async () => {
    try {
      await worker({ type: "setFrequency", frequency: input.value });
      if (tabId) await tab({ type: "inflow:refreshProfile" });
      await refresh();
    } catch (error) {
      status.textContent = `介入频率没有保存：${error.message}`;
    }
  });
});

document.querySelectorAll('input[name="displaySize"]').forEach((input) => {
  input.addEventListener("change", async () => {
    settings.displaySize = input.value;
    await chrome.storage.local.set({ displaySize: settings.displaySize });
    await broadcastSettings();
  });
});

library.addEventListener("click", () => worker({ type: "openLibrary" }).then(() => window.close()));
refresh();
