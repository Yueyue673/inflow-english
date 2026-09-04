from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ExtensionContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_v3_has_stable_id_and_minimal_permissions(self):
        self.assertEqual(self.manifest["manifest_version"], 3)
        self.assertEqual(self.manifest["version"], "0.2.7")
        public_key = base64.b64decode(self.manifest["key"], validate=True)
        alphabet = "abcdefghijklmnop"
        extension_id = "".join(alphabet[byte >> 4] + alphabet[byte & 15] for byte in hashlib.sha256(public_key).digest()[:16])
        self.assertEqual(extension_id, "hfkkhkdpakcmpokgbihceoppleeokifd")
        self.assertEqual(self.manifest["permissions"], ["storage"])
        self.assertEqual(
            self.manifest["host_permissions"],
            ["https://www.youtube.com/*"],
        )
        self.assertEqual(
            self.manifest["optional_host_permissions"],
            ["http://127.0.0.1:8767/*"],
        )
        self.assertEqual(self.manifest["web_accessible_resources"], [{"resources": ["page-bridge.js"], "matches": ["https://www.youtube.com/*"]}])
        self.assertEqual(self.manifest["content_scripts"][0]["js"], ["page-hook.js"])
        self.assertEqual(self.manifest["content_scripts"][0]["run_at"], "document_start")
        self.assertEqual(self.manifest["content_scripts"][0]["world"], "MAIN")
        self.assertEqual(self.manifest["content_scripts"][1]["js"], ["content-script.js"])
        self.assertEqual(self.manifest["content_scripts"][1]["run_at"], "document_start")
        forbidden = {"cookies", "downloads", "webRequest", "nativeMessaging", "clipboardRead", "clipboardWrite", "<all_urls>"}
        self.assertFalse(forbidden.intersection(self.manifest["permissions"] + self.manifest["host_permissions"]))

    def test_release_builder_separates_development_and_first_store_upload_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "first"
            duplicate_output = Path(temporary) / "second"
            development = subprocess.run(
                ["python", "tools/build_extension.py", "--output-dir", str(output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
            )
            store = subprocess.run(
                ["python", "tools/build_extension.py", "--output-dir", str(output), "--store-first-upload"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
            )
            duplicate = subprocess.run(
                ["python", "tools/build_extension.py", "--output-dir", str(duplicate_output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
            )
            self.assertEqual(development.returncode, 0, development.stderr)
            self.assertEqual(store.returncode, 0, store.stderr)
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
            development_path = Path(json.loads(development.stdout)["archive"])
            store_path = Path(json.loads(store.stdout)["archive"])
            duplicate_path = Path(json.loads(duplicate.stdout)["archive"])
            with zipfile.ZipFile(development_path) as archive:
                development_manifest = json.loads(archive.read("manifest.json"))
                development_infos = archive.infolist()
            with zipfile.ZipFile(store_path) as archive:
                store_manifest = json.loads(archive.read("manifest.json"))
                store_infos = archive.infolist()
            self.assertIn("key", development_manifest)
            self.assertNotIn("key", store_manifest)
            expected_store_manifest = dict(development_manifest)
            expected_store_manifest.pop("key")
            self.assertEqual(store_manifest, expected_store_manifest)
            self.assertTrue(store_path.name.endswith("-CWS-first-upload.zip"))
            self.assertEqual(development_path.read_bytes(), duplicate_path.read_bytes())
            for info in [*development_infos, *store_infos]:
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.external_attr >> 16, 0o100644)
                self.assertEqual(info.extra, b"")
                self.assertEqual(info.comment, b"")
            for archive_path in (development_path, store_path):
                checksum = archive_path.with_suffix(archive_path.suffix + ".sha256").read_bytes()
                self.assertTrue(checksum.endswith(b"\n"))
                self.assertNotIn(b"\r", checksum)

    def test_store_assets_are_full_bleed_and_privacy_draft_discloses_local_data(self):
        pairs = [
            (ROOT / "store" / "assets" / "01-bilingual-captions.png", ROOT / "docs" / "images" / "youtube-subtitles.png"),
            (ROOT / "store" / "assets" / "02-learning-card.png", ROOT / "docs" / "images" / "learning-card.png"),
        ]
        for store_path, source_path in pairs:
            payload = store_path.read_bytes()
            self.assertEqual(payload, source_path.read_bytes())
            self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual((int.from_bytes(payload[16:20], "big"), int.from_bytes(payload[20:24], "big")), (1280, 800))
        listing = (ROOT / "store" / "LISTING.md").read_text(encoding="utf-8")
        popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
        self.assertNotIn("Web history: not collected", listing)
        for disclosure in ("Web history: collected for core functionality", "User activity: collected for core functionality", "Website content: collected for core functionality", "默认使用离线翻译", "当前视频 URL"):
            self.assertIn(disclosure, listing + popup)

    def test_manifest_references_real_files_and_all_scripts_parse(self):
        paths = [
            self.manifest["background"]["service_worker"],
            self.manifest["action"]["default_popup"],
            *[entry["js"][0] for entry in self.manifest["content_scripts"]],
            *self.manifest["icons"].values(),
            *self.manifest["action"]["default_icon"].values(),
            "popup.js",
            "popup.css",
            "page-bridge.js",
        ]
        self.assertTrue(all((EXTENSION / path).is_file() for path in paths))
        for script in ("service-worker.js", "popup.js", "content-script.js", "page-hook.js", "page-bridge.js"):
            result = subprocess.run(
                ["node", "--check", str(EXTENSION / script)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_main_world_hook_captures_only_bounded_youtube_timedtext(self):
        source = (EXTENSION / "page-hook.js").read_text(encoding="utf-8")
        for required in (
            'location.pathname !== "/watch"',
            'url.pathname !== "/api/timedtext"',
            "MAX_BYTES = 5_000_000",
            "MAX_EVENTS = 50_000",
            "MAX_ENTRIES = 6",
            "TTL_MS = 120_000",
            "response.clone()",
            "clone.body?.getReader",
            "total > MAX_BYTES",
            "await reader.cancel()",
            'source: "inflow-caption-cache-response"',
            "entries.length > MAX_ENTRIES",
        ):
            self.assertIn(required, source)
        self.assertNotIn("document.cookie", source)
        self.assertNotIn("response.clone().arrayBuffer()", source)
        self.assertNotIn("chrome.", source)
        self.assertNotIn("127.0.0.1", source)

    def test_main_world_hook_cancels_oversized_fetch_clone_before_buffering(self):
        source = (EXTENSION / "page-hook.js").read_text(encoding="utf-8")
        harness = """
        global.location = { pathname:'/watch', href:'https://www.youtube.com/watch?v=abcdefghijk', origin:'https://www.youtube.com' };
        global.listeners = [];
        global.sent = null;
        global.cancelled = false;
        global.window = global;
        global.addEventListener = (type, listener) => { if (type === 'message') listeners.push(listener); };
        global.postMessage = (payload) => { if (payload?.source === 'inflow-caption-cache-response') global.sent = payload; };
        class FakeXHR { addEventListener() {} }
        FakeXHR.prototype.open = function() {};
        FakeXHR.prototype.send = function() {};
        global.XMLHttpRequest = FakeXHR;
        global.fetch = async () => ({
          url:'https://www.youtube.com/api/timedtext?v=abcdefghijk&lang=en',
          ok:true,
          headers:{ get:() => null },
          clone:() => ({ body:{ getReader:() => {
            let sent=false;
            return {
              read:async () => sent ? {done:true} : (sent=true, {done:false,value:new Uint8Array(5_000_001)}),
              cancel:async () => { global.cancelled=true; },
            };
          } } }),
        });
        """ + source + """
        fetch('https://www.youtube.com/api/timedtext?v=abcdefghijk&lang=en');
        setTimeout(() => {
          const event={ source:window, origin:location.origin, data:{source:'inflow-caption-cache-request',nonce:'12345678-1234-1234-1234-123456789abc',video_id:'abcdefghijk'} };
          listeners.forEach(listener => listener(event));
          setTimeout(() => process.stdout.write(JSON.stringify({cancelled:global.cancelled,entries:global.sent?.entries?.length})), 10);
        }, 80);
        """
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"cancelled": True, "entries": 0})

    def test_main_world_hook_obeys_persisted_and_live_caption_control(self):
        source = (EXTENSION / "page-hook.js").read_text(encoding="utf-8")
        harness = """
        const originalFetch=async()=>({ok:true});
        function originalOpen(){} function originalSend(){}
        function XHR(){} XHR.prototype.open=originalOpen;XHR.prototype.send=originalSend;
        global.fetch=originalFetch;global.XMLHttpRequest=XHR;
        const listeners=[];global.window={addEventListener:(type,listener)=>{if(type==='message')listeners.push(listener)},postMessage(){}};
        global.location={origin:'https://www.youtube.com',href:'https://www.youtube.com/watch?v=abcdefghijk',pathname:'/watch'};
        const storage={value:'0'};global.localStorage={getItem:()=>storage.value,setItem:(_key,value)=>{storage.value=value}};
        """ + source + """
        const initial={fetch:global.fetch===originalFetch,open:XHR.prototype.open===originalOpen,send:XHR.prototype.send===originalSend};
        const control=(enabled)=>listeners[0]({source:global.window,origin:global.location.origin,data:{source:'inflow-caption-capture-control',enabled}});
        control(true);const enabled={fetch:global.fetch!==originalFetch,open:XHR.prototype.open!==originalOpen,send:XHR.prototype.send!==originalSend,stored:storage.value};
        control(false);const disabled={fetch:global.fetch===originalFetch,open:XHR.prototype.open===originalOpen,send:XHR.prototype.send===originalSend,stored:storage.value};
        process.stdout.write(JSON.stringify({initial,enabled,disabled}));
        """
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["initial"], {"fetch": True, "open": True, "send": True})
        self.assertEqual(payload["enabled"], {"fetch": True, "open": True, "send": True, "stored": "1"})
        self.assertEqual(payload["disabled"], {"fetch": True, "open": True, "send": True, "stored": "0"})

    def test_service_worker_is_bounded_not_an_arbitrary_localhost_proxy(self):
        source = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        allowed_messages = {
            "health", "bootstrap", "profile", "lexicon", "lexiconLookup", "lexiconFeedback", "setFrequency", "listPacks", "prepare", "importStatus",
            "cancelImport", "createSession", "progressiveFocus", "progressiveDelta", "syncProgressive", "getSession", "startSession", "claimSession", "progress",
            "encounter", "interactionStart", "interactionComplete", "completeSession", "captions", "transcript", "audio",
            "openLibrary",
        }
        declared = set()
        for line in source.splitlines():
            line = line.strip()
            if line.startswith('case "'):
                declared.add(line.split('"', 2)[1])
        self.assertEqual(declared, allowed_messages)
        self.assertIn("message_type_not_allowed", source)
        self.assertIn("allowedAssetPath", source)
        self.assertIn("local_service_timeout", source)
        self.assertIn("local_service_permission_required", source)
        self.assertIn("chrome.permissions.contains", source)
        self.assertIn("validateSender", source)
        self.assertIn("POPUP_MESSAGE_TYPES", source)
        self.assertIn("CONTENT_MESSAGE_TYPES", source)
        self.assertIn("youtube_top_frame_required", source)
        self.assertIn("sender_video_mismatch", source)
        self.assertIn("chrome.storage.session", source)
        self.assertIn('source === "subtitle" ? ["known", "familiar", "unclear", "undo"]', source)
        self.assertIn("owner:${documentId}", source)
        self.assertIn("setTimeout(() => controller.abort(), 3000)", source)
        self.assertNotIn("chrome.cookies", source)
        self.assertNotIn("chrome.scripting", source)
        self.assertNotIn("case \"fetch\"", source)
        self.assertNotIn("message.method", source)

    def test_content_script_uses_native_video_shadow_dom_and_user_owned_exit(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        for required in (
            'video.html5-main-video',
            'attachShadow({ mode: "closed" })',
            "requestVideoFrameCallback",
            "interactionStart",
            "interactionComplete",
            "activeInteraction.pauseOwned",
            'id="continueLearning"',
            'id="skipMapping"',
            'finalizeInteraction("skipped", "user_skip_mapping")',
            'id="suppressSense"',
            "explicit_no_more_explanations",
            "看清了，继续",
            "teachingTranslation",
            "normalizedExpression(row.text || \"\") === expected",
            'id="replay"',
            "familiarity_feedback",
            "syncProgressiveAssets",
            "focusProgressiveAt",
            "progressiveSyncTimer",
            "playhead_sec",
            "syncProgressive",
            "manageAutoEnable",
            "manualDisabledVideoId === videoId",
            "InFlow 已暂停",
            "ensureSubtitleFirst",
            "requestPageCaptionBridge",
            "inflow-page-caption-bridge",
            "youtube_page_caption_bridge_rpc_timeout",
            "backend_bootstrap_timeout",
            "backendAvailable",
            "autoLearning = false",
            "autoLearning = next.autoLearning === true",
            "autoLearning = stored.autoLearning === true",
            "stopLearning",
            "sessionTakeoverRequired",
            "takeoverLearning",
            "forceClaim: true",
            "接管学习",
            "重试字幕",
            "parsePageCaptionTrack",
            "subtitleActive",
            "loadCachedPackSubtitles",
            "enableNativeCaptionBridge",
            "nativeCaptionsOwned",
            "releaseNativeCaptionBridge",
            "observeAdState",
            "MutationObserver",
            'attributeFilter: ["class"]',
            'finalizeInteraction("technical_failure", "ad_started")',
            "!document.hidden && !inAd()",
            "replayCurrentCaption",
            "suppressLearningUntilMediaTime",
            "isTypingTarget",
            'id="captionReplay"',
            "syncVideoBounds",
            "--video-center-x",
            "displaySize",
            'host.dataset.dock = sideSpace >= 320',
            'host.dataset.videoVisible = "false"',
            'host.dataset.videoVisible === "true"',
            'activeInteraction.pauseOwned = false',
            'finalizeInteraction("technical_failure", "player_not_visible")',
            'id="wordOutcome"',
            'id="wordUndo"',
            "lexiconFeedback",
            "contextFingerprint",
            "context_fingerprints",
            "140ms",
            "yt-navigate-finish",
            'subtitleState !== "idle" || nativeCaptionsOwned',
            "document.hidden",
            "inAd()",
        ):
            self.assertIn(required, source)
        self.assertNotIn("autoplay = true", source)
        self.assertNotIn("chrome.cookies", source)

    def test_page_bridge_reads_player_and_returns_caption_payloads(self):
        bridge = (EXTENSION / "page-bridge.js").read_text(encoding="utf-8")
        self.assertIn('url.pathname !== "/api/timedtext"', bridge)
        self.assertIn('new Set(["youtube.com", "www.youtube.com", "m.youtube.com"])', bridge)
        self.assertIn('credentials: "same-origin"', bridge)
        self.assertIn('redirect: "error"', bridge)
        self.assertIn("value.length > 8192", bridge)
        self.assertIn("tracks.slice(0, 100)", bridge)
        self.assertIn("response.body.getReader()", bridge)
        self.assertIn("total > 5_000_000", bridge)
        self.assertIn("observedTimedTextUrl", bridge)
        self.assertIn('performance.getEntriesByType("resource")', bridge)
        self.assertIn('reject(new Error("youtube_caption_fetch_timeout"))', bridge)
        self.assertIn("Promise.race([operation, hardTimeout])", bridge)
        self.assertNotIn("waitForObservedTimedTextUrl", bridge)
        constants = {
            name: int(re.search(rf"const {name} = ([0-9_]+);", bridge).group(1).replace("_", ""))
            for name in ("CAPTURE_CACHE_WAIT_MS", "TRACK_SWITCH_WAIT_MS", "GET_OPTION_RETRY_MS", "FETCH_TIMEOUT_MS")
        }
        worst_case_ms = constants["CAPTURE_CACHE_WAIT_MS"] * 3 + constants["TRACK_SWITCH_WAIT_MS"] * 2 + constants["GET_OPTION_RETRY_MS"] + constants["FETCH_TIMEOUT_MS"]
        self.assertLess(worst_case_ms, 4000)
        self.assertNotIn("chrome.cookies", bridge)
        harness = """
        global.sent = null;
        global.messageListeners = new Set();
        global.window = {
          addEventListener: (type, listener) => { if (type === 'message') global.messageListeners.add(listener); },
          removeEventListener: (type, listener) => { if (type === 'message') global.messageListeners.delete(listener); },
          postMessage: (payload) => {
            if (payload?.source === 'inflow-caption-cache-request') {
              queueMicrotask(() => {
                const responseEvent = { source: global.window, origin: global.location.origin, data: { source: 'inflow-caption-cache-response', nonce: payload.nonce, video_id: payload.video_id, entries: [] } };
                [...global.messageListeners].forEach(listener => listener(responseEvent));
              });
            } else {
              global.sent = payload;
            }
          },
        };
        global.location = { origin: 'https://www.youtube.com', href: 'https://www.youtube.com/watch?v=abcdefghijk' };
        const currentScript = { dataset: { inflowNonce: '12345678-1234-1234-1234-123456789abc', inflowVideoId: 'abcdefghijk' }, remove() {} };
        const response = { videoDetails: { videoId: 'abcdefghijk', title: 'Fixture', lengthSeconds: '14400' }, captions: { playerCaptionsTracklistRenderer: { captionTracks: [{ baseUrl: 'https://www.youtube.com/api/timedtext?v=abcdefghijk&lang=en', languageCode: 'en', isTranslatable: true }] } } };
        global.resourceEntries = [{ name: 'https://www.youtube.com/api/timedtext?v=abcdefghijk&lang=en&pot=proof&tlang=zh' }];
        global.getOptionCalls = 0;
        const player = {
          getPlayerResponse: () => response,
          getOption: () => { global.getOptionCalls += 1; if (global.getOptionCalls === 1) throw new Error('captions_not_ready'); return { languageCode: 'en', translationLanguage: { languageCode: 'zh' } }; },
          setOption: (_module, _name, value) => {
            if (value?.languageCode === 'en' && !value?.translationLanguage) global.resourceEntries.push({ name: 'https://www.youtube.com/api/timedtext?v=abcdefghijk&lang=en&pot=proof' });
          },
        };
        global.document = { currentScript, querySelector: (selector) => selector === '#movie_player' ? player : { duration: 60 } };
        global.performance = { getEntriesByType: () => global.resourceEntries };
        global.fetchUrls = [];
        const captionBytes = new TextEncoder().encode(JSON.stringify({ events: [{ tStartMs: 0, dDurationMs: 1000, segs: [{ utf8: 'Hello' }] }] }));
        global.fetch = async (url) => {
          global.fetchUrls.push(String(url));
          return {
            ok: true,
            status: 200,
            headers: { get: name => name === 'content-length' ? String(captionBytes.byteLength) : null },
            body: { getReader: () => { let sent = false; return { read: async () => sent ? { done: true } : (sent = true, { done: false, value: captionBytes }) }; } },
          };
        };
        """ + bridge + "\nsetTimeout(() => process.stdout.write(JSON.stringify({sent:global.sent,urls:global.fetchUrls})), 900);"
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        payload = output["sent"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["video_id"], "abcdefghijk")
        self.assertEqual(len(payload["english"]["events"]), 1)
        self.assertEqual(payload["source"], "inflow-page-caption-bridge")
        self.assertEqual(payload["caption_source"], "youtube_page_player")
        self.assertEqual(len(output["urls"]), 2)
        parsed_urls = [urlsplit(value) for value in output["urls"]]
        queries = [parse_qs(value.query) for value in parsed_urls]
        self.assertTrue(all(query.get("pot") == ["proof"] for query in queries))
        self.assertTrue(any("tlang" not in query for query in queries))
        self.assertTrue(any(query.get("tlang") == ["zh-Hans"] for query in queries))

    def test_page_bridge_never_restores_an_old_track_after_spa_navigation(self):
        bridge = (EXTENSION / "page-bridge.js").read_text(encoding="utf-8")
        harness = """
        global.messages=[]; global.messageListeners=new Set();
        global.location={origin:'https://www.youtube.com',href:'https://www.youtube.com/watch?v=abcdefghijk'};
        global.window={
          addEventListener:(type,listener)=>{if(type==='message')global.messageListeners.add(listener)},
          removeEventListener:(type,listener)=>{if(type==='message')global.messageListeners.delete(listener)},
          postMessage:(payload)=>{
            if(payload?.source==='inflow-caption-cache-request') queueMicrotask(()=>[...global.messageListeners].forEach(listener=>listener({source:global.window,origin:global.location.origin,data:{source:'inflow-caption-cache-response',nonce:payload.nonce,video_id:payload.video_id,entries:[]}})));
            else global.messages.push(payload);
          }
        };
        const currentScript={dataset:{inflowNonce:'12345678-1234-1234-1234-123456789abc',inflowVideoId:'abcdefghijk'},remove(){}};
        const tracks=[{baseUrl:'https://www.youtube.com/api/timedtext?v=abcdefghijk&lang=en',languageCode:'en',isTranslatable:true}];
        let response={videoDetails:{videoId:'abcdefghijk',title:'A',lengthSeconds:'60'},captions:{playerCaptionsTracklistRenderer:{captionTracks:tracks}}};
        let currentTrack={languageCode:'es',route:'A'}; const setCalls=[];
        const player={getPlayerResponse:()=>response,getOption:()=>currentTrack,setOption:(_m,_n,value)=>{currentTrack=value;setCalls.push({at:Date.now(),value})}};
        global.document={currentScript,querySelector:(selector)=>selector==='#movie_player'?player:{duration:60}};
        global.performance={getEntriesByType:()=>[]};
        const bytes=new TextEncoder().encode(JSON.stringify({events:[{tStartMs:0,dDurationMs:1000,segs:[{utf8:'A'}]}]}));
        global.fetch=async()=>({ok:true,status:200,headers:{get:()=>String(bytes.byteLength)},body:{getReader:()=>{let sent=false;return{read:async()=>sent?{done:true}:(sent=true,{done:false,value:bytes})}}}});
        setTimeout(()=>{global.location.href='https://www.youtube.com/watch?v=lmnopqrstuv';response={videoDetails:{videoId:'lmnopqrstuv',title:'B',lengthSeconds:'60'}};currentTrack={languageCode:'fr',route:'B'}},100);
        """ + bridge + "\nsetTimeout(()=>process.stdout.write(JSON.stringify({currentTrack,setCalls,messages:global.messages})),900);"
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["currentTrack"], {"languageCode": "fr", "route": "B"})
        self.assertEqual(len(output["setCalls"]), 1)
        self.assertFalse(output["messages"][-1]["ok"])
        self.assertEqual(output["messages"][-1]["error"], "youtube_player_video_mismatch")

    def test_startup_status_is_visible_without_hover(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        self.assertIn("min-width:118px", source)
        self.assertIn("min-height:32px", source)
        self.assertIn("top:calc(var(--video-top,0px) + 12px)", source)
        self.assertNotIn("width:10px", source)
        self.assertNotIn("font-size:0", source)
        self.assertNotIn("color:transparent", source)

    def test_concurrent_same_surface_lookups_cannot_mix_context_identity(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("async function openWordPanel")
        end = source.index("\n\n  async function saveWordFeedback", start)
        function = source[start:end]
        script = """
        let activeInteraction=false,currentCaptionCue=null,selectedWord=null,wordSelectionGeneration=0,panelOpen=false;
        const wordSurface={},wordGloss={},wordPanel={hidden:true},wordUndo={hidden:true,disabled:false},wordFeedbackControls=[],wordOutcome={hidden:true};
        function renderStatus(){} function showWordStatus(){} function setWordFeedbackBusy(){}
        const pending={};
        function callWorker(payload){return new Promise(resolve=>{pending[payload.sentence]=resolve})}
        const button=()=>({dataset:{surface:'bank',status:'unseen',knowledgeKey:'',glossZh:''}});
        """ + function + """
        (async()=>{
          currentCaptionCue={text:'They sat beside the river bank.'}; const first=openWordPanel(button());
          currentCaptionCue={text:'She called the bank about her account.'}; const second=openWordPanel(button());
          pending['They sat beside the river bank.']({knowledge_key:'KEY_RIVER',gloss_zh:'河岸',status:'unseen'}); await first;
          const afterFirst={...selectedWord};
          pending['She called the bank about her account.']({knowledge_key:'KEY_ACCOUNT',gloss_zh:'银行',status:'unseen'}); await second;
          process.stdout.write(JSON.stringify({afterFirst,final:selectedWord}));
        })();
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["afterFirst"]["sentence"], "She called the bank about her account.")
        self.assertNotEqual(output["afterFirst"].get("knowledge_key"), "KEY_RIVER")
        self.assertEqual(output["final"]["knowledge_key"], "KEY_ACCOUNT")
        self.assertEqual(output["final"]["sentence"], "She called the bank about her account.")

    def test_stale_activation_cannot_start_prepare_or_replace_closed_message(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("function activationIsCurrent")
        end = source.index("\n\n  async function stopLearning", start)
        functions = source[start:end]
        script = """
        let enabled=false,preparing=false,manualDisabledVideoId=null,videoId='abcdefghijk',routeGeneration=0,panelOpen=false,profile=null,session=null,importJob=null;
        let currentId='abcdefghijk',message='',listCalls=0,prepareCalls=0,bootstrapResolve;
        const video={currentTime:12,paused:false};
        function currentVideoId(){return currentId} function canonicalUrl(){return 'https://www.youtube.com/watch?v=abcdefghijk'}
        function sourceVideo(){return video} function cancelAutoEnable(){} function renderStatus(){}
        function setMessage(value){message=value} function publicState(){return {enabled,preparing,message,importJob}}
        async function ensureSubtitleFirst(){return true}
        function callWorker(payload){
          if(payload.type==='bootstrap') return new Promise(resolve=>{bootstrapResolve=resolve});
          if(payload.type==='listPacks'){listCalls+=1;return Promise.resolve({packs:[]})}
          if(payload.type==='prepare'){prepareCalls+=1;return Promise.resolve({job_id:'queued'})}
          throw new Error('unexpected:'+payload.type);
        }
        """ + functions + """
        (async()=>{
          const activation=activate({quiet:true});
          while(!bootstrapResolve) await new Promise(resolve=>setImmediate(resolve));
          routeGeneration+=1;preparing=false;manualDisabledVideoId=videoId;message='当前视频的 InFlow 已暂停';
          bootstrapResolve({profile:{},packs:{packs:[]}});
          await activation;
          process.stdout.write(JSON.stringify({listCalls,prepareCalls,message,importJob,preparing,enabled}));
        })();
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["listCalls"], 0)
        self.assertEqual(output["prepareCalls"], 0)
        self.assertEqual(output["message"], "当前视频的 InFlow 已暂停")
        self.assertIsNone(output["importJob"])
        self.assertFalse(output["preparing"])
        self.assertFalse(output["enabled"])

    def test_hidden_player_cannot_start_automatic_learning(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("function manageAutoEnable")
        end = source.index("\n\n  function activationIsCurrent", start)
        function = source[start:end]
        script = """
        let videoId='abcdefghijk',subtitleState='ready',autoMode=true,autoLearning=true,backendAvailable=true,learningRetryBlocked=false,sessionTakeoverRequired=false,manualDisabledVideoId=null,enabled=false,preparing=false,activeInteraction=null,subtitleActive=true,continuousPlaybackStartedAt=1000,autoEnableTimer=null,activateCalls=0;
        const host={dataset:{videoVisible:'false'}}, video={paused:false,ended:false};
        global.document={hidden:false}; global.performance={now:()=>10000};
        function currentVideoId(){return 'abcdefghijk'} function observeAdState(){} function inAd(){return false}
        function enableNativeCaptionBridge(){} function ensureSubtitleFirst(){} function sourceVideo(){return video} function attachVideo(){}
        function cancelAutoEnable(){autoEnableTimer=null} function activate(){activateCalls+=1}
        """ + function + "\nmanageAutoEnable();process.stdout.write(JSON.stringify({activateCalls,continuousPlaybackStartedAt,autoEnableTimer}));"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"activateCalls": 0, "continuousPlaybackStartedAt": 0, "autoEnableTimer": None})

    def test_seek_and_short_pause_reset_the_pre_learning_gate(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("function resetContinuousPlaybackGate")
        end = source.index("\n\n  function stopMonitor", start)
        functions = source[start:end]
        script = """
        let continuousPlaybackStartedAt=5000,autoEnableTimer=1,enabled=false,userGeneration=0,manualSeeks=0,activeInteraction=null,expectedPause=0,expectedPlay=0,subtitleActive=true,session=null;
        const listeners={}; const video={dataset:{},currentTime:3,addEventListener:(name,fn)=>{listeners[name]=fn}};
        function cancelAutoEnable(){autoEnableTimer=null} function finalizeInteraction(){} function renderCaption(){} function focusProgressiveAt(){} function scheduleAutomaticSubtitleRetry(){} function currentVideoId(){return 'abcdefghijk'} function startMonitor(){} function manageAutoEnable(){} function finishSession(){}
        """ + functions + """
        attachVideo(video);
        listeners.seeking(); const afterSeek={continuousPlaybackStartedAt,autoEnableTimer,manualSeeks,userGeneration};
        continuousPlaybackStartedAt=6000;autoEnableTimer=2;listeners.pause();
        process.stdout.write(JSON.stringify({afterSeek,afterPause:{continuousPlaybackStartedAt,autoEnableTimer,manualSeeks,userGeneration}}));
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["afterSeek"], {"continuousPlaybackStartedAt": 0, "autoEnableTimer": None, "manualSeeks": 0, "userGeneration": 0})
        self.assertEqual(output["afterPause"], {"continuousPlaybackStartedAt": 0, "autoEnableTimer": None, "manualSeeks": 0, "userGeneration": 0})

    def test_stale_open_interaction_is_closed_without_familiarity_evidence(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("async function recoverStaleOpenInteraction")
        end = source.index("\n\n  async function activate", start)
        function = source[start:end]
        script = """
        let sent=null;
        async function callWorker(payload){sent=payload;return {session_id:payload.session_id,stage:'watch',open_interaction:null};}
        """ + function + """
        const session={session_id:'a'.repeat(32),stage:'watch',owner_epoch:4,open_interaction:{item_id:'item-1',interaction_id:'b'.repeat(32)}};
        Promise.all([recoverStaleOpenInteraction(session),recoverStaleOpenInteraction({...session,open_interaction:null})]).then(([recovered,unchanged])=>process.stdout.write(JSON.stringify({sent,recovered,unchangedSame:unchanged.open_interaction===null})));
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["sent"]["type"], "interactionComplete")
        self.assertEqual(payload["sent"]["outcome"], "technical_failure")
        self.assertEqual(payload["sent"]["failure_reason"], "recovered_stale_interaction")
        self.assertFalse(payload["sent"]["phrase_confirmed"])
        self.assertNotIn("familiarity_feedback", payload["sent"])
        self.assertIsNone(payload["recovered"]["open_interaction"])
        self.assertTrue(payload["unchangedSame"])

    def test_ad_transition_does_not_consume_the_only_subtitle_retry(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("function retryableSubtitleError")
        end = source.index("\n\n  function attachVideo", start)
        function = source[start:end]
        script = """
        let autoMode=true,subtitleActive=false,subtitleState='failed',lastSubtitleErrorCode='youtube_caption_tracks_missing',automaticSubtitleRetryVideoId=null,automaticSubtitleRetryAttempts=0,adActive=false,retries=0;
        const video={paused:false};
        function currentVideoId(){return 'abcdefghijk'} function inAd(){return adActive} function retrySubtitles(){retries+=1}
        """ + function + """
        scheduleAutomaticSubtitleRetry(video,'abcdefghijk');
        const definitiveFailure={token:automaticSubtitleRetryVideoId,attempts:automaticSubtitleRetryAttempts,retries};
        scheduleAutomaticSubtitleRetry(video,'abcdefghijk',{fromPlayEvent:true});
        adActive=true;
        setTimeout(()=>{
          const duringAd={token:automaticSubtitleRetryVideoId,attempts:automaticSubtitleRetryAttempts,retries};
          adActive=false;
          scheduleAutomaticSubtitleRetry(video,'abcdefghijk',{fromPlayEvent:true});
          setTimeout(()=>process.stdout.write(JSON.stringify({definitiveFailure,duringAd,afterContent:{token:automaticSubtitleRetryVideoId,attempts:automaticSubtitleRetryAttempts,retries}})),350);
        },350);
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["definitiveFailure"], {"token": None, "attempts": 0, "retries": 0})
        self.assertEqual(payload["duringAd"], {"token": None, "attempts": 0, "retries": 0})
        self.assertEqual(payload["afterContent"]["attempts"], 1)
        self.assertEqual(payload["afterContent"]["retries"], 1)
        self.assertIsNone(payload["afterContent"]["token"])

    def test_stopping_learning_restarts_a_stale_inflight_subtitle_task(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("async function stopLearning")
        end = source.index("\n\n  async function disable", start)
        function = source[start:end]
        script = """
        let subtitleTask=Promise.resolve('stale'),subtitleTaskVideoId='abcdefghijk',subtitleActive=false,subtitleState='loading',statusError=true;
        let routeGeneration=0,importJob=null,preparing=false,activeInteraction=null,progressTimer=null,progressiveSyncTimer=null,focusEpoch=0,lastProgressiveFocusSec=null,enabled=false,session=null,continuousPlaybackStartedAt=0,suppressLearningUntilMediaTime=null,autoMode=true,videoId='abcdefghijk';
        let restartCalls=0;
        const audioCache=new Map();
        function cancelAutoEnable(){} async function callWorker(){} async function finalizeInteraction(){} async function saveProgress(){}
        function clearInterval(){} function hideOverlay(){} function stopMonitor(){} function startMonitor(){} function setMessage(){} function readyMessage(){return 'ready'} function waitingMessage(){return 'waiting'} function renderStatus(){} function publicState(){return {}}
        function ensureSubtitleFirst(){restartCalls+=1;subtitleState='loading';return Promise.resolve();}
        """ + function + """
        stopLearning().then(()=>process.stdout.write(JSON.stringify({subtitleTask,subtitleTaskVideoId,subtitleState,restartCalls,routeGeneration})));
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["subtitleTask"])
        self.assertIsNone(payload["subtitleTaskVideoId"])
        self.assertEqual(payload["subtitleState"], "loading")
        self.assertEqual(payload["restartCalls"], 1)
        self.assertEqual(payload["routeGeneration"], 1)

    def test_page_caption_alignment_never_concatenates_neighboring_chinese_rows(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("function pageCaptionOverlap")
        end = source.index("\n\n  function applySubtitlePreview", start)
        functions = source[start:end]
        script = functions + """
        const aligned = mergePageCaptionTracks(
          [{start:0,end:3,text:'English one'},{start:3,end:6,text:'English two'}],
          [{start:0,end:3,text:'中文一'},{start:1.5,end:5.8,text:'中文二'}]
        );
        const fallback = mergePageCaptionTracks(
          [{start:10,end:12,text:'English current'}],
          [{start:8,end:10.8,text:'上一句'},{start:10.1,end:12.1,text:'当前句'}]
        );
        const unequal = mergePageCaptionTracks(
          [{start:0,end:3,text:'English first'},{start:3,end:6,text:'English current'}],
          [{start:0,end:1.5,text:'第一句前段'},{start:2,end:2.9,text:'上一句尾巴'},{start:3,end:6,text:'当前中文'}]
        );
        process.stdout.write(JSON.stringify({aligned,fallback,unequal}));
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([row["text_zh"] for row in payload["aligned"]], ["中文一", "中文二"])
        self.assertEqual(payload["fallback"][0]["text_zh"], "当前句")
        self.assertEqual(payload["unequal"][1]["text_zh"], "当前中文")
        self.assertNotIn("上一句当前句", json.dumps(payload, ensure_ascii=False))

    def test_page_caption_track_parser_publishes_timed_rows(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("function parsePageCaptionTrack")
        end = source.index("\n\n  function pageCaptionOverlap", start)
        function_source = source[start:end]
        payload = {
            "events": [
                {"tStartMs": 1000, "dDurationMs": 1200, "segs": [{"utf8": "Hello world"}]},
                {"tStartMs": 2300, "dDurationMs": 1000, "segs": [{"utf8": "Next line"}]},
            ]
        }
        script = f"{function_source}\nconst normal=parsePageCaptionTrack({json.dumps(payload)},10,false,'en');const oversized=parsePageCaptionTrack({{events:Array.from({{length:50001}},()=>({{tStartMs:0,dDurationMs:1,segs:[{{utf8:'a'}}]}}))}},10,false,'en');process.stdout.write(JSON.stringify({{normal,oversized}}));"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        rows = output["normal"]
        self.assertEqual([(row["start"], row["end"], row["text"]) for row in rows], [(1, 2.2, "Hello world"), (2.3, 3.3, "Next line")])
        self.assertEqual(output["oversized"], [])

    def test_content_script_merges_rolling_caption_fragments(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("function mergeCaptionFragments")
        end = source.index("\n\n  function captionAt", start)
        function_source = source[start:end]
        script = function_source + "\nprocess.stdout.write(JSON.stringify([mergeCaptionFragments([{start:0,end:1,text:'。 每一次'},{start:1,end:2,text:'被拒绝'},{start:2,end:3,text:'都让你更近一步'}]),mergeCaptionFragments([{start:0,end:1,text:'[音乐]'},{start:1,end:2,text:'韩语里有个词'}])]));"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["每一次被拒绝都让你更近一步", "[音乐] 韩语里有个词"])

    def test_python_and_content_script_context_fingerprints_match(self):
        from lexical_sense import context_fingerprint

        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        start = source.index("function contextFingerprint")
        end = source.index("\n\n  function lexicalEntry", start)
        function_source = source[start:end]
        samples = [
            "They sat by the bank and watched the river.",
            "  She   called the BANK about her account. ",
            "[Music] 韩语里有个词",
        ]
        script = f"{function_source}\nprocess.stdout.write(JSON.stringify({json.dumps(samples, ensure_ascii=False)}.map(contextFingerprint)));"
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [context_fingerprint(value) for value in samples])

    def test_popup_calls_the_content_script_instead_of_copying_learning_state(self):
        html = (EXTENSION / "popup.html").read_text(encoding="utf-8")
        script = (EXTENSION / "popup.js").read_text(encoding="utf-8")
        self.assertIn("介入频率", html)
        self.assertIn("自动用于英语视频", html)
        self.assertIn("先加载中英字幕；本机学习服务连接后，连续观看 8 秒才准备学习", html)
        self.assertIn("本机学习库", html)
        self.assertIn("learningSettings", html)
        self.assertIn("字幕与学习卡大小", html)
        self.assertIn("只控制暂停密度；候选不足时会少做", html)
        self.assertNotIn('"inflow:activate"', script)
        self.assertIn('"开启本视频"', script)
        self.assertIn('"关闭本视频"', script)
        self.assertIn("inflow:retrySubtitles", script)
        self.assertIn("inflow:takeoverLearning", script)
        self.assertIn("autoLearning: false", script)
        self.assertIn("stored.autoLearning === true", script)
        self.assertIn("允许自动教学暂停", html)
        self.assertIn("默认关闭", html)
        self.assertIn("chrome.permissions.request", script)
        self.assertIn("LOCAL_SERVICE_PERMISSION", script)
        self.assertIn("未授予本机服务权限；字幕保持可用", script)
        self.assertIn("字幕可用 · 学习服务未连接", script)
        self.assertIn("autoMode", script)
        self.assertIn("displaySize", script)
        self.assertIn("setFrequency", script)
        self.assertNotIn("interactions/start", script)


if __name__ == "__main__":
    unittest.main()
