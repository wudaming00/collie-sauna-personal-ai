"""Real-browser regression for the app-global Collie command/voice capsule.

Run through the isolated mock-server harness:

    python tests/browser_suite.py capsule_ui_check
"""

import os
import json
import sys
import time
from urllib.parse import parse_qs, urlparse


WEB = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8795")
RESULTS = []


def check(name, condition, detail=""):
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(("  PASS " if ok else "  FAIL ") + name + ((" :: " + detail) if detail and not ok else ""))


def wait_for(page, predicate, timeout=5.0):
    """Poll from Python so the page's strict CSP never needs unsafe-eval."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(25)
    return False


def open_presented_native_capsule(page, request_id):
    """Drive one exact-id desktop open through prepare into the final voice phase."""
    speech_starts = page.evaluate("window.__speechStarts")
    page.evaluate(
        "window.__webviewEmit({type:'collie-command',action:'open',voice:true,"
        f"host:'command',request_id:{request_id}}})"
    )
    page.wait_for_selector("#capsuleLayer", state="visible")
    page.evaluate(
        f"window.__webviewEmit({{type:'collie-command-prepare',request_id:{request_id}}})"
    )
    prepared = wait_for(page, lambda: page.evaluate(
        "window.__nativeCapsuleStates.some(x => x.type === 'collie-command-prepared-state' && "
        f"x.request_id === {request_id})"))
    page.evaluate(
        f"window.__webviewEmit({{type:'collie-command-presented',request_id:{request_id}}})"
    )
    started = wait_for(page, lambda: page.evaluate("window.__speechStarts") == speech_starts + 1)
    return prepared and started


BASE_INIT = r"""
(() => {
  sessionStorage.setItem('collie-name-onboard-skip', '1');
  sessionStorage.setItem('collie-onboard-skip', '1');
  window.__capsuleStreams = [];
  window.__capsuleEventSources = [];
  window.__speechStarts = 0;
  window.__speechStops = 0;
  window.__speechAborts = 0;
  window.__speechInstances = [];
  window.__capsuleDirectRequests = [];
  window.__capsuleMusicIntent = {music:false, action:'agent', query:''};
  window.__capsuleMusicPlay = null;
  window.__capsuleMusicStop = null;
  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = String(input && input.url || input || '');
    const directReply = payload => Promise.resolve(new Response(JSON.stringify(payload), {
      status:200, headers:{'content-type':'application/json'}
    }));
    if (url.includes('/api/desktop/music-intent')) {
      window.__capsuleDirectRequests.push({kind:'intent', body:JSON.parse(init.body || '{}')});
      return directReply(window.__capsuleMusicIntent || {music:false, action:'agent', query:''});
    }
    if (url.includes('/api/desktop/play') && window.__capsuleMusicPlay) {
      window.__capsuleDirectRequests.push({kind:'play', body:JSON.parse(init.body || '{}')});
      return directReply(window.__capsuleMusicPlay);
    }
    if (url.includes('/api/desktop/stopaudio') && window.__capsuleMusicStop) {
      window.__capsuleDirectRequests.push({kind:'stop', body:JSON.parse(init.body || '{}')});
      return directReply(window.__capsuleMusicStop);
    }
    return realFetch(input, init);
  };
  window.EventSource = class {
    constructor(url) {
      this.url = url; this.listeners = {}; window.__capsuleStreams.push(url);
      window.__capsuleEventSources.push(this);
    }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    emit(name, data = {}) {
      (this.listeners[name] || []).forEach(callback => callback({ data:JSON.stringify(data) }));
    }
    fail() { if (this.onerror) this.onerror(new Event('error')); }
    close() { this.closed = true; }
  };
})();
"""


WEBVIEW_INIT = r"""
(() => {
  const listeners = [];
  const bridge = {
    addEventListener(name, callback) { if (name === 'message') listeners.push(callback); },
    postMessage(payload) { (window.__nativeCapsuleStates ||= []).push(payload); }
  };
  window.chrome = window.chrome || {};
  try { Object.defineProperty(window.chrome, 'webview', { value: bridge, configurable: true }); }
  catch (_) { window.chrome.webview = bridge; }
  window.__webviewEmit = payload => listeners.forEach(callback => callback({ data: payload }));
})();
"""


SPEECH_INIT = r"""
(() => {
  window.SpeechRecognition = class {
    constructor() { window.__speechInstances.push(this); }
    start() {
      window.__speechStarts += 1;
      this.started = true;
      if (this.onstart) this.onstart();
    }
    emitFinal(transcript = 'voice outcome') {
      const result = [{ transcript }];
      result.isFinal = true;
      if (this.onresult) this.onresult({ resultIndex: 0, results: [result] });
    }
    naturalEnd() {
      this.started = false;
      if (this.onend) this.onend();
    }
    stop() {
      window.__speechStops += 1;
      this.emitFinal();
      this.naturalEnd();
    }
    abort() {
      window.__speechAborts += 1;
      this.aborted = true;
      this.naturalEnd();
    }
  };
})();
"""


NO_SPEECH_INIT = r"""
(() => {
  try { Object.defineProperty(window, 'SpeechRecognition', { value: undefined, configurable: true }); }
  catch (_) { window.SpeechRecognition = undefined; }
  try { Object.defineProperty(window, 'webkitSpeechRecognition', { value: undefined, configurable: true }); }
  catch (_) { window.webkitSpeechRecognition = undefined; }
})();
"""


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 390, "height": 760})
        # A regular browser tab must not pretend to be WebView2: the document owns the shortcut
        # here, while the desktop host exercises its separate RegisterHotKey message path below.
        context.add_init_script(BASE_INIT + SPEECH_INIT)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/route*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"kind":"chat"}'))
        needs_payload = {"items": [{
            "id": "permission:approval-1", "source": "permission",
            "category": "authorization", "session": "", "mission": "", "created_at": 1,
            "data": {"id": "approval-1", "title": "Approve release publication?",
                     "body": "Publish version 1.4.0", "reason": "Publishing changes a public listing.",
                     "impact_summary": "The release becomes visible to customers.",
                     "approve_effect": "This exact release payload is submitted once.",
                     "reject_effect": "The listing stays unchanged.", "actionable": False,
                     "risk": "external write"}
        }], "total": 1, "has_more": False, "next_cursor": ""}
        page.route("**/api/needs-you*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(needs_payload)))
        page.goto(WEB, wait_until="load")
        page.wait_for_selector("#collieSummon")
        token = page.locator('meta[name="collie-token"]').get_attribute("content")
        narrow_probe = page.request.post(
            WEB.rstrip("/") + "/api/desktop/music-intent?token=" + token,
            data={"text": "ordinary non-music probe"}).json()
        check("server exposes a side-effect-free narrow music preflight",
              narrow_probe.get("action") == "agent" and narrow_probe.get("music") is False,
              json.dumps(narrow_probe))

        # The document shortcut is one-key talk: first press opens and listens, second is an
        # unconditional cancel/close escape hatch. It must not overwrite the main composer draft.
        page.fill("#input", "keep this composer draft")
        page.keyboard.press("Control+Shift+Space")
        page.wait_for_selector("#capsuleLayer", state="visible")
        check("document shortcut opens and starts voice automatically",
              page.evaluate("window.__speechStarts") == 1 and
              page.locator("#capsuleMic").get_attribute("aria-pressed") == "true")
        check("background is inert while command is open", page.locator(".app").get_attribute("inert") == "")
        page.keyboard.press("Control+Shift+Space")
        page.wait_for_selector("#capsuleLayer", state="hidden")
        check("second document shortcut cancels voice and closes",
              page.evaluate("window.__speechAborts") == 1 and page.input_value("#input") == "keep this composer draft")
        check("shortcut close removes background inert", page.locator(".app").get_attribute("inert") is None)

        # Focus, shared inert manager, and Escape cancel semantics.
        page.click("#collieSummon")
        page.wait_for_selector("#capsuleLayer", state="visible")
        wait_for(page, lambda: page.evaluate("document.activeElement && document.activeElement.id") == "capsuleInput")
        check("capsule focuses the compact command input",
              page.evaluate("document.activeElement && document.activeElement.id") == "capsuleInput")
        check("background is inert while command is open", page.locator(".app").get_attribute("inert") == "")
        check("capsule does not contain a model chooser", page.locator("#capsuleLayer #modelTrigger").count() == 0)
        page.fill("#capsuleInput", "discard only this capsule edit")
        page.keyboard.press("Escape")
        page.wait_for_selector("#capsuleLayer", state="hidden")
        check("Escape cancels without overwriting the composer",
              page.input_value("#input") == "keep this composer draft")
        check("closing removes background inert", page.locator(".app").get_attribute("inert") is None)

        # The capsule stays bounded and its secondary controls stay touch-sized at the narrow floor.
        page.set_viewport_size({"width": 320, "height": 700})
        page.click("#collieSummon")
        page.wait_for_selector("#capsuleLayer", state="visible")
        capsule_box = page.locator("#collieCapsule").bounding_box()
        mic_box = page.locator("#capsuleMic").bounding_box()
        send_box = page.locator("#capsuleSend").bounding_box()
        close_box = page.locator("#capsuleClose").bounding_box()
        check("capsule fits a 320px viewport", capsule_box and capsule_box["x"] >= 0 and
              capsule_box["x"] + capsule_box["width"] <= 320, str(capsule_box))
        check("voice/send/close remain 44px targets", all(box and box["width"] >= 44 and box["height"] >= 44
              for box in (mic_box, send_box, close_box)), str((mic_box, send_box, close_box)))
        page.click("#capsuleClose")
        check("Collie chrome has no horizontal overflow at 320px",
              page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))

        # The independent Needs You snapshot carries the decision contract, and an unchanged poll
        # does not replace the focused card underneath keyboard users.
        page.set_viewport_size({"width": 390, "height": 760})
        page.click("#sideToggle")
        page.click("#navMore")
        page.click("#needsYouNav")
        page.wait_for_selector("#needsList .needs-card")
        needs_text = page.locator("#needsList .needs-card").inner_text()
        check("Needs You explains reason, impact, approval and refusal outcomes",
              all(text in needs_text for text in (
                  "Publishing changes", "visible to customers", "submitted once", "stays unchanged")),
              needs_text)
        page.locator("#needsList .needs-open").focus()
        page.evaluate("window.__needsFocused = document.activeElement; document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(150)
        check("unchanged Needs You polling preserves card focus and node identity",
              page.evaluate("document.activeElement === window.__needsFocused"))
        page.click("#sideToggle")
        page.click("#navHome")

        # Invalid hidden run setup must stay visible instead of closing and silently losing a send.
        page.evaluate("document.querySelector('#runVerification').value='required'; document.querySelector('#verifyCommand').value=''")
        page.click("#collieSummon")
        page.fill("#capsuleInput", "preflight should remain here")
        page.click("#capsuleSend")
        wait_for(page, lambda: "Run setup needs review" in
                 page.locator("#capsuleVoiceStatus").inner_text())
        check("invalid run setup keeps the capsule open with an actionable status",
              page.is_visible("#capsuleLayer") and "Run setup needs review" in
              page.locator("#capsuleVoiceStatus").inner_text())
        page.click("#capsuleClose")
        page.evaluate("document.querySelector('#runVerification').value='auto'")

        page.click("#collieSummon")
        page.dispatch_event("#capsuleMic", "pointerdown", {"button": 0, "pointerId": 2})
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        check("losing window focus aborts microphone capture",
              page.locator("#capsuleMic").get_attribute("aria-pressed") == "false" and
              "stopped" in page.locator("#capsuleVoiceStatus").inner_text().lower())
        page.click("#capsuleClose")

        # The explicit close button is also a hard cancel while hands-free recognition is active.
        aborts_before_close = page.evaluate("window.__speechAborts")
        page.keyboard.press("Control+Shift+Space")
        page.wait_for_selector("#capsuleLayer", state="visible")
        page.click("#capsuleClose")
        check("explicit close cancels recognition and hides the capsule",
              page.is_hidden("#capsuleLayer") and
              page.evaluate("window.__speechAborts") == aborts_before_close + 1)

        # Hold/release recognition appends its transcript and hands it to the exact normal send path.
        page.fill("#input", "")
        page.click("#collieSummon")
        page.dispatch_event("#capsuleMic", "pointerdown", {"button": 0, "pointerId": 1})
        check("hold-to-talk exposes listening state",
              page.locator("#capsuleMic").get_attribute("aria-pressed") == "true")
        page.dispatch_event("#capsuleMic", "pointerup", {"button": 0, "pointerId": 1})
        wait_for(page, lambda: any(url.startswith("/api/stream?")
                                  for url in page.evaluate("window.__capsuleStreams")))
        stream_url = next(url for url in page.evaluate("window.__capsuleStreams")
                          if url.startswith("/api/stream?"))
        stream_params = parse_qs(urlparse(stream_url).query)
        sent = stream_params.get("q", [""])[0]
        check("release sends recognized text through the existing stream", sent == "voice outcome", stream_url)
        check("capsule marks only this shared stream with its request-local entrypoint",
              stream_params.get("entrypoint") == ["capsule"], stream_url)
        check("voice transcript remains visible while Collie starts",
              page.is_visible("#capsuleLayer") and page.is_visible("#capsuleResult") and
              page.locator("#capsuleHeardText").inner_text() == "voice outcome")
        page.evaluate("window.__capsuleEventSources.at(-1).emit('start',"
                      "{session:'voice-session',run:'voice-run',model:'test-model'})")
        page.evaluate("window.__capsuleEventSources.at(-1).emit('token',{t:'A natural '});"
                      "window.__capsuleEventSources.at(-1).emit('token',{t:'answer.'})")
        check("the existing stream renders Collie's answer inside the capsule",
              page.locator("#capsuleReplyText").inner_text() == "A natural answer." and
              page.locator("#capsuleReplyText").get_attribute("role") == "document" and
              page.locator("#capsuleReplyText").get_attribute("aria-live") is None)
        page.evaluate("window.__capsuleEventSources.at(-1).emit('done',"
                      "{session:'voice-session',run:'voice-run',answer:'A natural answer.',"
                      "personal_state:{task:null,suggestion:null,activities:['voice outcome']}})")
        check("completed answer stays visible until explicit close",
              page.is_visible("#capsuleLayer") and
              "Answer ready" in page.locator("#capsuleVoiceStatus").inner_text())
        check("ordinary chat does not echo its prompt in a Done card",
              page.locator(".ps-exec-card:not(.ps-cloud-card)").count() == 0)
        followup_streams_before = len(page.evaluate("window.__capsuleStreams"))
        check("a completed turn keeps a visible composer for follow-up instructions",
              page.is_visible("#capsuleInput") and not page.is_disabled("#capsuleInput") and
              "follow-up" in (page.get_attribute("#capsuleInput", "placeholder") or "").lower() and
              "capsule-followup" in (page.get_attribute("#collieCapsule", "class") or ""))
        page.fill("#capsuleInput", "Continue with one more detail")
        page.click("#capsuleSend")
        wait_for(page, lambda: len(page.evaluate("window.__capsuleStreams")) == followup_streams_before + 1)
        followup_url = page.evaluate("window.__capsuleStreams.at(-1)")
        check("follow-up uses the same capsule send path instead of requiring a new window",
              parse_qs(urlparse(followup_url).query).get("q", [""])[0] ==
              "Continue with one more detail" and
              page.locator("#capsuleHeardText").inner_text() == "Continue with one more detail",
              followup_url)
        page.evaluate("window.__capsuleEventSources.at(-1).emit('done',"
                      "{session:'voice-session',run:'followup-run',answer:'One more detail.'})")
        page.click("#capsuleClose")
        check("explicit close acknowledges and hides the completed answer", page.is_hidden("#capsuleLayer"))
        check("capsule interactions produce no page errors", not errors, "; ".join(errors))

        # Music is a narrow direct action, not a reason to grant the agent broad desktop control.
        # The capsule classifies it, plays it on this computer, and offers an immediate stop button;
        # ordinary chat/stream routing must never start for the same utterance.
        page.evaluate("""() => {
          window.__capsuleMusicIntent = {music:true, action:'music', query:'轻音乐', artist:'', title:'', duration_seconds:30};
          window.__capsuleMusicPlay = {ok:true, title:'Quiet Test Track', display_title:'轻音乐', uploader:'Test Artist',
            clip_seconds:30,
            stoppable:true, menubar:false,
            answer:'▶ Playing Quiet Test Track — Test Artist. Say “stop the music”, or use the stop button in Collie.'};
          window.__capsuleMusicStop = {ok:true};
          window.__musicRequestStart = window.__capsuleDirectRequests.length;
        }""")
        music_streams_before = len(page.evaluate("window.__capsuleStreams"))
        page.click("#collieSummon")
        page.fill("#capsuleInput", "给我播放一小段音乐")
        page.click("#capsuleSend")
        wait_for(page, lambda: "正在播放" in page.locator("#capsuleVoiceStatus").inner_text())
        music_requests = page.evaluate("window.__capsuleDirectRequests.slice(window.__musicRequestStart)")
        music_reply = page.locator("#capsuleReplyText").inner_text()
        check("capsule music bypasses the model stream and starts the narrow player",
              [item["kind"] for item in music_requests] == ["intent", "play"] and
              music_requests[0]["body"]["text"] == "给我播放一小段音乐" and
              music_requests[1]["body"]["q"] == "轻音乐" and
              music_requests[1]["body"]["duration_seconds"] == 30 and
              len(page.evaluate("window.__capsuleStreams")) == music_streams_before,
              json.dumps(music_requests, ensure_ascii=False))
        check("playing state names the track and exposes a stop control without a permission prompt",
              "轻音乐" in music_reply and "30 秒后自动停止" in music_reply and
              "desktop control" not in music_reply.lower() and
              page.is_visible(".capsule-music-actions button") and
              page.locator(".capsule-music-actions button").inner_text() == "停止",
              music_reply)
        page.click(".capsule-music-actions button")
        wait_for(page, lambda: "音乐已停止" in page.locator("#capsuleVoiceStatus").inner_text())
        check("capsule stop control stops the same direct playback",
              page.locator("#capsuleReplyText").inner_text() == "音乐已停止。" and
              page.evaluate("window.__capsuleDirectRequests.at(-1).kind") == "stop")
        page.click("#capsuleClose")
        page.evaluate("window.__capsuleMusicIntent={music:false,action:'agent',query:''};"
                      "window.__capsuleMusicPlay=null;window.__capsuleMusicStop=null")

        # Silence is an actionable input state, not a reason for the surface to disappear.
        streams_before_silence = len(page.evaluate("window.__capsuleStreams"))
        page.keyboard.press("Control+Shift+Space")
        page.wait_for_selector("#capsuleLayer", state="visible")
        page.evaluate("window.__speechInstances.at(-1).naturalEnd()")
        check("no speech keeps the capsule open with a useful retry prompt",
              page.is_visible("#capsuleLayer") and
              "No speech heard" in page.locator("#capsuleVoiceStatus").inner_text() and
              len(page.evaluate("window.__capsuleStreams")) == streams_before_silence)
        page.click("#capsuleClose")

        # Hands-free voice submits on the recognizer's natural endpoint: no second key is needed.
        auto = context.new_page()
        auto_errors = []
        auto.on("pageerror", lambda error: auto_errors.append(str(error)))
        auto.route("**/api/route*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"kind":"chat"}'))
        auto.goto(WEB, wait_until="load")
        auto.wait_for_selector("#collieSummon")
        auto.keyboard.press("Control+Shift+Space")
        auto.wait_for_selector("#capsuleLayer", state="visible")
        auto_aborts_before_result = auto.evaluate("window.__speechAborts")
        auto.evaluate("window.__speechInstances.at(-1).emitFinal('natural voice outcome')")
        check("recognizer transcript updates do not cancel their own voice session",
              auto.input_value("#capsuleInput") == "natural voice outcome" and
              auto.locator("#capsuleMic").get_attribute("aria-pressed") == "true" and
              auto.evaluate("window.__speechAborts") == auto_aborts_before_result)
        auto.evaluate("window.__speechInstances.at(-1).naturalEnd()")
        wait_for(auto, lambda: any(url.startswith("/api/stream?")
                                   for url in auto.evaluate("window.__capsuleStreams")))
        auto_stream = next(url for url in auto.evaluate("window.__capsuleStreams")
                           if url.startswith("/api/stream?"))
        auto_sent = parse_qs(urlparse(auto_stream).query).get("q", [""])[0]
        check("natural speech end auto-sends through the shared stream",
              auto_sent == "natural voice outcome", auto_stream)
        check("natural auto-send keeps the transcript and progress visible",
              auto.is_visible("#capsuleLayer") and auto.is_visible("#capsuleResult") and
              auto.locator("#capsuleHeardText").inner_text() == "natural voice outcome" and
              not auto_errors, "; ".join(auto_errors))
        auto.click("#capsuleClose")
        auto.close()

        # A person can begin typing while hands-free recognition is still active. Browser-native
        # beforeinput must retire that recognizer before the edit lands so a late transcript cannot
        # replace, submit, or close the typed request. Exercise real keyboard, paste, delete, and
        # Chromium IME events here; synthetic InputEvents are deliberately untrusted and insufficient.
        race_context = browser.new_context(viewport={"width": 420, "height": 190})
        race_context.add_init_script(BASE_INIT + WEBVIEW_INIT + SPEECH_INIT)
        race = race_context.new_page()
        race_errors = []
        race.on("pageerror", lambda error: race_errors.append(str(error)))
        race.route("**/api/route*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"kind":"chat"}'))
        race.goto(WEB.rstrip("/") + "/?capsule=1", wait_until="load")
        race.wait_for_selector("#capsuleLayer", state="hidden")
        race.evaluate("window.__manualBeforeInputs=[];"
                      "document.getElementById('capsuleInput').addEventListener('beforeinput',e=>"
                      "window.__manualBeforeInputs.push({trusted:e.isTrusted,type:e.inputType,"
                      "composing:e.isComposing}),true)")

        check("manual-input race host reaches active hands-free recognition",
              open_presented_native_capsule(race, 1))
        race.keyboard.insert_text("focus-probe")
        typed_instance = race.evaluate("window.__speechInstances.length - 1")
        typed_state = race.evaluate("({value:document.getElementById('capsuleInput').value,"
                                    "aborts:window.__speechAborts,"
                                    "pressed:document.getElementById('capsuleMic').getAttribute('aria-pressed'),"
                                    "status:document.getElementById('capsuleVoiceStatus').textContent,"
                                    "events:window.__manualBeforeInputs.slice()})")
        check("trusted typing stops voice before preserving the complete manual request",
              typed_state["value"] == "focus-probe" and typed_state["aborts"] == 1 and
              typed_state["pressed"] == "false" and "typing" in typed_state["status"].lower() and
              any(item["trusted"] and item["type"] == "insertText"
                  for item in typed_state["events"]), json.dumps(typed_state, ensure_ascii=False))
        race.evaluate(f"window.__speechInstances[{typed_instance}].emitFinal('late overwrite');"
                      f"window.__speechInstances[{typed_instance}].naturalEnd()")
        race.wait_for_timeout(25)
        check("late speech result and end cannot overwrite, submit, or close manual typing",
              race.input_value("#capsuleInput") == "focus-probe" and
              race.is_visible("#capsuleLayer") and
              not any(url.startswith("/api/stream?") for url in race.evaluate("window.__capsuleStreams")))
        race.evaluate("window.__webviewEmit({type:'collie-command',action:'close',voice:true,"
                      "host:'command',request_id:2})")
        race.wait_for_selector("#capsuleLayer", state="hidden")

        race.evaluate("window.__manualBeforeInputs=[]")
        check("delete race reaches active hands-free recognition",
              open_presented_native_capsule(race, 3))
        race.evaluate("const el=document.getElementById('capsuleInput');el.value='delete-me';"
                      "el.setSelectionRange(el.value.length,el.value.length)")
        race.keyboard.press("Backspace")
        delete_state = race.evaluate("({value:document.getElementById('capsuleInput').value,"
                                     "events:window.__manualBeforeInputs.slice(),"
                                     "aborts:window.__speechAborts})")
        check("trusted delete stops voice and applies the deletion normally",
              delete_state["value"] == "delete-m" and delete_state["aborts"] == 2 and
              any(item["trusted"] and item["type"] == "deleteContentBackward"
                  for item in delete_state["events"]), json.dumps(delete_state, ensure_ascii=False))
        race.evaluate("window.__webviewEmit({type:'collie-command',action:'close',voice:true,"
                      "host:'command',request_id:4});document.getElementById('input').value=''")
        race.wait_for_selector("#capsuleLayer", state="hidden")

        check("paste race reaches active hands-free recognition",
              open_presented_native_capsule(race, 5))
        paste_supported = True
        try:
            race_context.grant_permissions(["clipboard-read", "clipboard-write"], origin=WEB)
            race.evaluate("navigator.clipboard.writeText('paste-probe')")
            race.evaluate("window.__manualBeforeInputs=[]")
            race.keyboard.press("Control+V")
            race.evaluate("navigator.clipboard.writeText('')")
        except Exception:
            paste_supported = False
        paste_state = race.evaluate("({value:document.getElementById('capsuleInput').value,"
                                    "events:window.__manualBeforeInputs.slice(),"
                                    "aborts:window.__speechAborts})")
        check("trusted paste stops voice and preserves the pasted request",
              paste_supported and paste_state["value"] == "paste-probe" and
              paste_state["aborts"] == 3 and
              any(item["trusted"] and item["type"] == "insertFromPaste"
                  for item in paste_state["events"]), json.dumps(paste_state, ensure_ascii=False))
        race.evaluate("window.__webviewEmit({type:'collie-command',action:'close',voice:true,"
                      "host:'command',request_id:6})")
        race.wait_for_selector("#capsuleLayer", state="hidden")

        race.evaluate("window.__manualBeforeInputs=[]")
        check("IME race reaches active hands-free recognition",
              open_presented_native_capsule(race, 7))
        cdp = race_context.new_cdp_session(race)
        cdp.send("Input.imeSetComposition", {
            "text": "你好", "selectionStart": 2, "selectionEnd": 2,
        })
        cdp.send("Input.insertText", {"text": "你好"})
        ime_instance = race.evaluate("window.__speechInstances.length - 1")
        ime_state = race.evaluate("({value:document.getElementById('capsuleInput').value,"
                                  "events:window.__manualBeforeInputs.slice(),"
                                  "aborts:window.__speechAborts})")
        race.evaluate(f"window.__speechInstances[{ime_instance}].emitFinal('迟到语音');"
                      f"window.__speechInstances[{ime_instance}].naturalEnd()")
        check("trusted IME composition stops voice without corrupting composed text",
              ime_state["value"] == "你好" and race.input_value("#capsuleInput") == "你好" and
              ime_state["aborts"] == 4 and
              any(item["trusted"] and item["type"] == "insertCompositionText"
                  for item in ime_state["events"]) and
              race.is_visible("#capsuleLayer") and
              not any(url.startswith("/api/stream?") for url in race.evaluate("window.__capsuleStreams")),
              json.dumps(ime_state, ensure_ascii=False))
        check("manual-input voice races produce no page errors",
              not race_errors, "; ".join(race_errors))
        race_context.close()

        # Microsoft Pinyin/Windows TSF may confirm a candidate with an Enter keydown whose
        # isComposing flag is already false but whose legacy keyCode/which is 229. Exercise the
        # actual Chromium event listener at both sides of compositionend, then prove an ordinary
        # Enter still sends exactly once.
        ime_key_context = browser.new_context(viewport={"width": 420, "height": 240})
        ime_key_context.add_init_script(BASE_INIT + NO_SPEECH_INIT)
        ime_keys = ime_key_context.new_page()
        ime_key_errors = []
        ime_keys.on("pageerror", lambda error: ime_key_errors.append(str(error)))
        ime_keys.route("**/api/route*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"kind":"chat"}'))
        ime_keys.goto(WEB, wait_until="load")
        ime_keys.click("#collieSummon")
        ime_keys.fill("#capsuleInput", "候选词")
        ime_keys.evaluate("""() => {
          const input = document.getElementById('capsuleInput');
          window.__dispatchImeEnter = (keyCode, which) => {
            const event = new KeyboardEvent('keydown', {
              key:'Enter', code:'Enter', bubbles:true, cancelable:true, isComposing:false
            });
            Object.defineProperties(event, {
              keyCode:{value:keyCode, configurable:true},
              which:{value:which, configurable:true},
              isComposing:{value:false, configurable:true}
            });
            input.dispatchEvent(event);
            return event.defaultPrevented;
          };
          input.dispatchEvent(new CompositionEvent('compositionstart', {
            bubbles:true, cancelable:true, data:'候'
          }));
        }""")
        streams_before_ime_enter = len(ime_keys.evaluate("window.__capsuleStreams"))
        active_composition_prevented = ime_keys.evaluate("window.__dispatchImeEnter(13,13)")
        check("explicit composition state ignores Enter even when isComposing is false",
              not active_composition_prevented and ime_keys.is_visible("#capsuleLayer") and
              len(ime_keys.evaluate("window.__capsuleStreams")) == streams_before_ime_enter)
        ime_keys.evaluate("document.getElementById('capsuleInput').dispatchEvent("
                          "new CompositionEvent('compositionend',{bubbles:true,data:'候选词'}))")
        keycode_229_prevented = ime_keys.evaluate("window.__dispatchImeEnter(229,13)")
        which_229_prevented = ime_keys.evaluate("window.__dispatchImeEnter(13,229)")
        check("post-composition keyCode or which 229 cannot submit a Pinyin candidate",
              not keycode_229_prevented and not which_229_prevented and
              ime_keys.is_visible("#capsuleLayer") and
              len(ime_keys.evaluate("window.__capsuleStreams")) == streams_before_ime_enter)
        ordinary_enter_prevented = ime_keys.evaluate("window.__dispatchImeEnter(13,13)")
        wait_for(ime_keys, lambda: len(ime_keys.evaluate(
            "window.__capsuleStreams")) == streams_before_ime_enter + 1)
        check("ordinary Enter after composition still sends exactly once",
              ordinary_enter_prevented and ime_keys.is_visible("#capsuleLayer") and
              ime_keys.locator("#capsuleHeardText").inner_text() == "候选词" and
              len(ime_keys.evaluate("window.__capsuleStreams")) == streams_before_ime_enter + 1)
        check("IME Enter boundary produces no page errors",
              not ime_key_errors, "; ".join(ime_key_errors))
        ime_key_context.close()

        # A rejected route keeps both the exact transcript and the server's useful reason. It must
        # not collapse back to a generic composer state that looks like nothing happened.
        failure_context = browser.new_context(viewport={"width": 520, "height": 360})
        failure_context.add_init_script(BASE_INIT + NO_SPEECH_INIT)
        failed_page = failure_context.new_page()
        failed_errors = []
        failed_page.on("pageerror", lambda error: failed_errors.append(str(error)))
        failed_page.route("**/api/route*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"error": "model_unavailable",
                             "detail": "Provider overloaded; retry in 30 seconds."})))
        failed_page.goto(WEB, wait_until="load")
        failed_page.click("#collieSummon")
        failed_page.fill("#capsuleInput", "preserve this exact request")
        failed_page.click("#capsuleSend")
        wait_for(failed_page, lambda: "Provider overloaded" in
                 failed_page.locator("#capsuleReplyText").inner_text())
        check("model rejection preserves transcript and precise safe reason",
              failed_page.is_visible("#capsuleLayer") and
              failed_page.is_visible("#capsuleResult") and
              failed_page.locator("#capsuleHeardText").inner_text() == "preserve this exact request" and
              "Provider overloaded; retry in 30 seconds." in
              failed_page.locator("#capsuleReplyText").inner_text() and
              not failed_errors, "; ".join(failed_errors))
        failed_page.click("#capsuleClose")
        failed_page.click("#collieSummon")
        check("closing a terminal failure explicitly returns the next summon to a fresh draft",
              failed_page.is_hidden("#capsuleResult") and not failed_page.input_value("#capsuleInput"))
        failure_context.close()

        # Provider retries are internal recovery, not three user-facing answer lines. Even an old
        # server that still emits its parser marker is filtered, and the final terminal error is
        # collapsed into one actionable failure card whose retry resubmits the exact request once.
        stream_failure_context = browser.new_context(viewport={"width": 660, "height": 360})
        stream_failure_context.add_init_script(BASE_INIT + NO_SPEECH_INIT)
        stream_failure_page = stream_failure_context.new_page()
        stream_failure_errors = []
        stream_failure_page.on("pageerror", lambda error: stream_failure_errors.append(str(error)))
        stream_failure_page.route("**/api/route*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"kind":"chat"}'))
        stream_failure_page.goto(WEB, wait_until="load")
        stream_failure_page.click("#collieSummon")
        stream_failure_page.fill("#capsuleInput", "retry this exact request")
        stream_failure_page.click("#capsuleSend")
        wait_for(stream_failure_page, lambda: bool(stream_failure_page.evaluate(
            "window.__capsuleEventSources.some(source => source.url.includes('/api/stream?'))")))
        stream_failure_page.evaluate("""() => {
          const source=window.__capsuleEventSources.find(source => source.url.includes('/api/stream?'));
          source.emit('start',{session:'rate-session',run:'rate-run'});
          source.emit('token',{t:'\\n[stream error: Anthropic stream ended before its terminal events]'});
          source.emit('retry',{attempt:1,max:3,error:'temporary stream failure'});
          source.emit('token',{t:'\\n[stream error: Anthropic stream ended before its terminal events]'});
          source.emit('retry',{attempt:2,max:3,error:'temporary stream failure'});
          source.emit('token',{t:'\\n[stream error: Anthropic stream ended before its terminal events]'});
          source.emit('done',{session:'rate-session',run:'rate-run',
            error:'retryable: [gave up after 3 retries] HTTP 429 {"type":"rate_limit_error"}'});
        }""")
        failure_text = stream_failure_page.locator("#capsuleReplyText").inner_text()
        check("stream retries collapse into one friendly capsule failure",
              "Model temporarily rate-limited" in failure_text and
              "stream error" not in failure_text.lower() and
              failure_text.count("Anthropic stream ended") == 0 and
              stream_failure_page.is_visible(".capsule-failure-actions button") and
              not stream_failure_errors,
              json.dumps({"text": failure_text, "errors": stream_failure_errors}, ensure_ascii=False))
        streams_before_retry = len(stream_failure_page.evaluate("window.__capsuleStreams"))
        stream_failure_page.click(".capsule-failure-actions button")
        wait_for(stream_failure_page, lambda: len(stream_failure_page.evaluate(
            "window.__capsuleStreams")) == streams_before_retry + 1)
        check("capsule retry resubmits the exact request once",
              stream_failure_page.locator("#capsuleHeardText").inner_text() ==
              "retry this exact request" and
              len(stream_failure_page.evaluate("window.__capsuleStreams")) == streams_before_retry + 1)
        stream_failure_context.close()

        timeout_context = browser.new_context(viewport={"width": 520, "height": 360})
        timeout_context.add_init_script(BASE_INIT + NO_SPEECH_INIT + r"""
          (() => {
            const nativeTimeout = window.setTimeout.bind(window);
            window.setTimeout = (callback, delay, ...args) =>
              nativeTimeout(callback, delay === 12000 ? 60 : delay, ...args);
          })();
        """)
        timeout_page = timeout_context.new_page()
        held_timeout_routes = []
        timeout_page.route("**/api/route*", lambda route: held_timeout_routes.append(route))
        timeout_page.goto(WEB, wait_until="load")
        timeout_page.click("#collieSummon")
        timeout_page.fill("#capsuleInput", "wait without disappearing")
        timeout_page.click("#capsuleSend")
        wait_for(timeout_page, lambda: "Still waiting" in
                 timeout_page.locator("#capsuleVoiceStatus").inner_text())
        check("route timeout stays open with transcript and no duplicate submission claim",
              timeout_page.is_visible("#capsuleLayer") and
              timeout_page.locator("#capsuleHeardText").inner_text() == "wait without disappearing" and
              "Nothing has been submitted twice" in
              timeout_page.locator("#capsuleVoiceStatus").inner_text() and
              len(held_timeout_routes) == 1)
        held_timeout_routes.pop(0).fulfill(
            status=200, content_type="application/json",
            body='{"error":"model_unavailable","detail":"Timeout test released."}')
        wait_for(timeout_page, lambda: "Timeout test released" in
                 timeout_page.locator("#capsuleReplyText").inner_text())
        timeout_context.close()

        # Speech language is a separate preference: this user keeps the app chrome in English but
        # speaks Mandarin. The recognizer must receive zh-CN instead of inheriting en-US from UI.
        speech_language_context = browser.new_context(viewport={"width": 420, "height": 190})
        speech_language_context.add_init_script(BASE_INIT + SPEECH_INIT)
        speech_language_page = speech_language_context.new_page()
        speech_language_errors = []
        speech_language_page.on("pageerror", lambda error: speech_language_errors.append(str(error)))
        speech_language_page.route("**/api/settings*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"values": {"LANG": "en", "VOICE_INPUT": "on",
                                        "VOICE_LANGUAGE": "zh-CN"}})))
        speech_language_page.goto(WEB, wait_until="load")
        wait_for(speech_language_page, lambda: speech_language_page.evaluate(
            "document.documentElement.lang === 'en'"))
        speech_language_page.keyboard.press("Control+Shift+Space")
        wait_for(speech_language_page, lambda: speech_language_page.evaluate("window.__speechStarts === 1"))
        language_state = speech_language_page.evaluate(
            "({ui:document.documentElement.lang,speech:window.__speechInstances.at(-1).lang})")
        check("English interface can recognize spoken Mandarin",
              language_state == {"ui": "en", "speech": "zh-CN"} and not speech_language_errors,
              json.dumps({"state": language_state, "errors": speech_language_errors}, ensure_ascii=False))
        speech_language_page.keyboard.press("Control+Shift+Space")
        speech_language_context.close()

        # The native command host is the card: a compact 660×176 CSS-DIP surface with no page
        # gutter for WebView's fallback colour to leak through, and enough DPI headroom for IME.
        native_context = browser.new_context(viewport={"width": 660, "height": 176})
        native_context.add_init_script(BASE_INIT + WEBVIEW_INIT + SPEECH_INIT)
        host = native_context.new_page()
        host_errors = []
        host.on("pageerror", lambda error: host_errors.append(str(error)))
        host.route("**/api/route*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"kind":"chat"}'))
        host.set_viewport_size({"width": 660, "height": 176})
        host.goto(WEB.rstrip("/") + "/?capsule=1", wait_until="load")
        host.wait_for_selector("#capsuleLayer", state="hidden")
        check("native capsule DOM starts closed with its app chrome removed",
              host.locator(".app").evaluate("e => getComputedStyle(e).display") == "none")
        host.evaluate("window.__webviewEmit({type:'collie-command',action:'open',voice:true,"
                      "host:'command',request_id:1})")
        host.wait_for_selector("#capsuleLayer", state="visible")
        check("native voice waits until the window is presented",
              host.evaluate("window.__speechStarts") == 0 and
              host.evaluate("document.activeElement && document.activeElement.id") != "capsuleInput")
        presented_states_before_prepare = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
        host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:1})")
        wait_for(host, lambda: host.evaluate(
            "window.__nativeCapsuleStates.some(x => x.type === 'collie-command-prepared-state' && x.request_id === 1)"))
        check("native prepare focuses one frame without starting voice or surfacing presented state",
              host.evaluate("document.activeElement && document.activeElement.id") == "capsuleInput" and
              host.evaluate("window.__speechStarts") == 0 and
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
              == presented_states_before_prepare)
        # Same-id delivery is an ACK retry only. It must not replay focus or any prepare side effect.
        host.locator("#capsuleClose").focus()
        prepared_acks_before_retry = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-prepared-state').length")
        host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:1})")
        check("duplicate native prepare re-ACKs without replaying focus or voice",
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-prepared-state').length")
              == prepared_acks_before_retry + 1 and
              host.evaluate("document.activeElement && document.activeElement.id") == "capsuleClose" and
              host.evaluate("window.__speechStarts") == 0 and
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
              == presented_states_before_prepare)
        host.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:1})")
        host.wait_for_timeout(350)
        host_box = host.locator("#collieCapsule").bounding_box()
        host_visual = host.evaluate("""() => {
          const card = document.getElementById('collieCapsule');
          const layer = document.getElementById('capsuleLayer');
          const head = card.querySelector('.capsule-head').getBoundingClientRect();
          const meta = card.querySelector('.capsule-chips').getBoundingClientRect();
          const context = card.querySelector('.capsule-context-chips').getBoundingClientRect();
          const entry = card.querySelector('.capsule-entry').getBoundingClientRect();
          const cs = getComputedStyle(card), ls = getComputedStyle(layer);
          return {cardWidth:card.getBoundingClientRect().width, cardHeight:card.getBoundingClientRect().height,
            cardScrollHeight:card.scrollHeight, cardClientHeight:card.clientHeight,
            layerBackground:ls.backgroundColor, layerPadding:ls.padding,
            overflow:cs.overflow, borderWidth:cs.borderTopWidth, radius:cs.borderRadius,
            shadow:cs.boxShadow, contentHeight:entry.bottom-head.top,
            parts:{head:[head.top,head.bottom,head.height],meta:[meta.top,meta.bottom,meta.height],
              context:[context.top,context.bottom,context.height],entry:[entry.top,entry.bottom,entry.height]},
            order:head.top <= meta.top && meta.bottom <= entry.top,
            pageWidth:document.documentElement.scrollWidth, pageHeight:document.documentElement.scrollHeight,
            viewportWidth:innerWidth, viewportHeight:innerHeight,
            footers:card.querySelectorAll('.capsule-foot').length};
        }""")
        check("native capsule fills the 660x176 command host", host_box and
              abs(host_box["x"]) < .5 and abs(host_box["y"]) < .5 and
              abs(host_box["width"] - 660) < .5 and abs(host_box["height"] - 176) < .5,
              str(host_box))
        check("native host has transparent backdrop and no scrollable black gutter",
              host_visual["layerBackground"] == "rgba(0, 0, 0, 0)" and
              host_visual["layerPadding"] == "0px" and host_visual["overflow"] == "hidden" and
              host_visual["borderWidth"] == "0px" and host_visual["shadow"] == "none" and
              host_visual["radius"] == "18px" and
              host_visual["cardScrollHeight"] == host_visual["cardClientHeight"] and
              host_visual["pageWidth"] == host_visual["viewportWidth"] and
              host_visual["pageHeight"] == host_visual["viewportHeight"],
              json.dumps(host_visual))
        check("status and context precede a footer-free final composer row",
              host_visual["order"] and host_visual["contentHeight"] <= 144 and
              host_visual["footers"] == 0, json.dumps(host_visual))
        check("native capsule mode suppresses product chrome and onboarding",
              host.locator(".app").evaluate("e => getComputedStyle(e).display") == "none" and
              "open" not in (host.locator("#nameOverlay").get_attribute("class") or "") and
              "open" not in (host.locator("#obOverlay").get_attribute("class") or ""))
        first_native_state = host.evaluate("({starts:window.__speechStarts,"
            "active:document.activeElement&&document.activeElement.id,"
            "focusRequests:window.__nativeCapsuleStates.filter(x=>x.type==='collie-command-focus-request'),"
            "presented:window.__nativeCapsuleStates.filter(x=>x.type==='collie-command-presented-state'),"
            "command:window.__nativeCapsuleStates.filter(x=>x.type==='collie-command-state').at(-1)})")
        first_native_state["status"] = host.locator("#capsuleVoiceStatus").inner_text()
        check("first native press starts hands-free voice",
              first_native_state["starts"] == 1 and
              first_native_state["active"] == "capsuleInput" and
              not first_native_state["focusRequests"] and
              "automatically" in first_native_state["status"] and
              first_native_state["presented"] and
              first_native_state["presented"][0].get("dom_focused") is True and
              any(x.get("request_id") == 1 and x.get("voice_started") is True and
                  x.get("dom_focused") is True
                  for x in first_native_state["presented"]) and
              first_native_state["command"] == {
                  "type": "collie-command-state", "open": True, "request_id": 1},
              json.dumps(first_native_state, ensure_ascii=False))
        host.set_viewport_size({"width": 320, "height": 176})
        narrow_host = host.evaluate("""() => {
          const card = document.getElementById('collieCapsule');
          const input = document.getElementById('capsuleInput').getBoundingClientRect();
          const meta = card.querySelector('.capsule-chips');
          const controls = ['capsuleMic','capsuleSend','capsuleClose'].map(id => {
            const r = document.getElementById(id).getBoundingClientRect(); return [r.width,r.height];
          });
          return {width:card.getBoundingClientRect().width, height:card.getBoundingClientRect().height,
            inputWidth:input.width, controls, metaFits:meta.scrollWidth <= meta.clientWidth,
            contexts:['capsuleState','capsuleContext','capsuleWorkspace'].every(id =>
              getComputedStyle(document.getElementById(id)).display !== 'none'),
            pageFits:document.documentElement.scrollWidth === innerWidth &&
              document.documentElement.scrollHeight === innerHeight,
            cardFits:card.scrollHeight === card.clientHeight};
        }""")
        check("320px native host keeps all context semantics and 44px targets",
              narrow_host["width"] == 320 and narrow_host["height"] == 176 and
              narrow_host["inputWidth"] >= 170 and narrow_host["metaFits"] and
              narrow_host["contexts"] and narrow_host["pageFits"] and narrow_host["cardFits"] and
              all(width >= 44 and height >= 44 for width, height in narrow_host["controls"]),
              json.dumps(narrow_host))
        host.set_viewport_size({"width": 660, "height": 176})
        host.keyboard.insert_text("type without clicking")
        check("presented native capsule accepts typing without an activation click",
              host.input_value("#capsuleInput") == "type without clicking")
        # Reproduce the real sequence: the first spoken request created a dispatch, then the person
        # dismissed the capsule and summoned it again.  The new voice decision must happen after
        # openCollieCapsule retires that old dispatch, otherwise only the very first summon listens.
        host.keyboard.press("Enter")
        host.wait_for_selector("#collieCapsule.capsule-response")
        host.evaluate("window.__webviewEmit({type:'collie-command',action:'close',voice:true,"
                      "host:'command',request_id:2})")
        host.wait_for_selector("#capsuleLayer", state="hidden")
        check("second native press always cancels and closes",
              host.evaluate("window.__speechAborts") == 1 and
              host.evaluate("window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-state').at(-1)") == {
                  "type": "collie-command-state", "open": False, "request_id": 2})
        prepared_count_after_close = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-prepared-state').length")
        presented_count_after_close = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
        host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:1});"
                      "window.__webviewEmit({type:'collie-command-prepare',request_id:2});"
                      "window.__webviewEmit({type:'collie-command-presented',request_id:1});"
                      "window.__webviewEmit({type:'collie-command-presented',request_id:2})")
        host.wait_for_timeout(25)
        check("closed and stale native prepare/presented messages are ignored",
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-prepared-state').length")
              == prepared_count_after_close and
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
              == presented_count_after_close and
              host.is_hidden("#capsuleLayer"))

        # WebView delivery is asynchronous. Once request 3 has reopened the host, the older request
        # 2 close must be ignored rather than racing the newly visible capsule back to hidden.
        host.evaluate("window.__webviewEmit({type:'collie-command',action:'open',voice:true,"
                      "host:'command',request_id:3})")
        host.wait_for_selector("#capsuleLayer", state="visible")
        starts_before_presented = host.evaluate("window.__speechStarts")
        prepared_count_before_stale = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-prepared-state').length")
        presented_count_before_stale = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
        host.locator("#capsuleClose").focus()
        host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:2});"
                      "window.__webviewEmit({type:'collie-command-presented',request_id:2})")
        host.wait_for_timeout(25)
        check("stale prepare/presented cannot affect a newer open request",
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-prepared-state').length")
              == prepared_count_before_stale and
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
              == presented_count_before_stale and
              host.evaluate("document.activeElement.id") == "capsuleClose" and
              host.evaluate("window.__speechStarts") == starts_before_presented)
        host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:3})")
        wait_for(host, lambda: host.evaluate(
            "window.__nativeCapsuleStates.some(x => x.type === 'collie-command-prepared-state' && x.request_id === 3)"))
        host.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:3})")
        check("first exact presentation ACK confirms DOM focus",
              host.evaluate("window.__speechStarts") == starts_before_presented + 1 and
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').at(-1)")
              .get("dom_focused") is True)

        # Native does not commit presentation from activeElement alone. Deterministically emulate the
        # WebView document being denied top-level focus: a duplicate may focus the textarea's DOM node
        # but must ACK dom_focused=false. Once focus eligibility returns, the next duplicate re-focuses
        # and ACKs true without creating a second recognizer.
        host.evaluate("Object.defineProperty(document,'hasFocus',{value:()=>false,configurable:true})")
        presented_acks_before_unfocused_retry = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
        host.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:3})")
        unfocused_ack = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').at(-1)")
        check("unfocused duplicate presentation ACK is explicitly false",
              unfocused_ack.get("request_id") == 3 and unfocused_ack.get("dom_focused") is False and
              host.evaluate("window.__speechStarts") == starts_before_presented + 1 and
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').length")
              == presented_acks_before_unfocused_retry + 1, json.dumps(unfocused_ack))
        host.evaluate("delete document.hasFocus")
        host.locator("#capsuleClose").focus()
        host.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:3})")
        refocused_ack = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-presented-state').at(-1)")
        check("duplicate presentation re-focuses and ACKs without restarting voice",
              host.evaluate("document.hasFocus() && document.activeElement.id === 'capsuleInput'") and
              refocused_ack.get("request_id") == 3 and refocused_ack.get("dom_focused") is True and
              host.evaluate("window.__speechStarts") == starts_before_presented + 1,
              json.dumps(refocused_ack))
        # Pointerdown and its resulting focus event are one intentional interaction. The page asks
        # native to reclaim the foreground exactly once for that task and includes the active id.
        host.locator("#capsuleClose").focus()
        focus_requests_before_input = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request').length")
        host.evaluate("const el=document.getElementById('capsuleInput');"
                      "el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));el.focus()")
        host.wait_for_timeout(25)
        input_focus_requests = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request')")
        check("native input interaction requests foreground once with the exact request id",
              len(input_focus_requests) == focus_requests_before_input + 1 and
              input_focus_requests[-1] == {
                  "type": "collie-command-focus-request", "request_id": 3})
        focus_requests_before_backdrop = len(input_focus_requests)
        host.mouse.dblclick(1, 1)
        host.wait_for_timeout(50)
        backdrop_focus_requests = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request')")
        check("desktop-host activation double-click cannot dismiss the capsule",
              host.is_visible("#capsuleLayer") and
              host.evaluate("document.activeElement && document.activeElement.id") == "capsuleInput" and
              len(backdrop_focus_requests) > focus_requests_before_backdrop and
              all(item == {"type": "collie-command-focus-request", "request_id": 3}
                  for item in backdrop_focus_requests[focus_requests_before_backdrop:]))
        state_count_before_stale = host.evaluate("window.__nativeCapsuleStates.length")
        check("newer native open reports its request id", host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-state').at(-1)") == {
                "type": "collie-command-state", "open": True, "request_id": 3})
        host.evaluate("window.__webviewEmit({type:'collie-command',action:'close',voice:true,"
                      "host:'command',request_id:2})")
        host.wait_for_timeout(100)
        check("stale native close cannot hide a newer open",
              host.is_visible("#capsuleLayer") and
              host.evaluate("window.__nativeCapsuleStates.length") == state_count_before_stale and
              host.evaluate("window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-state').at(-1)") == {
                  "type": "collie-command-state", "open": True, "request_id": 3})
        # A pointer event queued by the old visible request can land after a newer close. The handler
        # reads the replaced id and the hidden state, so it must not emit a foreground request.
        focus_count_before_delayed_close = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request').length")
        host.evaluate("setTimeout(() => document.getElementById('capsuleInput').dispatchEvent("
                      "new PointerEvent('pointerdown',{bubbles:true})),0);"
                      "window.__webviewEmit({type:'collie-command',action:'close',voice:true,"
                      "host:'command',request_id:4})")
        host.wait_for_selector("#capsuleLayer", state="hidden")
        host.wait_for_timeout(25)
        check("delayed old interaction cannot focus a closed or replaced request",
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request').length")
              == focus_count_before_delayed_close)

        # Voice-off is still a useful typed command and, critically, remains a real toggle.
        speech_before_voice_off = host.evaluate("window.__speechStarts")
        host.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'open',voice:false,"
            "host:'command',request_id:5})")
        host.wait_for_selector("#capsuleLayer", state="visible")
        host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:5})")
        wait_for(host, lambda: host.evaluate(
            "window.__nativeCapsuleStates.some(x => x.type === 'collie-command-prepared-state' && x.request_id === 5)"))
        focus_count_before_unpresented = host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request').length")
        host.evaluate("document.getElementById('capsuleInput').dispatchEvent("
                      "new PointerEvent('pointerdown',{bubbles:true}))")
        check("unpresented native request cannot ask for foreground",
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request').length")
              == focus_count_before_unpresented)
        host.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:5})")
        host.wait_for_timeout(25)
        check("voice-off native command focuses input without recognition",
              host.evaluate("window.__speechStarts") == speech_before_voice_off and
              host.evaluate("document.activeElement && document.activeElement.id") == "capsuleInput" and
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request').length")
              == focus_count_before_unpresented)
        host.evaluate("document.getElementById('capsuleInput').dispatchEvent("
                      "new PointerEvent('pointerdown',{bubbles:true}))")
        host.wait_for_timeout(25)
        check("deliberate input pointerdown requests foreground for the presented id",
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request').at(-1)") == {
                      "type": "collie-command-focus-request", "request_id": 5} and
              host.evaluate(
                  "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-focus-request').length")
              == focus_count_before_unpresented + 1)
        host.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'close',voice:false,"
            "host:'command',request_id:6})")
        host.wait_for_selector("#capsuleLayer", state="hidden")
        check("second voice-off native press closes", host.evaluate(
            "window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-state').at(-1)") == {
                "type": "collie-command-state", "open": False, "request_id": 6})
        host.reload(wait_until="load")
        host.wait_for_selector("#capsuleLayer", state="hidden")
        host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:5});"
                      "window.__webviewEmit({type:'collie-command-prepare',request_id:6})")
        host.wait_for_timeout(25)
        check("prepare from a pre-reload request is ignored by the fresh page",
              not host.evaluate(
                  "window.__nativeCapsuleStates.some(x => x.type === 'collie-command-prepared-state')"))
        check("native capsule mode produces no page errors", not host_errors, "; ".join(host_errors))
        native_context.close()

        # Every new native summon is a new conversation. Hold /api/route open to prove closing the
        # capsule retires its old observer without cancelling server-owned background work.
        dispatch_context = browser.new_context(viewport={"width": 660, "height": 176})
        dispatch_context.add_init_script(BASE_INIT + WEBVIEW_INIT + NO_SPEECH_INIT)
        dispatch_host = dispatch_context.new_page()
        dispatch_errors = []
        pending_routes = []
        dispatch_host.on("pageerror", lambda error: dispatch_errors.append(str(error)))
        dispatch_host.route("**/api/route*", lambda route: pending_routes.append(route))
        dispatch_host.goto(WEB.rstrip("/") + "/?capsule=1", wait_until="load")
        dispatch_host.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'open',voice:false,"
            "host:'command',request_id:1})")
        dispatch_host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:1})")
        wait_for(dispatch_host, lambda: dispatch_host.evaluate(
            "window.__nativeCapsuleStates.some(x=>x.type==='collie-command-prepared-state'&&x.request_id===1)"))
        dispatch_host.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:1})")
        dispatch_host.fill("#capsuleInput", "route while hidden")
        dispatch_host.keyboard.press("Enter")
        wait_for(dispatch_host, lambda: bool(pending_routes))
        first_layouts = dispatch_host.evaluate(
            "window.__nativeCapsuleStates.filter(x=>x.type==='collie-command-layout')")
        check("unresolved dispatch stays compact with the exact native request id",
              first_layouts and first_layouts[-1] == {
                  "type": "collie-command-layout", "request_id": 1, "phase": "compact"})

        dispatch_host.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'close',voice:false,"
            "host:'command',request_id:2})")
        dispatch_host.wait_for_selector("#capsuleLayer", state="hidden")
        dispatch_host.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'open',voice:false,"
            "host:'command',request_id:3})")
        dispatch_host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:3})")
        prepared_after_reopen = wait_for(dispatch_host, lambda: dispatch_host.evaluate(
            "window.__nativeCapsuleStates.some(x=>x.type==='collie-command-prepared-state'&&x.request_id===3)"))
        dispatch_host.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:3})")
        presented_after_reopen = wait_for(dispatch_host, lambda: dispatch_host.evaluate(
            "window.__nativeCapsuleStates.some(x=>x.type==='collie-command-presented-state'&&"
            "x.request_id===3&&x.dom_focused===true)"))
        reopen_state = dispatch_host.evaluate("({disabled:document.getElementById('capsuleInput').disabled,"
            "value:document.getElementById('capsuleInput').value,result:document.getElementById('capsuleResult').hidden,"
            "active:document.activeElement&&document.activeElement.id,layouts:window.__nativeCapsuleStates.filter("
            "x=>x.type==='collie-command-layout'&&x.request_id===3)})")
        check("native reopen during routing starts a blank focusable conversation",
              prepared_after_reopen and presented_after_reopen and not reopen_state["disabled"] and
              reopen_state["active"] == "capsuleInput" and reopen_state["value"] == "" and
              reopen_state["result"] and reopen_state["layouts"] and
              reopen_state["layouts"][-1]["phase"] == "compact", json.dumps(reopen_state))

        sources_after_reopen = len(dispatch_host.evaluate("window.__capsuleEventSources"))
        pending_routes.pop(0).fulfill(
            status=200, content_type="application/json", body='{"kind":"chat"}')
        dispatch_host.wait_for_timeout(50)
        check("late route completion from the closed conversation is ignored",
              len(dispatch_host.evaluate("window.__capsuleEventSources")) == sources_after_reopen)

        dispatch_host.fill("#capsuleInput", "fresh after reopen")
        dispatch_host.keyboard.press("Enter")
        wait_for(dispatch_host, lambda: bool(pending_routes))
        pending_routes.pop(0).fulfill(
            status=200, content_type="application/json", body='{"kind":"chat"}')
        wait_for(dispatch_host, lambda: len(dispatch_host.evaluate("window.__capsuleEventSources")) >
                 sources_after_reopen)
        dispatch_host.evaluate("window.__capsuleEventSources.at(-1).emit('start',"
                               "{session:'fresh-session',run:'fresh-run'});")
        dispatch_host.evaluate("window.__capsuleEventSources.at(-1).emit('token',"
                               "{t:'" + ("long answer " * 240) + "'});")
        # Keep this run stream: onerror intentionally opens a separate recovery mirror,
        # which then becomes the last EventSource but is not this request's observer callback.
        dispatch_host.evaluate("window.__dispatchSource=window.__capsuleEventSources.at(-1)")
        dispatch_host.set_viewport_size({"width": 320, "height": 360})
        conversation_metrics = dispatch_host.evaluate("""() => {
          const card=document.getElementById('collieCapsule'), reply=document.getElementById('capsuleReplyText');
          const close=document.getElementById('capsuleClose').getBoundingClientRect();
          return {width:card.getBoundingClientRect().width,height:card.getBoundingClientRect().height,
            pageWidth:document.documentElement.scrollWidth,pageHeight:document.documentElement.scrollHeight,
            replyClient:reply.clientHeight,replyScroll:reply.scrollHeight,close:[close.width,close.height],
            inputDisabled:document.getElementById('capsuleInput').disabled};
        }""")
        check("320x360 conversation keeps the page fixed and scrolls only the long answer",
              conversation_metrics["width"] == 320 and conversation_metrics["height"] == 360 and
              conversation_metrics["pageWidth"] == 320 and conversation_metrics["pageHeight"] == 360 and
              conversation_metrics["replyScroll"] > conversation_metrics["replyClient"] and
              conversation_metrics["close"] == [44, 44] and not conversation_metrics["inputDisabled"],
              json.dumps(conversation_metrics))
        streams_before_duplicate = len(dispatch_host.evaluate("window.__capsuleStreams"))
        dispatch_host.keyboard.press("Enter")
        check("active response cannot be submitted twice",
              len(dispatch_host.evaluate("window.__capsuleStreams")) == streams_before_duplicate)

        dispatch_host.evaluate("window.__dispatchSource.fail()")
        check("a dropped connection is shown only in its current invocation",
              "Connection interrupted" in dispatch_host.locator("#capsuleVoiceStatus").inner_text())
        dispatch_host.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'close',voice:false,"
            "host:'command',request_id:4})")
        dispatch_host.wait_for_selector("#capsuleLayer", state="hidden")
        dispatch_host.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'open',voice:false,"
            "host:'command',request_id:5})")
        dispatch_host.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:5})")
        wait_for(dispatch_host, lambda: dispatch_host.evaluate(
            "window.__nativeCapsuleStates.some(x=>x.type==='collie-command-prepared-state'&&x.request_id===5)"))
        dispatch_host.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:5})")
        wait_for(dispatch_host, lambda: dispatch_host.evaluate(
            "window.__nativeCapsuleStates.some(x=>x.type==='collie-command-presented-state'&&"
            "x.request_id===5&&x.dom_focused===true)"))
        fresh_layouts = dispatch_host.evaluate(
            "window.__nativeCapsuleStates.filter(x=>x.type==='collie-command-layout'&&x.request_id===5)")
        fresh_state = dispatch_host.evaluate("({result:document.getElementById('capsuleResult').hidden,"
            "input:document.getElementById('capsuleInput').value,reply:document.getElementById('capsuleReplyText').textContent,"
            "status:document.getElementById('capsuleVoiceStatus').textContent,sourceClosed:window.__dispatchSource.closed})")
        check("reopening after connection interruption is a fresh compact conversation",
              dispatch_host.is_visible("#capsuleLayer") and fresh_state["result"] and
              fresh_state["input"] == "" and fresh_state["reply"] == "" and
              "Connection interrupted" not in fresh_state["status"] and fresh_state["sourceClosed"] and
              fresh_layouts and fresh_layouts[-1]["phase"] == "compact",
              json.dumps({"fresh": fresh_state, "layouts": fresh_layouts}, ensure_ascii=False))
        dispatch_host.evaluate("window.__dispatchSource.emit('done',"
                               "{session:'fresh-session',run:'fresh-run',answer:'Recovered final answer.'})")
        late_done_state = dispatch_host.evaluate("({reply:document.getElementById('capsuleReplyText').textContent,"
            "status:document.getElementById('capsuleVoiceStatus').textContent,"
            "classes:document.getElementById('collieCapsule').className,sourceClosed:window.__dispatchSource.closed})")
        check("late completion from the prior invocation cannot overwrite the new conversation",
              late_done_state["reply"] == "" and
              "Answer ready" not in late_done_state["status"] and not dispatch_errors,
              json.dumps({"late": late_done_state, "errors": dispatch_errors}, ensure_ascii=False))
        dispatch_context.close()

        # The normal app window may share the WebView bridge, but it is not the dedicated capsule
        # host and must never acquire the stronger foreground-reassert capability.
        window_context = browser.new_context(viewport={"width": 520, "height": 360})
        window_context.add_init_script(BASE_INIT + WEBVIEW_INIT + NO_SPEECH_INIT)
        app_window = window_context.new_page()
        app_window.goto(WEB, wait_until="load")
        app_window.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'open',voice:false,"
            "host:'window',request_id:1})")
        app_window.wait_for_selector("#capsuleLayer", state="visible")
        app_window.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:1})")
        app_window.wait_for_timeout(25)
        app_window.evaluate("document.getElementById('capsuleInput').dispatchEvent("
                            "new PointerEvent('pointerdown',{bubbles:true}))")
        check("ordinary app window cannot use native prepare or foreground focus",
              not app_window.evaluate(
                  "window.__nativeCapsuleStates.some(x => x.type === 'collie-command-prepared-state')") and
              not app_window.evaluate(
                  "window.__nativeCapsuleStates.some(x => x.type === 'collie-command-focus-request')"))
        window_context.close()

        # A runtime without the non-standard Web Speech API must still provide a closable typed capsule.
        no_speech_context = browser.new_context(viewport={"width": 420, "height": 190})
        no_speech_context.add_init_script(BASE_INIT + WEBVIEW_INIT + NO_SPEECH_INIT)
        no_speech = no_speech_context.new_page()
        no_speech_errors = []
        no_speech.on("pageerror", lambda error: no_speech_errors.append(str(error)))
        no_speech.goto(WEB.rstrip("/") + "/?capsule=1", wait_until="load")
        no_speech.wait_for_selector("#capsuleLayer", state="hidden")
        no_speech.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'open',voice:true,"
            "host:'command',request_id:1})")
        no_speech.wait_for_selector("#capsuleLayer", state="visible")
        no_speech.evaluate("window.__webviewEmit({type:'collie-command-prepare',request_id:1})")
        wait_for(no_speech, lambda: no_speech.evaluate(
            "window.__nativeCapsuleStates.some(x => x.type === 'collie-command-prepared-state' && x.request_id === 1)"))
        no_speech.evaluate("window.__webviewEmit({type:'collie-command-presented',request_id:1})")
        check("missing SpeechRecognition falls back to focused typed input",
              no_speech.evaluate("document.activeElement && document.activeElement.id") == "capsuleInput" and
              "not available" in no_speech.locator("#capsuleVoiceStatus").inner_text())
        no_speech.evaluate(
            "window.__webviewEmit({type:'collie-command',action:'close',voice:true,"
            "host:'command',request_id:2})")
        no_speech.wait_for_selector("#capsuleLayer", state="hidden")
        check("missing SpeechRecognition still closes on the second native press",
              no_speech.evaluate("window.__nativeCapsuleStates.filter(x => x.type === 'collie-command-state').at(-1)") == {
                  "type": "collie-command-state", "open": False, "request_id": 2} and
              not no_speech_errors,
              "; ".join(no_speech_errors))
        no_speech_context.close()

        browser.close()

    failed = [name for name, ok in RESULTS if not ok]
    if failed:
        print("capsule_ui_check: %d failed" % len(failed))
        return 1
    print("capsule_ui_check: all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
