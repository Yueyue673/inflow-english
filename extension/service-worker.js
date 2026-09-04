const API_BASE = "http://127.0.0.1:8767";
const LOCAL_SERVICE_PERMISSION = "http://127.0.0.1:8767/*";
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const PACK_ID = /^[A-Za-z0-9_-]{1,96}$/;
const HEX32 = /^[0-9a-f]{32}$/;

function assert(condition, code) {
  if (!condition) throw new Error(code);
}

function safeId(value, pattern, code) {
  const text = String(value || "");
  assert(pattern.test(text), code);
  return text;
}

function youtubeUrl(value) {
  const url = new URL(String(value || ""));
  assert(url.protocol === "https:" && ["www.youtube.com", "youtube.com", "m.youtube.com"].includes(url.hostname), "unsupported_youtube_url");
  assert(url.pathname === "/watch", "youtube_watch_page_required");
  const id = url.searchParams.get("v") || "";
  safeId(id, VIDEO_ID, "invalid_video_id");
  return `https://www.youtube.com/watch?v=${id}`;
}

async function localServicePermissionGranted() {
  return chrome.permissions.contains({ origins: [LOCAL_SERVICE_PERMISSION] });
}

async function api(path, { method = "GET", body = undefined } = {}) {
  if (!(await localServicePermissionGranted())) throw new Error("local_service_permission_required");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch(API_BASE + path, {
      method,
      cache: "no-store",
      signal: controller.signal,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: "invalid_server_response" }; }
    if (!response.ok) {
      const error = new Error(String(payload?.detail || `HTTP ${response.status}`));
      error.status = response.status;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("local_service_timeout");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function clientIdForSender(sender) {
  const documentId = String(sender?.documentId || "");
  assert(/^[A-Za-z0-9_-]{8,200}$/.test(documentId), "youtube_document_id_required");
  const key = `owner:${documentId}`;
  const stored = await chrome.storage.session.get(key);
  if (HEX32.test(String(stored[key] || ""))) return stored[key];
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const value = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  await chrome.storage.session.set({ [key]: value });
  return value;
}

async function ownerBody(sender, body = {}) {
  return { ...body, client_id: await clientIdForSender(sender) };
}

function sessionId(value) { return safeId(value, HEX32, "invalid_session_id"); }
function packId(value) { return safeId(value, PACK_ID, "invalid_pack_id"); }
function playhead(value) {
  const number = Number(value || 0);
  assert(Number.isFinite(number) && number >= 0 && number <= 3 * 60 * 60, "invalid_playhead");
  return number;
}
function itemId(value) {
  const text = String(value || "");
  assert(text.length >= 1 && text.length <= 300 && !/[\x00-\x1f\x7f]/.test(text), "invalid_item_id");
  return encodeURIComponent(text);
}
function familiarity(value, source = "") {
  const text = String(value || "");
  const allowed = source === "subtitle" ? ["known", "familiar", "unclear", "undo"] : ["known", "familiar", "unclear"];
  assert(allowed.includes(text), "invalid_familiarity_feedback");
  return text;
}
function lexicalText(value, kind) {
  const text = String(value || "").trim();
  const pattern = kind === "surface"
    ? /^[A-Za-z][A-Za-z'’\-]*(?:\s+[A-Za-z][A-Za-z'’\-]*){0,3}$/
    : /[\u3400-\u9fff]/;
  assert(text.length >= 1 && text.length <= (kind === "surface" ? 80 : 100) && pattern.test(text), `invalid_lexicon_${kind}`);
  return text;
}

function lexicalSentence(value) {
  const text = String(value || "").trim();
  assert(text.length >= 1 && text.length <= 500 && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text), "invalid_lexicon_sentence");
  return text;
}

function allowedAssetPath(value, kind) {
  const path = String(value || "");
  const pattern = kind === "audio"
    ? /^(?:\/api\/packs\/[A-Za-z0-9_-]{1,96}\/files\/phrases\/[A-Za-z0-9._-]+\.mp3|\/api\/progressive\/ytp-[A-Za-z0-9_-]{11}-[0-9a-f]{12}\/files\/shards\/w[0-9]{4}\/phrases\/[A-Za-z0-9._-]+\.mp3)$/
    : kind === "transcript"
      ? /^\/api\/packs\/[A-Za-z0-9_-]{1,96}\/files\/transcript\.json$/
      : /^\/api\/packs\/[A-Za-z0-9_-]{1,96}\/files\/captions-zh\.json$/;
  assert(pattern.test(path), "asset_path_not_allowed");
  return path;
}

