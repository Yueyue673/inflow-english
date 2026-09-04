from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ExtensionContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_v3_has_stable_id_and_minimal_permissions(self):
        self.assertEqual(self.manifest["manifest_version"], 3)
        self.assertEqual(self.manifest["version"], "0.2.4")
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
        self.assertEqual(self.manifest["content_scripts"][0]["run_at"], "document_start")
        forbidden = {"cookies", "downloads", "webRequest", "nativeMessaging", "clipboardRead", "clipboardWrite", "<all_urls>"}
        self.assertFalse(forbidden.intersection(self.manifest["permissions"] + self.manifest["host_permissions"]))

    def test_manifest_references_real_files_and_all_scripts_parse(self):
        paths = [
            self.manifest["background"]["service_worker"],
            self.manifest["action"]["default_popup"],
            self.manifest["content_scripts"][0]["js"][0],
            *self.manifest["icons"].values(),
            *self.manifest["action"]["default_icon"].values(),
            "popup.js",
            "popup.css",
            "page-bridge.js",
        ]
        self.assertTrue(all((EXTENSION / path).is_file() for path in paths))
        for script in ("service-worker.js", "popup.js", "content-script.js", "page-bridge.js"):
            result = subprocess.run(
                ["node", "--check", str(EXTENSION / script)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

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
            "lexiconFeedback",
            "contextFingerprint",
            "context_fingerprints",
            "140ms",
            "yt-navigate-finish",
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
        self.assertIn('credentials: "omit"', bridge)
        self.assertIn('redirect: "error"', bridge)
        self.assertIn("rawUrl.length > 8192", bridge)
        self.assertIn("tracks.slice(0, 100)", bridge)
        self.assertIn("response.body.getReader()", bridge)
        self.assertIn("total > 5_000_000", bridge)
        self.assertIn("setTimeout(() => controller.abort(), 3200)", bridge)
        self.assertNotIn("chrome.cookies", bridge)
        harness = """
        global.sent = null;
        global.window = { postMessage: (payload) => { global.sent = payload; } };
        global.location = { origin: 'https://www.youtube.com' };
        const currentScript = { dataset: { inflowNonce: '12345678-1234-1234-1234-123456789abc', inflowVideoId: 'abcdefghijk' }, remove() {} };
        const response = { videoDetails: { videoId: 'abcdefghijk', title: 'Fixture', lengthSeconds: '60' }, captions: { playerCaptionsTracklistRenderer: { captionTracks: [{ baseUrl: 'https://www.youtube.com/api/timedtext?v=abcdefghijk&lang=en', languageCode: 'en', isTranslatable: true }] } } };
        global.document = { currentScript, querySelector: (selector) => selector === '#movie_player' ? { getPlayerResponse: () => response } : { duration: 60 } };
        const captionBytes = new TextEncoder().encode(JSON.stringify({ events: [{ tStartMs: 0, dDurationMs: 1000, segs: [{ utf8: 'Hello' }] }] }));
        global.fetch = async () => ({
          ok: true,
          status: 200,
          headers: { get: name => name === 'content-length' ? String(captionBytes.byteLength) : null },
          body: { getReader: () => { let sent = false; return { read: async () => sent ? { done: true } : (sent = true, { done: false, value: captionBytes }) }; } },
        });
        """ + bridge + "\nsetTimeout(() => process.stdout.write(JSON.stringify(global.sent)), 30);"
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=5, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["video_id"], "abcdefghijk")
        self.assertEqual(len(payload["english"]["events"]), 1)
        self.assertEqual(payload["source"], "inflow-page-caption-bridge")
        self.assertEqual(payload["caption_source"], "youtube_page_player")

    def test_startup_status_is_visible_without_hover(self):
        source = (EXTENSION / "content-script.js").read_text(encoding="utf-8")
        self.assertIn("min-width:118px", source)
        self.assertIn("min-height:32px", source)
        self.assertIn("top:calc(var(--video-top,0px) + 12px)", source)
        self.assertNotIn("width:10px", source)
        self.assertNotIn("font-size:0", source)
        self.assertNotIn("color:transparent", source)

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
        script = f"{function_source}\nprocess.stdout.write(JSON.stringify(parsePageCaptionTrack({json.dumps(payload)},10,false,'en')));"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual([(row["start"], row["end"], row["text"]) for row in rows], [(1, 2.2, "Hello world"), (2.3, 3.3, "Next line")])

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
        self.assertIn("inflow:activate", script)
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
