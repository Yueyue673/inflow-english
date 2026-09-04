(() => {
  if (location.pathname !== "/watch") return;
  const INSTALL_KEY = "__inflowCaptionCaptureV1";
  if (globalThis[INSTALL_KEY]) return;
  Object.defineProperty(globalThis, INSTALL_KEY, { value: true, configurable: false, enumerable: false });

  const MAX_BYTES = 5_000_000;
  const MAX_ENTRIES = 6;
  const TTL_MS = 120_000;
  const entries = [];

  function captionIdentity(rawUrl) {
    try {
      const url = new URL(String(rawUrl || ""), location.href);
      if (url.protocol !== "https:" || !new Set(["youtube.com", "www.youtube.com", "m.youtube.com"]).has(url.hostname) || url.pathname !== "/api/timedtext") return null;
      return {
        video_id: String(url.searchParams.get("v") || ""),
        language: String(url.searchParams.get("lang") || "").toLowerCase(),
        translated_language: String(url.searchParams.get("tlang") || "").toLowerCase(),
      };
    } catch {
      return null;
    }
  }

  function cachePayload(rawUrl, payload, byteLength) {
    const identity = captionIdentity(rawUrl);
    if (!identity || !/^[A-Za-z0-9_-]{11}$/.test(identity.video_id) || !identity.language) return;
    if (!payload || !Array.isArray(payload.events) || !Number.isFinite(byteLength) || byteLength <= 10 || byteLength > MAX_BYTES) return;
    const now = Date.now();
    const key = `${identity.video_id}|${identity.language}|${identity.translated_language}`;
    const index = entries.findIndex((entry) => entry.key === key);
    const row = { key, ...identity, payload, byte_length: byteLength, captured_at: now };
    if (index >= 0) entries.splice(index, 1);
    entries.push(row);
    while (entries.length > MAX_ENTRIES) entries.shift();
  }

  async function captureFetchResponse(response) {
    try {
      const identity = captionIdentity(response?.url);
      if (!identity || !response.ok) return;
      const declared = Number(response.headers.get("content-length") || 0);
      if (Number.isFinite(declared) && declared > MAX_BYTES) return;
      const buffer = await response.clone().arrayBuffer();
      if (buffer.byteLength <= 10 || buffer.byteLength > MAX_BYTES) return;
      const text = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
      const payload = JSON.parse(text);
      cachePayload(response.url, payload, buffer.byteLength);
    } catch {}
  }

  const originalFetch = globalThis.fetch;
  if (typeof originalFetch === "function") {
    globalThis.fetch = async function inflowObservedFetch(...args) {
      const response = await originalFetch.apply(this, args);
      void captureFetchResponse(response);
      return response;
    };
  }

  const xhrOpen = XMLHttpRequest.prototype.open;
  const xhrSend = XMLHttpRequest.prototype.send;
  const urlKey = Symbol("inflowTimedTextUrl");
  XMLHttpRequest.prototype.open = function inflowObservedOpen(method, url, ...rest) {
    this[urlKey] = String(url || "");
    return xhrOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function inflowObservedSend(...args) {
    if (captionIdentity(this[urlKey])) {
      this.addEventListener("loadend", () => {
        try {
          if (this.status < 200 || this.status >= 300) return;
          let payload;
          let text;
          if (this.responseType === "json") {
            payload = this.response;
            text = JSON.stringify(payload);
          } else if (!this.responseType || this.responseType === "text") {
            text = String(this.responseText || "");
            payload = JSON.parse(text);
          } else {
            return;
          }
          cachePayload(this.responseURL || this[urlKey], payload, new TextEncoder().encode(text).byteLength);
        } catch {}
      }, { once: true });
    }
    return xhrSend.apply(this, args);
  };

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (event.source !== window || event.origin !== location.origin || data?.source !== "inflow-caption-cache-request") return;
    const nonce = String(data.nonce || "");
    const videoId = String(data.video_id || "");
    if (!/^[0-9a-f-]{16,64}$/i.test(nonce) || !/^[A-Za-z0-9_-]{11}$/.test(videoId)) return;
    const cutoff = Date.now() - TTL_MS;
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      if (entries[index].captured_at < cutoff) entries.splice(index, 1);
    }
    window.postMessage({
      source: "inflow-caption-cache-response",
      nonce,
      video_id: videoId,
      entries: entries
        .filter((entry) => entry.video_id === videoId)
        .map(({ language, translated_language, payload, byte_length, captured_at }) => ({ language, translated_language, payload, byte_length, captured_at })),
    }, location.origin);
  });
})();
