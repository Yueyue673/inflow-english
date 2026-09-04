(() => {
  const script = document.currentScript;
  const nonce = String(script?.dataset?.inflowNonce || "");
  const expectedVideoId = String(script?.dataset?.inflowVideoId || "");
  const send = (payload) => {
    window.postMessage({ ...payload, source: "inflow-page-caption-bridge", nonce }, location.origin);
  };
  const fail = (code) => send({ ok: false, error: String(code || "youtube_page_bridge_failed") });

  async function fetchTrack(track, translatedLanguage = null) {
    if (!track?.baseUrl) return null;
    const rawUrl = String(track.baseUrl);
    if (rawUrl.length < 10 || rawUrl.length > 8192) throw new Error("youtube_caption_url_size_invalid");
    const url = new URL(rawUrl);
    const captionHosts = new Set(["youtube.com", "www.youtube.com", "m.youtube.com"]);
    if (url.protocol !== "https:" || !captionHosts.has(url.hostname) || url.pathname !== "/api/timedtext" || url.username || url.password || url.port) {
      throw new Error("youtube_caption_path_invalid");
    }
    url.searchParams.set("fmt", "json3");
    if (translatedLanguage) url.searchParams.set("tlang", translatedLanguage);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3200);
    try {
      const response = await fetch(url.toString(), { cache: "no-store", credentials: "omit", redirect: "error", signal: controller.signal });
      if (!response.ok) throw new Error(`youtube_caption_http_${response.status}`);
      const declaredLength = Number(response.headers.get("content-length") || 0);
      if (Number.isFinite(declaredLength) && declaredLength > 5_000_000) throw new Error("youtube_caption_payload_size_invalid");
      if (!response.body?.getReader) throw new Error("youtube_caption_stream_unavailable");
      const reader = response.body.getReader();
      const chunks = [];
      let total = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > 5_000_000) {
          controller.abort();
          throw new Error("youtube_caption_payload_size_invalid");
        }
        chunks.push(value);
      }
      if (total <= 10) throw new Error("youtube_caption_payload_size_invalid");
      const bytes = new Uint8Array(total);
      let offset = 0;
      chunks.forEach((chunk) => { bytes.set(chunk, offset); offset += chunk.byteLength; });
      const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      let payload;
      try { payload = JSON.parse(text); } catch { throw new Error("youtube_caption_payload_not_json"); }
      if (!payload || !Array.isArray(payload.events)) throw new Error("youtube_caption_payload_invalid");
      return payload;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("youtube_caption_fetch_timeout");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  (async () => {
    if (!/^[A-Za-z0-9_-]{11}$/.test(expectedVideoId) || !/^[0-9a-f-]{16,64}$/i.test(nonce)) {
      fail("youtube_page_bridge_request_invalid");
      return;
    }
    const player = document.querySelector("#movie_player");
    const playerResponse = typeof player?.getPlayerResponse === "function" ? player.getPlayerResponse() : null;
    const initialResponse = globalThis.ytInitialPlayerResponse || null;
    const response = [playerResponse, initialResponse].find((candidate) => String(candidate?.videoDetails?.videoId || "") === expectedVideoId)
      || playerResponse || initialResponse;
    const details = response?.videoDetails || {};
    if (String(details.videoId || "") !== expectedVideoId) throw new Error("youtube_player_video_mismatch");
    const duration = Number(details.lengthSeconds || document.querySelector("video")?.duration || 0);
    if (!Number.isFinite(duration) || duration <= 0 || duration > 3 * 60 * 60) throw new Error("youtube_player_duration_invalid");
    const tracks = response?.captions?.playerCaptionsTracklistRenderer?.captionTracks;
    if (!Array.isArray(tracks) || !tracks.length) throw new Error("youtube_caption_tracks_missing");
    const boundedTracks = tracks.slice(0, 100);
    const choose = (prefix) => boundedTracks
      .filter((track) => {
        const code = String(track?.languageCode || "").toLowerCase();
        return code === prefix || code.startsWith(`${prefix}-`);
      })
      .sort((left, right) => Number(left?.kind === "asr") - Number(right?.kind === "asr"))[0] || null;
    const englishTrack = choose("en");
    const chineseTrack = choose("zh");
    if (!englishTrack && !chineseTrack) throw new Error("youtube_supported_caption_track_missing");
    const [englishResult, chineseResult] = await Promise.allSettled([
      fetchTrack(englishTrack),
      chineseTrack
        ? fetchTrack(chineseTrack)
        : (englishTrack?.isTranslatable ? fetchTrack(englishTrack, "zh-Hans") : Promise.resolve(null)),
    ]);
    const english = englishResult.status === "fulfilled" ? englishResult.value : null;
    const chinese = chineseResult.status === "fulfilled" ? chineseResult.value : null;
    if (!english && !chinese) {
      const reason = (result) => String(result?.reason?.message || "youtube_caption_fetch_failed").replace(/[^A-Za-z0-9_:-]/g, "").slice(0, 80) || "youtube_caption_fetch_failed";
      throw new Error(`youtube_caption_fetch_empty:${reason(englishResult)}:${reason(chineseResult)}`);
    }
    send({
      ok: true,
      video_id: expectedVideoId,
      title: String(details.title || "").slice(0, 300),
      duration_sec: duration,
      english,
      chinese,
      caption_source: "youtube_page_player",
    });
  })().catch((error) => {
    const raw = String(error?.message || error || "youtube_page_bridge_failed");
    fail(/^[A-Za-z0-9_:-]{1,180}$/.test(raw) ? raw : "youtube_page_bridge_failed");
  }).finally(() => script?.remove());
})();
