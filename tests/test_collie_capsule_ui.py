"""Static contract checks for the app-global Collie command/voice capsule.

These are intentionally narrow: the browser suite exercises behavior, while these checks keep the
desktop message contract, accessibility landmarks, and Needs You boundary visible in code review.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "harness" / "webui" / "index.html").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return HTML.split(start, 1)[1].split(end, 1)[0]


def test_capsule_is_a_modal_collie_first_entry_not_a_model_picker():
    block = _between('<div class="capsule-layer" id="capsuleLayer"', "<!-- ============================================ model picker -->")
    assert 'id="collieCapsule" role="dialog" aria-modal="true"' in block
    assert 'aria-labelledby="capsuleTitle" aria-describedby="capsuleHelp"' in block
    assert 'id="capsuleInput"' in block
    assert 'id="capsuleMic" aria-pressed="false"' in block
    assert 'id="capsuleSend"' in block
    assert 'id="capsuleState"' in block and 'id="capsuleContext"' in block
    assert 'id="capsuleWorkspace"' in block
    assert 'id="capsuleAttachments" hidden' in block
    assert 'class="capsule-status-slot" id="capsuleHelp"' in block
    assert 'class="capsule-voice-status" id="capsuleVoiceStatus"' in block
    assert "capsule-foot" not in block
    assert "modelTrigger" not in block
    assert "provider" not in block.lower()


def test_capsule_reuses_the_single_composer_and_run_contract():
    code = _between("// ---- Collie command / voice capsule", "// the interactive stream run")
    assert 'mainInput.value = q;' in code
    assert 'mainInput.dispatchEvent(new Event("input", { bubbles:true }))' in code
    assert 'var dispatchId = beginCapsuleDispatch(q);' in code
    assert 'send(dispatchId);' in code
    assert 'closeCollieCapsule(); send();' not in code
    assert 'if (!running && !currentSession) newThread();' in code
    assert "/api/stream" not in code
    assert "/api/route" not in code
    # Run setup is validated by the one composer only after its shared audio preflight falls through.
    assert "validateRunConfig(runConfig)" in HTML
    assert "tryDirectAudio(q, capsuleDispatchId)" in HTML
    assert 't("{n} attached images")' in code
    assert ".capsule-chip[hidden] { display:none; }" in HTML
    assert ".capsule-chips[hidden] { display:none !important; }" in HTML


def test_capsule_shortcut_voice_escape_and_native_shell_contract_are_explicit():
    code = _between("// ---- Collie command / voice capsule", "// the interactive stream run")
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in code
    assert "function startCapsuleVoice(autoSendOnEnd)" in code
    assert "capsuleAutoSend = !!autoSendOnEnd" in code
    manual_input_guard = _between("function stopCapsuleVoiceForManualInput(event)",
                                  "function startCapsuleVoice(autoSendOnEnd)")
    assert "!event.isTrusted" in manual_input_guard
    assert "!capsuleRecognition" in manual_input_guard
    assert "capsuleManualDraft = true" in manual_input_guard
    assert "stopCapsuleVoice(false, true)" in manual_input_guard
    assert 'setCapsuleVoiceStatus("Typing…")' in manual_input_guard
    assert 'capsuleInput.addEventListener("beforeinput", stopCapsuleVoiceForManualInput)' in code
    assert 'capsuleInput.addEventListener("input", stopCapsuleVoiceForManualInput)' not in code
    assert "capsuleManualDraft = false, capsuleComposing = false" in code
    assert 'capsuleInput.addEventListener("compositionstart", function () { capsuleComposing = true; })' in code
    assert 'capsuleInput.addEventListener("compositionend", function () { capsuleComposing = false; })' in code
    assert 'capsuleInput.addEventListener("blur", function () { capsuleComposing = false; })' in code
    key_handler = _between('capsuleInput.addEventListener("keydown", function (event)',
                           'capsuleSend.addEventListener("click", submitCapsule)')
    assert "capsuleComposing || event.isComposing" in key_handler
    assert "event.keyCode === 229 || event.which === 229" in key_handler
    assert 'event.key === "Enter" && !event.shiftKey && !imeCandidateEnter' in key_handler
    assert "event.preventDefault(); submitCapsule();" in key_handler
    assert "capsuleRecognition !== recognition" in code
    assert "capsuleInput.value = [capsuleVoiceBase, spoken]" in code
    assert 'capsuleManualDraft && capsuleLayer && !capsuleLayer.hidden' in code
    assert 'event.code === "Space"' in code
    assert "capsuleDocumentHotkey(event)" in code and "event.shiftKey" in code
    assert 'spec === "ctrl+shift+space"' in code and 'spec === "off"' in code
    for event_name in ("pointerdown", "pointerup", "pointercancel", "keydown", "keyup"):
        assert f'addEventListener("{event_name}"' in code
    assert 'event.key === "Escape" && !capsuleLayer.hidden' in code
    assert 'stopCapsuleVoice(false, true)' in code
    assert 'window.addEventListener("blur", cancelCapsuleVoiceOnInterruption)' in code
    assert 'if (document.hidden) cancelCapsuleVoiceOnInterruption()' in code
    assert 'if (window.chrome && window.chrome.webview) return;' in code
    assert 'if (!capsuleLayer.hidden) { closeCollieCapsule(); return; }' in code
    assert "startCapsuleVoice(true)" in code
    assert 'message.type !== "collie-command"' in code
    assert '["open","close","toggle"].indexOf(message.action) < 0' in code
    assert 'message.action === "close"' in code
    assert 'message.action === "toggle" && !capsuleLayer.hidden' in code
    assert "message.request_id !== undefined" in code
    assert "incomingRequestId < capsuleNativeRequestId" in code
    assert "capsuleNativeVoiceEnabled = message.voice === true" in code
    assert "capsuleInput.focus();" in code
    assert "capsuleMic.disabled = responseActive || blocked || !capsuleVoiceAvailable()" in code
    assert 'type:"collie-command-state", open:!!open, request_id:capsuleNativeRequestId' in code
    assert 'type:"collie-command-presented-state"' in code
    assert 'voice_started:!!capsuleRecognitionStarted' in code
    assert 'dom_focused:!!(document.hasFocus() && document.activeElement === capsuleInput)' in code
    assert 'voice_available:capsuleVoiceAvailable()' in code
    assert 'message.type === "collie-command-presented"' in code
    assert 'waitForNativePresentation = message.host === "command"' in code
    assert "capsuleNativePresentedRequestId" in code
    assert "capsuleNativePreparingRequestId" in code
    assert "capsuleNativePreparedRequestId" in code
    assert "capsuleNativeCommandHost" in code
    assert 'message.type === "collie-command-prepare"' in code
    assert 'type:"collie-command-prepared-state", request_id:requestId' in code
    assert "function focusCapsuleInputForPresentation(requestId)" in code
    assert "capsuleInput.focus({ preventScroll:true })" in code
    assert "window.requestAnimationFrame(function () { focusIfCurrent(true); })" in code
    assert "focusIfCurrent(false);" in code
    assert "focusCapsuleInputForPresentation(presentedRequestId);" in code
    assert code.index("focusCapsuleInputForPresentation(presentedRequestId);") < code.index(
        "if (capsuleNativeVoiceToggle) startCapsuleVoice(true)",
        code.index("focusCapsuleInputForPresentation(presentedRequestId);"),
    )
    presented_handler = _between('message.type === "collie-command-presented"',
                                 'message.type !== "collie-command"')
    assert "!capsuleUsesNativePresentationFence()" in presented_handler
    assert "!capsuleLayer || capsuleLayer.hidden" in presented_handler
    assert "presentedRequestId !== capsuleNativeRequestId" in presented_handler
    assert "presentedRequestId !== capsuleNativeHandledRequestId" in presented_handler
    duplicate_presented = _between(
        "presentedRequestId === capsuleNativePresentedRequestId", "capsuleNativePresentedRequestId =")
    assert "document.hasFocus()" in duplicate_presented
    assert "document.activeElement === capsuleInput" in duplicate_presented
    assert "focusCapsuleInputForPresentation(presentedRequestId)" in duplicate_presented
    assert "postCapsuleNativePresentedState()" in duplicate_presented
    assert "startCapsuleVoice" not in duplicate_presented
    assert "if (event.target !== capsuleLayer) return;" in code
    assert "if (!CAPSULE_HOST_MODE) { closeCollieCapsule(); return; }" in code
    focus_bridge = _between("function requestCapsuleNativeFocus(requestId)",
                            "function focusCapsuleInputForPresentation(requestId)")
    assert "capsuleUsesNativePresentationFence()" in focus_bridge
    assert "capsuleLayer.hidden" in focus_bridge
    assert "requestId !== capsuleNativeRequestId" in focus_bridge
    assert "requestId !== capsuleNativeHandledRequestId" in focus_bridge
    assert "requestId !== capsuleNativePresentedRequestId" in focus_bridge
    assert "capsuleNativeFocusRequestQueuedId === requestId" in focus_bridge
    assert 'type:"collie-command-focus-request", request_id:requestId' in focus_bridge
    assert code.count("requestCapsuleNativeFocus(capsuleNativeRequestId);") >= 2
    assert 'capsuleInput.addEventListener("pointerdown"' in code
    assert 'capsuleInput.addEventListener("focus"' not in code
    prepare_handler = _between('message.type === "collie-command-prepare"',
                               'message.type === "collie-command-presented"')
    assert "prepareRequestId !== capsuleNativeRequestId" in prepare_handler
    assert "prepareRequestId !== capsuleNativeHandledRequestId" in prepare_handler
    assert "capsuleLayer.hidden" in prepare_handler
    assert "capsuleUsesNativePresentationFence()" in prepare_handler
    assert "prepareRequestId === capsuleNativePreparedRequestId" in prepare_handler
    assert "postCapsuleNativePreparedState(prepareRequestId)" in prepare_handler
    assert "capsuleNativePresentedRequestId =" not in prepare_handler
    assert "startCapsuleVoice" not in prepare_handler
    assert "postCapsuleNativePresentedState" not in prepare_handler
    prepare_focus = _between("function prepareCapsuleInputForNative(requestId)",
                             "function openCollieCapsule()")
    assert "window.requestAnimationFrame(finishPreparation)" in prepare_focus
    assert "postCapsuleNativePreparedState(requestId)" in prepare_focus
    assert "startCapsuleVoice" not in prepare_focus
    assert "postCapsuleNativePresentedState" not in prepare_focus
    assert "if (CAPSULE_HOST_MODE) openCollieCapsule()" not in code
    assert 'html[data-capsule-host="true"] .app { display:none !important; }' in HTML


def test_capsule_keeps_final_transcript_but_native_reopen_starts_a_fresh_conversation():
    code = _between("// ---- Collie command / voice capsule", "// the interactive stream run")
    stream = HTML.split("function runStream(q, imgs, runConfig, runSession, userMsgEl, capsuleDispatchId)", 1)[1]
    block = _between('<div class="capsule-layer" id="capsuleLayer"',
                     "<!-- ============================================ model picker -->")

    assert 'id="capsuleResult" hidden aria-labelledby="capsuleReplyHead"' in block
    assert 'id="capsuleHeardText"' in block and 'id="capsuleReplyText" role="document" tabindex="0"' in block
    assert 'capsuleReplyText" role="status"' not in block and 'capsuleResult" hidden aria-live=' not in block
    assert "capsuleHeardText.textContent = dispatch.text" in code
    assert "capsuleActiveDispatch && !capsuleActiveDispatch.terminal" in code
    assert 'updateCapsuleDispatch(id, "timeout", {})' in code
    assert '(capsuleActiveDispatch && !capsuleActiveDispatch.terminal) || routePending' in code
    fresh = _between("function prepareFreshNativeCapsule()", "function openCollieCapsule()")
    assert "!CAPSULE_HOST_MODE || !capsuleFreshOnNextNativeOpen" in fresh
    assert "capsuleActiveDispatch = null" in fresh
    assert "capsuleDispatchSequence++" in fresh
    assert "resetCapsuleDispatchView()" in fresh
    assert "newThread()" in fresh
    opened = _between("function openCollieCapsule()", "function resetCapsuleVoiceUi()")
    assert "prepareFreshNativeCapsule();" in opened
    closed = _between("function closeCollieCapsule()", "function submitCapsule()")
    assert "if (CAPSULE_HOST_MODE) capsuleFreshOnNextNativeOpen = true" in closed
    assert "server-owned work continues in Activity" in closed
    failed = _between("function failCapsuleDispatch(dispatch, data)",
                      "function updateCapsuleDispatch(id, type, payload)")
    assert 'dispatch.phase = "failed"; dispatch.terminal = true' in failed
    assert "capsuleActiveDispatch = null" not in failed
    assert "resetCapsuleDispatchView()" not in failed
    assert 'reason === "model-unavailable" && failure.detail' in failed
    unknown = _between('} else if (type === "unknown") {',
                       '} else if (type === "timeout") {')
    assert 'dispatch.phase = "unknown"' in unknown
    assert "dispatch.terminal = true" not in unknown
    assert "var responseActive = !!(capsuleActiveDispatch && !capsuleActiveDispatch.terminal)" in code

    # The observer is an optional id on the one existing route/SSE path, not a second request.
    assert "function send(capsuleDispatchId, directCommandChecked)" in HTML
    assert 'runStream(q, imgs, runConfig, runSession, userMsgEl, capsuleDispatchId)' in HTML
    for event in ('"accepted"', '"token"', '"tool"', '"needs-you"', '"done"', '"unknown"'):
        assert f"updateCapsuleDispatch(capsuleDispatchId, {event}" in stream
    assert 'updateCapsuleDispatch(capsuleDispatchId, "retry", d)' in stream
    assert "function capsuleTokenIsInternalError(value)" in code
    assert 'dispatch.answer = ""; dispatch.failureInfo = null' in code
    assert "dispatch.failureInfo = friendlyRunError(data.error)" in code
    assert 'retryButton.onclick = retryCapsuleDispatch' in code
    assert 'answer:cleanCapsuleStreamText(d.answer || streamedAll || "")' in stream
    assert "new EventSource(url)" in stream
    assert HTML.count("new EventSource(url)") == 1
    assert "Number.isSafeInteger(capsuleDispatchId) && capsuleDispatchId > 0" in stream
    assert '"&entrypoint=capsule"' in stream
    assert 'id="stopRun"' in HTML
    assert '$("send").onclick = function () { send(); };' in HTML
    assert '$("stopRun").onclick = function () { cancelRun(); };' in HTML
    assert 'type:"collie-command-layout", request_id:capsuleNativeRequestId' in code
    assert 'phase:phase === "conversation" ? "conversation" : "compact"' in code
    assert ".collie-capsule.capsule-response .capsule-entry { position:absolute" in HTML
    assert "display:none" not in _between(
        ".collie-capsule.capsule-response .capsule-entry {", "}")
    assert ".collie-capsule.capsule-followup .capsule-entry" in HTML
    assert 'capsuleCard.classList.toggle("capsule-followup", !!dispatch.terminal)' in code
    assert 'capsuleActiveDispatch && !capsuleActiveDispatch.terminal' in code
    assert '"Ask a follow-up or add an instruction…"' in code


def test_capsule_speech_language_can_differ_from_interface_language():
    code = _between("// ---- Collie command / voice capsule", "// the interactive stream run")
    recognition = _between("function capsuleRecognitionLang()", "function capsulePlatformName()")
    assert 'CAPSULE_SPEECH_LANG = "auto"' in HTML
    assert 'CAPSULE_SPEECH_LANG = bootValues.VOICE_LANGUAGE || "auto"' in HTML
    assert 'String(CAPSULE_SPEECH_LANG || "auto").trim()' in recognition
    assert 'configured !== "auto"' in recognition
    assert 'return configured' in recognition
    assert 'recognition.lang = capsuleRecognitionLang()' in code


def test_capsule_uses_shared_modal_inert_and_focus_management():
    modal_helpers = _between("function visibleDialogOverlay()", "// Escape ALL five")
    assert '$("capsuleLayer")' in modal_helpers
    assert 'background.setAttribute("inert", "")' in modal_helpers
    assert 'background.removeAttribute("inert")' in modal_helpers
    code = _between("// ---- Collie command / voice capsule", "// the interactive stream run")
    assert 'dialogOpened(capsuleLayer, capsuleInput.disabled ? $("capsuleClose") : capsuleInput,' in code
    assert "capsuleUsesNativePresentationFence());" in code
    assert "if (deferInitialFocus) return;" in modal_helpers
    assert "dialogClosed(capsuleLayer, capsuleSummon)" in code


def test_activity_only_personal_state_cannot_render_a_duplicate_done_card():
    card = _between("function renderExecutiveCard(ps)", '// nav\n  ["Today"')
    assert '!ps.suggestion && !(ps.task && ps.task.status === "done")' in card
    assert 'duplicate "Done" card' in card


def test_capsule_targets_are_mobile_sized_and_fit_320px():
    assert ".capsule-mic,.capsule-send { width:44px; height:44px" in HTML
    assert ".capsule-close { width:44px; height:44px" in HTML
    mobile = _between("@media (max-width:420px)", "</style>")
    assert ".capsule-layer { padding:64px 8px 16px" in mobile
    assert ".collie-capsule { width:calc(100vw - 16px)" in mobile
    assert ".capsule-entry { margin:0 7px" in mobile


def test_command_host_is_the_card_and_has_no_scrollable_black_gutter():
    host = _between('html[data-capsule-host="true"],html[data-capsule-host="true"] body',
                    "/* Missions and decision queues")
    assert "background:transparent" in host
    assert 'html[data-capsule-host="true"] .capsule-layer { padding:0; align-items:stretch; background:transparent' in host
    assert 'html[data-capsule-host="true"] .collie-capsule { width:100%; height:100%; max-height:none; justify-content:center; padding:8px 0;' in host
    assert "overflow:hidden; border:0; border-radius:18px; background:var(--surface); box-shadow:none" in host
    assert 'html[data-capsule-host="true"] .capsule-entry { min-height:64px; height:64px; margin:0 12px; }' in host
    assert 'html[data-capsule-host="true"] .capsule-key-hint { display:none; }' in host
    assert "overflow:auto" not in host


def test_needs_you_is_only_a_real_interruption_inbox():
    # Attribute order changed when the secondary destinations became a real menu. Keep the
    # contract focused on semantics: the destination starts hidden and remains a menu item.
    assert 'id="needsYouNav" data-i18n-aria-label="Needs You" aria-label="Needs You"' in HTML
    assert 'id="needsYouNav" data-i18n-aria-label="Needs You" aria-label="Needs You" role="menuitem" hidden' in HTML
    assert 'nav.hidden = n === 0 && !nav.classList.contains("on");' in HTML
    assert 'missionVerificationConflictReasons(row).indexOf("authorization") >= 0' in HTML
    assert 'state === "status_conflict" && missionVerificationConflictReasons(row).indexOf("authorization") >= 0' in HTML
    assert "!terminal[state] && !humanBound" in HTML
    assert "humanSignals.some(function (value)" in HTML
    assert 'badge.textContent = t("Attention")' in HTML
    assert "Only sensitive approvals, identity steps, and decisions Collie cannot make for you." in HTML


def test_capsule_and_interruption_copy_is_localized_in_both_chinese_variants():
    for key in (
        '"Ask {name}"',
        '"Hold the microphone, then release to send."',
        '"Listening… release to send."',
        '"Listening… pause to send automatically."',
        '"Typing…"',
        '"I heard:"',
        '"Handing this to {name}…"',
        '"Answer ready · ask a follow-up or close."',
        '"Ask a follow-up or add an instruction…"',
        '"Connection interrupted. {name} may still be working; check Activity."',
        '"Could not start the stream."',
        '"The request was not delivered. The current run is unchanged."',
        '"I couldn\'t start that request. Nothing was submitted twice."',
        '"Only sensitive approvals, identity steps, and decisions Collie cannot make for you."',
    ):
        assert HTML.count(key) >= 3, key