function bytesToBase64(bytes) {
  let binary = "";
  const stride = 0x8000;
  for (let index = 0; index < bytes.length; index += stride) {
    binary += String.fromCharCode(...bytes.subarray(index, index + stride));
  }
  return btoa(binary);
}

async function assetAudio(path) {
  if (!(await localServicePermissionGranted())) throw new Error("local_service_permission_required");
  const response = await fetch(API_BASE + allowedAssetPath(path, "audio"), { cache: "force-cache" });
  assert(response.ok, `audio_http_${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  assert(bytes.length > 32 && bytes.length < 2_000_000, "audio_payload_invalid");
  return { data_url: `data:audio/mpeg;base64,${bytesToBase64(bytes)}` };
}

const POPUP_MESSAGE_TYPES = new Set(["health", "bootstrap", "setFrequency", "openLibrary"]);
const CONTENT_MESSAGE_TYPES = new Set([
  "health", "bootstrap", "profile", "lexicon", "lexiconLookup", "lexiconFeedback", "listPacks",
  "prepare", "importStatus", "cancelImport", "createSession", "progressiveFocus", "progressiveDelta",
  "syncProgressive", "getSession", "startSession", "claimSession", "progress", "encounter",
  "interactionStart", "interactionComplete", "completeSession", "captions", "transcript", "audio",
]);

function validateSender(message, sender) {
  assert(sender?.id === chrome.runtime.id, "foreign_sender");
  const type = String(message?.type || "");
  let url;
  try { url = new URL(String(sender?.url || "")); }
  catch { throw new Error("sender_url_invalid"); }
  if (url.protocol === "chrome-extension:" && url.hostname === chrome.runtime.id) {
    assert(url.pathname === "/popup.html" && POPUP_MESSAGE_TYPES.has(type), "popup_message_not_allowed");
    return;
  }
  assert(Number.isInteger(sender?.tab?.id) && sender?.frameId === 0, "youtube_top_frame_required");
  assert(typeof sender?.documentId === "string" && sender.documentId.length >= 8, "youtube_document_id_required");
  assert(url.protocol === "https:" && ["www.youtube.com", "youtube.com", "m.youtube.com"].includes(url.hostname), "youtube_sender_required");
  assert(url.pathname === "/watch", "youtube_watch_page_required");
  const videoId = url.searchParams.get("v") || "";
  safeId(videoId, VIDEO_ID, "invalid_video_id");
  assert(CONTENT_MESSAGE_TYPES.has(type), "content_message_not_allowed");
  if (message.url != null) {
    assert(youtubeUrl(message.url) === `https://www.youtube.com/watch?v=${videoId}`, "sender_video_mismatch");
  }
}

