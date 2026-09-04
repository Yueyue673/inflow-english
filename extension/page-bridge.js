(() => {
  const script = document.currentScript;
  const nonce = String(script?.dataset?.inflowNonce || "");
  const expectedVideoId = String(script?.dataset?.inflowVideoId || "");
  const send = (payload) => {
    window.postMessage({ ...payload, source: "inflow-page-caption-bridge", nonce }, location.origin);
  };
  const fail = (code) => send({ ok: false, error: String(code || "youtube_page_bridge_failed") });

  const captionHosts = new Set(["youtube.com", "www.youtube.com", "m.youtube.com"]);
  const MAX_CAPTION_EVENTS = 50_000;
  const MAX_VIDEO_DURATION_SEC = 24 * 60 * 60;
  const CAPTURE_CACHE_WAIT_MS = 120;
  const TRACK_SWITCH_WAIT_MS = 250;
  const GET_OPTION_RETRY_MS = 75;
  const FETCH_TIMEOUT_MS = 2800;

  function validatedCaptionUrl(rawUrl) {
    const value = String(rawUrl || "");
    if (value.length < 10 || value.length > 8192) throw new Error("youtube_caption_url_size_invalid");
    const url = new URL(value);
    if (url.protocol !== "https:" || !captionHosts.has(url.hostname) || url.pathname !== "/api/timedtext" || url.username || url.password || url.port) {
      throw new Error("youtube_caption_path_invalid");
    }
    return url;
  }

  function currentWatchVideoId() {
    try {
      const url = new URL(location.href);
      return url.pathname === "/watch" ? String(url.searchParams.get("v") || "") : "";
    } catch { return ""; }
  }

  function playerMatchesVideo(player, expectedVideoId) {
    if (currentWatchVideoId() !== expectedVideoId) return false;
    try { return String(player?.getPlayerResponse?.()?.videoDetails?.videoId || "") === expectedVideoId; }
    catch { return false; }
  }

  function observedTimedTextUrl(languageCode, { untranslated = false } = {}) {
    const expectedLanguage = String(languageCode || "").toLowerCase();
    const entries = performance.getEntriesByType("resource").slice(-500).reverse();
    for (const entry of entries) {
      try {
        const url = validatedCaptionUrl(entry?.name);
        const video = String(url.searchParams.get("v") || expectedVideoId);
        const language = String(url.searchParams.get("lang") || "").toLowerCase();
        if (untranslated && url.searchParams.has("tlang")) continue;
        if (video === expectedVideoId && language === expectedLanguage) return url.toString();
      } catch {}
    }
    return null;
  }

  function currentCaptionTrack(player) {
    if (typeof player?.getOption !== "function") return null;
    try { return player.getOption("captions", "track") || null; }
    catch { return null; }
  }

  function requestCapturedCaptionEntries() {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (entries) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        window.removeEventListener("message", onMessage);
        resolve(entries);
      };
      const onMessage = (event) => {
        const data = event.data;
        if (event.source !== window || event.origin !== location.origin || data?.source !== "inflow-caption-cache-response" || data?.nonce !== nonce || data?.video_id !== expectedVideoId) return;
        const safe = (Array.isArray(data.entries) ? data.entries : []).filter((entry) =>
          /^[a-z0-9-]{2,20}$/.test(String(entry?.language || ""))
          && /^([a-z0-9-]{0,20})$/.test(String(entry?.translated_language || ""))
          && Number.isFinite(Number(entry?.byte_length))
          && Number(entry.byte_length) > 10
          && Number(entry.byte_length) <= 5_000_000
          && entry?.payload
          && Array.isArray(entry.payload.events)
          && entry.payload.events.length <= MAX_CAPTION_EVENTS
        ).slice(-6);
        finish(safe);
      };
      const timer = setTimeout(() => finish([]), CAPTURE_CACHE_WAIT_MS);
      window.addEventListener("message", onMessage);
      window.postMessage({ source: "inflow-caption-cache-request", nonce, video_id: expectedVideoId }, location.origin);
    });
  }

  function pickCaptured(entries, kind) {
    return [...entries].reverse().find((entry) => {
      const language = String(entry.language || "").toLowerCase();
      const translated = String(entry.translated_language || "").toLowerCase();
      return kind === "english"
        ? language.startsWith("en") && !translated
        : language.startsWith("zh") || translated.startsWith("zh");
    })?.payload || null;
  }

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function captureTrackWithPlayer(player, track, expectedVideoId) {
    if (typeof player?.setOption !== "function" || !playerMatchesVideo(player, expectedVideoId)) return [];
    try {
      player.setOption("captions", "track", track);
      await wait(TRACK_SWITCH_WAIT_MS);
      if (!playerMatchesVideo(player, expectedVideoId)) return [];
      return await requestCapturedCaptionEntries();
    } catch {
      return [];
    }
  }

  async function fetchTrack(track, translatedLanguage = null, observedUrl = null) {
    if (!track?.baseUrl) return null;
    const url = validatedCaptionUrl(observedUrl || track.baseUrl);
    url.searchParams.set("fmt", "json3");
    if (translatedLanguage) {
      if (!(observedUrl && url.searchParams.has("tlang"))) url.searchParams.set("tlang", translatedLanguage);
    } else {
      url.searchParams.delete("tlang");
    }
    const controller = new AbortController();
    let timer = null;
    const operation = (async () => {
      const response = await fetch(url.toString(), { cache: "no-store", credentials: "same-origin", redirect: "error", signal: controller.signal });
      if (!response.ok) throw new Error(`youtube_caption_http_${response.status}`);
      const declaredLength = Number(response.headers.get("content-length") || 0);
      if (Number.isFinite(declaredLength) && declaredLength > 5_000_000) throw new Error("youtube_caption_declared_too_large");
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
          throw new Error("youtube_caption_stream_too_large");
        }
        chunks.push(value);
      }
      if (total <= 10) throw new Error("youtube_caption_stream_empty");
      const bytes = new Uint8Array(total);
      let offset = 0;
      chunks.forEach((chunk) => { bytes.set(chunk, offset); offset += chunk.byteLength; });
      const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      let payload;
      try { payload = JSON.parse(text); } catch { throw new Error("youtube_caption_payload_not_json"); }
      if (!payload || !Array.isArray(payload.events) || payload.events.length > MAX_CAPTION_EVENTS) throw new Error("youtube_caption_payload_invalid");
      return payload;
    })();
    const hardTimeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new Error("youtube_caption_fetch_timeout"));
      }, FETCH_TIMEOUT_MS);
    });
    try {
      return await Promise.race([operation, hardTimeout]);
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
    if (!Number.isFinite(duration) || duration <= 0 || duration > MAX_VIDEO_DURATION_SEC) throw new Error("youtube_player_duration_invalid");
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
    let capturedEntries = await requestCapturedCaptionEntries();
    let english = pickCaptured(capturedEntries, "english");
    let chinese = pickCaptured(capturedEntries, "chinese");
    let previousTrack = currentCaptionTrack(player);
    if (!previousTrack && typeof player?.setOption === "function") {
      await wait(GET_OPTION_RETRY_MS);
      previousTrack = currentCaptionTrack(player);
    }
    if (previousTrack) {
      try {
        if (!english && englishTrack) {
          capturedEntries = [...capturedEntries, ...await captureTrackWithPlayer(player, { languageCode: String(englishTrack.languageCode || "en") }, expectedVideoId)].slice(-6);
          english = pickCaptured(capturedEntries, "english");
          chinese ||= pickCaptured(capturedEntries, "chinese");
        }
        if (!chinese && chineseTrack) {
          capturedEntries = [...capturedEntries, ...await captureTrackWithPlayer(player, { languageCode: String(chineseTrack.languageCode || "zh") }, expectedVideoId)].slice(-6);
          chinese = pickCaptured(capturedEntries, "chinese");
        }
        if (!chinese && englishTrack?.isTranslatable) {
          capturedEntries = [...capturedEntries, ...await captureTrackWithPlayer(player, {
            languageCode: String(englishTrack.languageCode || "en"),
            translationLanguage: { languageCode: "zh-Hans" },
          }, expectedVideoId)].slice(-6);
          chinese = pickCaptured(capturedEntries, "chinese");
        }
      } finally {
        if (playerMatchesVideo(player, expectedVideoId) && typeof player?.setOption === "function") {
          try { player.setOption("captions", "track", previousTrack); } catch {}
        }
      }
    }
    if (!playerMatchesVideo(player, expectedVideoId)) throw new Error("youtube_player_video_mismatch");

    const englishLanguage = String(englishTrack?.languageCode || "en");
    const currentEnglishResource = englishTrack ? observedTimedTextUrl(englishLanguage) : null;
    const currentEnglishUrl = currentEnglishResource ? validatedCaptionUrl(currentEnglishResource) : null;
    const englishObservedUrl = englishTrack && currentEnglishUrl && !currentEnglishUrl.searchParams.has("tlang")
      ? currentEnglishUrl.toString()
      : null;
    const chineseObservedUrl = chineseTrack
      ? observedTimedTextUrl(String(chineseTrack.languageCode || "zh"))
      : (currentEnglishUrl?.searchParams.has("tlang") ? currentEnglishUrl.toString() : englishObservedUrl);
    const [englishResult, chineseResult] = await Promise.allSettled([
      english ? Promise.resolve(english) : fetchTrack(englishTrack, null, englishObservedUrl),
      chinese
        ? Promise.resolve(chinese)
        : chineseTrack
          ? fetchTrack(chineseTrack, null, chineseObservedUrl)
          : (englishTrack?.isTranslatable ? fetchTrack(englishTrack, "zh-Hans", englishObservedUrl) : Promise.resolve(null)),
    ]);
    english = englishResult.status === "fulfilled" ? englishResult.value : null;
    chinese = chineseResult.status === "fulfilled" ? chineseResult.value : null;
    if (!english && !chinese) {
      const reason = (result) => String(result?.reason?.message || "youtube_caption_fetch_failed").replace(/[^A-Za-z0-9_:-]/g, "").slice(0, 80) || "youtube_caption_fetch_failed";
      throw new Error(`youtube_caption_fetch_empty:${reason(englishResult)}:${reason(chineseResult)}`);
    }
    if (!playerMatchesVideo(player, expectedVideoId)) throw new Error("youtube_player_video_mismatch");
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