async function handle(message, sender) {
  assert(message && typeof message === "object", "invalid_message");
  switch (message.type) {
    case "health": return api("/api/health");
    case "bootstrap": {
      const [health, profile, packs] = await Promise.all([api("/api/health"), api("/api/profile"), api("/api/packs")]);
      return { health, profile, packs };
    }
    case "profile": return api("/api/profile");
    case "lexicon": return api("/api/lexicon");
    case "lexiconLookup": return api("/api/lexicon/lookup", { method: "POST", body: await ownerBody(sender, {
      surface: lexicalText(message.surface, "surface"),
      sentence: lexicalSentence(message.sentence),
    }) });
    case "lexiconFeedback": {
      const source = String(message.source || "subtitle").slice(0, 40);
      return api("/api/lexicon/feedback", { method: "POST", body: await ownerBody(sender, {
        knowledge_key: String(message.knowledge_key || ""),
        surface: lexicalText(message.surface, "surface"),
        gloss_zh: lexicalText(message.gloss_zh, "gloss"),
        sentence: lexicalSentence(message.sentence),
        familiarity_feedback: familiarity(message.familiarity_feedback, source),
        source,
      }) });
    }
    case "setFrequency": {
      const frequency = String(message.frequency || "");
      assert(["low", "medium", "high"].includes(frequency), "invalid_frequency");
      return api("/api/profile/intensity", { method: "PUT", body: { intensity: frequency } });
    }
    case "listPacks": return api("/api/packs");
    case "prepare": return api("/api/imports", { method: "POST", body: { url: youtubeUrl(message.url), playhead_sec: playhead(message.playhead_sec), force_retry: message.force_retry === true } });
    case "importStatus": return api(`/api/imports/${safeId(message.job_id, HEX32, "invalid_job_id")}`);
    case "cancelImport": return api(`/api/imports/${safeId(message.job_id, HEX32, "invalid_job_id")}/cancel`, { method: "POST", body: {} });
    case "createSession": return api("/api/sessions", { method: "POST", body: await ownerBody(sender, { pack_id: packId(message.pack_id), playhead_sec: playhead(message.playhead_sec), qa: false }) });
    case "progressiveFocus": {
      const epoch = Number(message.focus_epoch);
      assert(Number.isInteger(epoch) && epoch >= 1, "invalid_focus_epoch");
      return api(`/api/progressive/${packId(message.pack_id)}/focus`, { method: "POST", body: await ownerBody(sender, { playhead_sec: playhead(message.playhead_sec), focus_epoch: epoch }) });
    }
    case "progressiveDelta": {
      const revision = Number(message.since_revision ?? -1);
      assert(Number.isInteger(revision) && revision >= -1, "invalid_progressive_revision");
      return api(`/api/progressive/${packId(message.pack_id)}/delta?since_revision=${revision}&playhead_sec=${encodeURIComponent(playhead(message.playhead_sec))}`);
    }
    case "syncProgressive": return api(`/api/sessions/${sessionId(message.session_id)}/sync-progressive`, { method: "POST", body: await ownerBody(sender, { owner_epoch: Number(message.owner_epoch) || 0, playhead_sec: playhead(message.playhead_sec) }) });
    case "getSession": return api(`/api/sessions/${sessionId(message.session_id)}`);
    case "startSession": return api(`/api/sessions/${sessionId(message.session_id)}/start`, { method: "POST", body: await ownerBody(sender) });
    case "claimSession": return api(`/api/sessions/${sessionId(message.session_id)}/claim`, { method: "POST", body: await ownerBody(sender) });
    case "progress": return api(`/api/sessions/${sessionId(message.session_id)}/progress`, { method: "POST", body: await ownerBody(sender, { sequence: Number(message.sequence) || 0, media_time: Math.max(0, Number(message.media_time) || 0), owner_epoch: Number(message.owner_epoch) || 0 }) });
    case "encounter": return api(`/api/sessions/${sessionId(message.session_id)}/encounters/${itemId(message.item_id)}`, { method: "POST", body: await ownerBody(sender, { owner_epoch: Number(message.owner_epoch) || 0 }) });
    case "interactionStart": return api(`/api/sessions/${sessionId(message.session_id)}/interactions/start`, { method: "POST", body: await ownerBody(sender, { owner_epoch: Number(message.owner_epoch) || 0, item_id: String(message.item_id || "") }) });
    case "interactionComplete": return api(`/api/sessions/${sessionId(message.session_id)}/interactions/${itemId(message.item_id)}/complete`, { method: "POST", body: await ownerBody(sender, { owner_epoch: Number(message.owner_epoch) || 0, interaction_id: safeId(message.interaction_id, HEX32, "invalid_interaction_id"), outcome: String(message.outcome || ""), dwell_ms: Math.max(0, Number(message.dwell_ms) || 0), phrase_confirmed: Boolean(message.phrase_confirmed), replays: Math.max(0, Number(message.replays) || 0), familiarity_feedback: message.familiarity_feedback == null ? null : familiarity(message.familiarity_feedback), failure_reason: String(message.failure_reason || "").slice(0, 120) }) });
    case "completeSession": return api(`/api/sessions/${sessionId(message.session_id)}/complete`, { method: "POST", body: await ownerBody(sender, { owner_epoch: Number(message.owner_epoch) || 0, source_ended: true, elapsed_ms: Math.max(0, Number(message.elapsed_ms) || 0), manual_seeks: Math.max(0, Number(message.manual_seeks) || 0), technical_failures: Math.max(0, Number(message.technical_failures) || 0) }) });
    case "captions": return api(allowedAssetPath(message.path, "captions"));
    case "transcript": return api(allowedAssetPath(message.path, "transcript"));
    case "audio": return assetAudio(message.path);
    case "openLibrary": await chrome.tabs.create({ url: API_BASE + "/" }); return { opened: true };
    default: throw new Error("message_type_not_allowed");
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  try {
    validateSender(message, sender);
  } catch (error) {
    sendResponse({ ok: false, error: String(error?.message || error) });
    return false;
  }
  handle(message, sender)
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => sendResponse({ ok: false, error: String(error?.message || error), status: Number(error?.status || 0) }));
  return true;
});
