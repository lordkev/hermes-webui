#!/usr/bin/env python3
"""Behavioral gate for the #8156 stale-bundle recovery (re-gate of 2026-10-19).

A page running an older bundle gets ``409 stale_client_bundle`` from
``/api/chat/start``. The reviewer reproduced three ways the recovery lost the
user's message where master would have sent it; each is replayed here against
the REAL shipped ``send()`` (plus ``_restoreComposerDraftAfterFailedSend`` and
``ui.js``'s ``setBusy``) extracted verbatim and run in a Node VM with stubbed
browser APIs, the technique ``test_7855_goal_continuation_binding.py`` uses:

  1. typing during the held start: the rejected message must stay reachable
     (the composer keeps the newer input, so the rejected turn is queued);
  2. follow-ups queued during the held start: one ``/api/chat/start`` only, the
     composer holds the original text + file, both follow-ups stay queued;
  3. a rejected send with attachments: the status says files must be re-added
     after the reload the same message asks for (a reload restores text only).

The October 27 gates also cover attachment-only newer input, warnings for the
owner's queued/staged files, and the persisted reload path through the real
queue restore, draft restore, compression-resume probe and setBusy(). API and
DOM boundaries are stubbed; this proves client state behavior, not browser
storage durability or provider execution. Older gates retain synthesized
pre-fix controls.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_SRC = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
COMMANDS_SRC = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level function (and its whole body) out of the real source."""
    key = f"async function {name}(" if f"async function {name}(" in source else f"function {name}("
    start = source.index(key)
    opening = re.search(r"\)\s*\{", source[start:])
    assert opening, f"{name} function body did not open"
    body_start = start + opening.end() - 1
    depth = 0
    for idx in range(body_start, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"{name} function body did not close")


_HARNESS = r"""
const vm = require('vm');
const SRC  = __SRC__;
const MODE = __MODE__;
const PROBE_STATUS = Number((MODE.match(/probe-(\d+)$/) || [])[1]) || null;
const {File} = require('buffer');
const ORIGINAL_FILE = new File(['original payload'], 'original.txt', {type:'text/plain'});
const NEWER_FILE = new File(['newer payload'], 'newer.txt', {type:'text/plain'});
let FINISH_CLEAR = null;

const POSTED = [], SAVED = [], STATUS = [], TOASTS = [], APPLIED = [];
const DRAFTS = new Map();
const store = () => { const data = new Map(); return {
  getItem: k => data.get(k) || null, setItem: (k, v) => data.set(k, String(v)),
  removeItem: k => data.delete(k) }; };
let HOLD = null, HELD_ONCE = false;
let COMPLETE_COMPRESSION = null;
const el = () => ({ value:'', style:{}, dataset:{}, classList:{add(){},remove(){},toggle(){},contains(){return false;}},
  appendChild(){}, addEventListener(){}, setAttribute(){}, removeAttribute(){}, focus(){}, click(){},
  querySelector(){return null;}, querySelectorAll(){return [];}, children:[], textContent:'', innerHTML:'' });
const staleError = () => {
  const err = new Error('This page is running an older WebUI build than the server; reload to continue.');
  err.status = 409;
  err.body = JSON.stringify({ type:'stale_client_bundle', retryable:true });
  return err;
};

const target = {
  console, setTimeout, clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  document:{ querySelector(){return null;}, querySelectorAll(){return [];}, getElementById(){return null;},
             createElement(){return el();}, addEventListener(){}, body:el(), documentElement:el() },
  localStorage:store(), sessionStorage:store(), SESSION_QUEUES:{},
  _compressionUi:null, _compressionLockSid:null,
  _compressionPlaceholderSaved:null, _compressionElapsedTimer:null,
  renderCompressionUi:() => {},
  _loadingSessionId:null, _composerDraftLocalFallbackBySid:new Map(),
  _composerDraftHasPayload:(text, files) => !!(text || files.length),
  _isComposerDraftRestoreSuppressed:() => false,
  history:{ replaceState(){}, pushState(){} },
  location:{ pathname:'/', search:'', href:'http://x/', reload(){ target.RELOADED = true; } },
  navigator:{ clipboard:{ writeText:async()=>{} } },
  S:{ session:{ session_id:'sid-1', model:'m', model_provider:'p', workspace:null, profile:null },
      activeProfile:'default', busy:false, activeStreamId:null, pendingFiles:[], _pendingSessionToolsets:null,
      messages:[{role:'user',content:'earlier question'},{role:'assistant',content:'earlier answer'}] },
  INFLIGHT:{}, LIVE_STREAMS:{}, COMMANDS:[], _AGENT_COMMANDS_RUN_ON_WEBUI:new Set(),
  _sendInProgress:false, _sendInProgressSid:null, _queueDrainSid:null, _pendingSelections:[],
  _forcedSkillDirectivePending:null, _pendingMoaConfig:null, _approvalSessionId:null, _clarifySessionId:null,
  _projectPickerOpenEpoch:0, RELOADED:false,
  t: (k) => k,
  api: async (url, opts) => {
    if (url.startsWith('/api/session/compress/status?')) {
      if (PROBE_STATUS) {
        throw Object.assign(new Error('status refused'), {
          status:PROBE_STATUS, body:JSON.stringify({code:'probe_refused'})});
      }
      if (MODE.endsWith('-error') && !MODE.startsWith('reload-running')) {
        throw Object.assign(new Error('status unavailable'), {status:503});
      }
      return {status:MODE.startsWith('reload-running') ? 'running' : 'idle'};
    }
    if (url === '/api/chat/start' && opts && opts.body) {
      POSTED.push(JSON.parse(opts.body));
      if (!HELD_ONCE) { HELD_ONCE = true; await new Promise(r => { HOLD = r; }); }
      if (MODE.startsWith('ordinary-')) throw Object.assign(new Error('start unavailable'), {status:503});
      if (MODE.startsWith('rotation-')) throw Object.assign(new Error('session rotated'), {
        status:409, body:JSON.stringify({code:'session_rotated', continuation_session_id:'child'})});
      throw staleError();
    }
    return {};
  },
  loadSession:async sid => {
    target.S.session = {session_id:sid};
    target.__msg.value = MODE.endsWith('-text') ? 'NEWER draft' : '';
    target.S.pendingFiles = MODE.endsWith('-files') ? [NEWER_FILE] : [];
  },
  uploadPendingFiles: async ({files}) => (files || []).map(f => ({ name:f.name, path:'/attachments/sid-1/' + f.name })),
  _saveComposerDraftNow: (sid, text, files, contId) => {
    SAVED.push({ sid, text, files:(files||[]).map(f => f.name), ...(contId ? { contId } : {}) });
    DRAFTS.set(sid, {text, files:(files||[]).map(f => ({name:f.name})), goal_continuation_id:contId || ''}); },
  _clearComposerDraft:() => new Promise(resolve => { FINISH_CLEAR = resolve; }),
  _manualCompressionVisibleMessages:() => target.S.messages,
  _pollManualCompressionResult:() => new Promise((resolve, reject) => {
    COMPLETE_COMPRESSION = () => MODE === 'reload-running-error'
      ? reject(Object.assign(new Error('compression failed'), {status:400})) : resolve({status:'done'});
  }),
  showToast:text => TOASTS.push(text),
  setComposerStatus: (s) => { STATUS.push(String(s || '')); },
  _chatPayloadModelState: () => ({ model: target.__model || 'm', model_provider:'p' }),
  _composerTextWithPendingSelections: () => target.__msg.value,
  _normalizeGoalContinuationId: (id) => String(id || '').trim(),
  __msg: el(),
};
for (const k of ['Array','String','Object','Number','Boolean','JSON','Math','Date','Promise','RegExp',
                 'TypeError','Error','RangeError','Map','Set','WeakMap','Symbol','parseInt','parseFloat',
                 'isNaN','isFinite','encodeURIComponent','decodeURIComponent','structuredClone',
                 'TextEncoder','TextDecoder','Intl','BigInt','queueMicrotask','setImmediate','clearImmediate']) {
  if (k in globalThis) target[k] = globalThis[k];
}
target.$ = (id) => (id === 'msg' ? target.__msg : el());
target.window = target; target.globalThis = target;
// Unmodelled presentation helpers are no-ops, never the lock/queue/send path.
const realStateFunctions = new Set(['setCompressionUi', 'isCompressionUiRunning',
  '_setCompressionSessionLock', '_compressionSessionLock', '_compressionStateForCurrentSession',
  'clearCompressionUi', 'setBusy', 'queueSessionMessage', 'shiftQueuedSessionMessage', 'send',
  '_applyManualCompressionResult', '_sessionProfileMismatchFromError']);
const ctx = new Proxy(target, {
  has: (t, k) => k in t,
  get: (t, k) => {
    if (k in t) return t[k];
    if (realStateFunctions.has(k)) throw new Error('Missing real function: ' + k);
    return (t[k] = () => {});
  },
});
vm.createContext(ctx);
vm.runInContext(SRC, ctx);
const applyCompressionResult = target._applyManualCompressionResult;
target._applyManualCompressionResult = async (...args) => {
  APPLIED.push(args[0].status);
  return applyCompressionResult(...args);
};

const tick = async (n) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
const compressionSnapshot = () => ({
  busy:target.S.busy, lock:target._compressionSessionLock(),
  running:target.isCompressionUiRunning(), phase:target._compressionUi?.phase || null,
});
const report = () => {
  const QUEUE = target._getSessionQueue(target.S.session.session_id, false);
  console.log('SESSION=' + JSON.stringify(target.S.session.session_id));
  console.log('TRANSCRIPT=' + JSON.stringify(target.S.messages.map(m => m.role + ':' + m.content)));
  console.log('COMPOSER=' + JSON.stringify(target.__msg.value));
  console.log('TRAY=' + JSON.stringify((target.S.pendingFiles || []).map(f => f.name)));
  console.log('QUEUE=' + JSON.stringify(QUEUE.map(e => e.text)));
  console.log('QUEUE_HEAD=' + JSON.stringify(QUEUE.length ? { text:QUEUE[0].text,
    files:(QUEUE[0].files || []).map(f => f.name), contId:QUEUE[0].goal_continuation_id || '',
    model:QUEUE[0].model } : null));
  console.log('STARTS=' + JSON.stringify(POSTED.length));
  console.log('SAVED=' + JSON.stringify(SAVED));
  console.log('STATUS=' + JSON.stringify(STATUS.filter(Boolean).pop() || ''));
  console.log('BUSY=' + JSON.stringify(!!target.S.busy));
  console.log('RELOADED=' + JSON.stringify(!!target.RELOADED));
  console.log('PERSISTED=' + JSON.stringify(target._readPersistedSessionQueue('sid-1').map(e => e.text)));
  console.log('APPLIED=' + JSON.stringify(APPLIED));
  console.log('COMPRESSION=' + JSON.stringify(compressionSnapshot()));
  console.log('POSTED_TEXTS=' + JSON.stringify(POSTED.map(p => p.message)));
  console.log('TOASTS=' + JSON.stringify(TOASTS));
  console.log('OWNER_QUEUE_FILES=' + JSON.stringify(target._getSessionQueue('sid-1', false)
    .map(e => (e.files || []).map(f => f.name))));
  console.log('ORIGINAL_FILE_RETAINED=' + JSON.stringify(QUEUE.some(e => e.files.includes(ORIGINAL_FILE))
    || target.S.pendingFiles.includes(ORIGINAL_FILE)));
  console.log('NEWER_FILE_RETAINED=' + JSON.stringify(target.S.pendingFiles.includes(NEWER_FILE)));
};

(async () => {
  target.__msg.value = 'ORIGINAL message';
  const noOriginalFiles = ['nofiles', 'queuefiles', 'composerfiles', 'foreignfiles', 'ownerqueuefiles-switch'].includes(MODE);
  target.S.pendingFiles = noOriginalFiles ? [] : [ORIGINAL_FILE];
  const first = (MODE.startsWith('ordinary-') || MODE.startsWith('rotation-') || MODE === 'goal' || MODE === 'goaltyping' || MODE === 'goalstaging' || MODE.startsWith('reload-typing'))
    ? target.send({ goalContinuationId:'0123aaaa' }) : target.send();
  for (let i = 0; i < 500 && !HOLD; i++) await tick(1);
  if (!HOLD) { console.log('HOLD_FAILED'); report(); return; }
  if (MODE.startsWith('ordinary-')) {
    target.__msg.value = MODE.endsWith('-text') ? 'NEWER draft' : '';
    target.S.pendingFiles = MODE.endsWith('-files') ? [NEWER_FILE] : [];
  } else if (MODE === 'typing' || MODE === 'goaltyping') {
    target.__msg.value = 'NEWER draft';
    target.__model = 'm2';  // and picks another model for it while the start is held
  } else if (MODE.startsWith('staging') || MODE === 'goalstaging') {
    for (const text of ['FOLLOWUP one', 'FOLLOWUP two']) {
      target.__msg.value = text;
      await target.send();
    }
    target.__msg.value = '';
    target.S.pendingFiles = [NEWER_FILE];
    target.__model = 'm2';
  } else if (MODE === 'composerfiles') {
    target.__msg.value = '';
    target.S.pendingFiles = [NEWER_FILE];
  } else if (MODE === 'queuefiles' || MODE === 'ownerqueuefiles-switch') {
    target.__msg.value = 'FOLLOWUP with file';
    target.S.pendingFiles = [{name:'followup.txt', size:32, type:'text/plain'}];
    await target.send();
    target.__msg.value = '';
  }
  if (MODE === 'switch' || MODE === 'foreignfiles' || MODE === 'ownerqueuefiles-switch') {
    // The user opens another session that is still running and has its own queue.
    target.S.session = { session_id:'sid-2', model:'m', model_provider:'p', workspace:null, profile:null };
    target.S.messages = [{ role:'user', content:'B question' }];
    target.S.busy = true;
    target.queueSessionMessage('sid-2', { text:'B queued', files:[] });
    target.__msg.value = '';
    if (MODE === 'foreignfiles') {
      target.S.pendingFiles = [{name:'B-composer.txt'}];
      target.queueSessionMessage('sid-2', {text:'B file queue', files:[{name:'B-queue.txt'}]});
    }
  } else if (MODE === 'queue' || MODE.startsWith('reload-')) {
    for (const text of ['FOLLOWUP one', 'FOLLOWUP two']) {
      target.__msg.value = text;
      await target.send();
      target.__msg.value = '';
    }
    if (MODE.startsWith('reload-typing')) {
      target.__msg.value = 'NEWER draft';
      target.__model = 'm2';
    }
  }
  HOLD();
  try { await first; } catch (e) { console.log('SEND_THREW ' + e.message); }
  console.log('SAVES_BEFORE_CLEAR=' + SAVED.length);
  if (MODE === 'staging-switch') {
    // Navigation saves the newer draft before the held send-time clear settles.
    target._saveComposerDraftNow('sid-1', target.__msg.value, target.S.pendingFiles);
    target.S.session = {session_id:'sid-2'};
    target.__msg.value = 'B draft';
    target.S.pendingFiles = [];
  }
  FINISH_CLEAR();
  await tick(2);
  if (MODE.startsWith('reload-')) {
    // A fresh page has no live File objects, queue, or composer. Retain only
    // serialized storage and the draft acknowledged by the API boundary.
    target.SESSION_QUEUES = {};
    target.__msg.value = '';
    target.S.pendingFiles = [];
    target.S.session.composer_draft = JSON.parse(JSON.stringify(DRAFTS.get('sid-1')));
    target.__restoreQueue('sid-1');
    // loadSession starts the asynchronous status probe BEFORE restoring draft.
    const resumed = target.resumeManualCompressionForSession('sid-1');
    target._restoreComposerDraft(target.S.session.composer_draft, 'sid-1');
    await tick(2);
    if (MODE.startsWith('reload-running')) {
      if (!COMPLETE_COMPRESSION || !target.S.busy) throw new Error('compression never resumed');
      console.log('RESUMED=' + JSON.stringify(compressionSnapshot()));
      if (MODE === 'reload-running-switch') {
        target.S.session = {session_id:'sid-2'};
        target.__msg.value = 'B draft';
        target.queueSessionMessage('sid-2', {text:'B queued', files:[]});
      }
      COMPLETE_COMPRESSION();
    }
    await resumed;
    console.log('SETTLED=' + JSON.stringify(compressionSnapshot()));
  }
  // Let setBusy's 120 ms queue drain (and anything it sends) run its course.
  await new Promise(r => setTimeout(r, 600));
  await tick(50);
  if (PROBE_STATUS) {
    console.log('BEFORE_EXPLICIT=' + JSON.stringify({
      ...compressionSnapshot(), composer:target.__msg.value, starts:POSTED.length,
      queue:target._getSessionQueue('sid-1', false).map(e => e.text),
      persisted:target._readPersistedSessionQueue('sid-1').map(e => e.text),
    }));
    await target.send();
    FINISH_CLEAR();
    await new Promise(r => setTimeout(r, 600));
    await tick(2);
  }
  report();
})().catch(e => console.log('HARNESS_ERR ' + (e && e.stack || e)));
"""


def _run(tmp_path: Path, mode: str, messages_src: str = MESSAGES_SRC, ui_src: str = UI_SRC) -> dict:
    queue_start = SESSIONS_SRC.index("    // Restore any queued message that survived page refresh or tab restore.")
    queue_end = SESSIONS_SRC.index("    // Reconstruct tool calls from message metadata", queue_start)
    src = "\n".join([
        *(_extract_function(ui_src, name) for name in (
            "_getSessionQueue", "_queueStorageKey", "_clearPersistedSessionQueue",
            "_persistSessionQueueStorage", "_readPersistedSessionQueue",
            "queueSessionMessage", "shiftQueuedSessionMessage", "getQueuedSessionCount",
            "_compressionSessionLock", "_setCompressionSessionLock",
            "_compressionStateForCurrentSession", "isCompressionUiRunning",
            "setCompressionUi", "clearCompressionUi", "_restoreCompressionPlaceholder",
            "_clearCompressionElapsedTimer")),
        "function __restoreQueue(sid){\n" + SESSIONS_SRC[queue_start:queue_end] + "\n}",
        _extract_function(SESSIONS_SRC, "_restoreComposerDraft"),
        _extract_function(SESSIONS_SRC, "_sessionProfileMismatchFromError"),
        _extract_function(COMMANDS_SRC, "resumeManualCompressionForSession"),
        _extract_function(COMMANDS_SRC, "_applyManualCompressionResult"),
        _extract_function(messages_src, "_restoreComposerDraftAfterFailedSend"),
        _extract_function(messages_src, "_isStaleClientBundleError"),
        *(_extract_function(messages_src, name) for name in (
            "_normalizeGoalContinuationId", "_setRestoredGoalContinuationDraft",
            "_clearRestoredGoalContinuationDraft", "_takeRestoredDraftGoalContinuationId",
            "_recoverCompressedSend")),
        _extract_function(ui_src, "setBusy"),
        _extract_function(messages_src, "send"),
    ])
    script = _HARNESS.replace("__SRC__", json.dumps(src)).replace("__MODE__", json.dumps(mode))
    path = tmp_path / f"harness_{mode}.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=120)
    out = proc.stdout
    assert "HARNESS_ERR" not in out and "HOLD_FAILED" not in out and "SEND_THREW" not in out, out + proc.stderr
    # Some Node builds abort (SIGABRT) during teardown after printing everything;
    # accept that only when every probe line was printed.
    assert proc.returncode == 0 or (proc.returncode == -6 and "RELOADED=" in out), proc.stderr[:2000] or out
    fields = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isupper():
            fields[key] = json.loads(value)
    assert "RELOADED" in fields, out
    return fields


def _pre_fix(messages_src: str, ui_src: str) -> tuple[str, str]:
    """The branch shape the 2026-10-19 re-gate tested: bubble removed before the
    restore, setBusy(false) drains the queue, one status for every case."""
    i = messages_src.index("if(e&&e.status===409&&_isStaleClientBundleError(e)){")
    anchor = "stopClarifyPollingForSession(activeSid);\n"
    start = messages_src.index(anchor, i) + len(anchor)
    end = messages_src.index("      if(typeof renderSessionList==='function') void renderSessionList();\n", start)
    old_branch = (
        "      removeThinking();\n"
        "      if((S.session&&S.session.session_id)===activeSid){\n"
        "        S.messages=S.messages.filter(m=>m!==userMsg);\n"
        "        if(typeof renderMessages==='function') renderMessages();\n"
        "      }\n"
        "      setBusy(false);\n"
        "      _restoreComposerDraftAfterFailedSend(_failedSendDraftText, _failedSendFilesSnapshot, activeSid, _composerDraftClearPromise);\n"
        "      showToast(t('stale_bundle_refresh'),8000);\n"
        "      setComposerStatus(t('stale_bundle_refresh'));\n"
    )
    old_messages = messages_src[:start] + old_branch + messages_src[end:]
    old_ui = ui_src.replace("    if(opts&&opts.drain===false) return;\n", "", 1)
    assert old_messages != messages_src and old_ui != ui_src, "could not synthesize the pre-fix shape"
    return old_messages, old_ui


def test_typing_during_the_send_keeps_the_rejected_message(tmp_path):
    """Re-gate case 1: newer typing keeps the composer, so the rejected message
    goes back on the queue, ahead of anything else, with its attachment."""
    r = _run(tmp_path, "typing")
    assert r["COMPOSER"] == "NEWER draft", r  # newer typing is never clobbered
    assert r["QUEUE_HEAD"] == {"text": "ORIGINAL message", "files": ["original.txt"], "contId": "", "model": "m"}, r  # posted model
    assert r["TRANSCRIPT"] == ["user:earlier question", "assistant:earlier answer"], r
    assert r["STARTS"] == 1 and r["RELOADED"] is False, r


def test_a_goal_continuation_blocked_by_newer_typing_is_queued_with_its_id(tmp_path):
    """Codex thread on 53dd9a0: a continuation rejected while the user types a
    new draft must keep its ID (the server never consumed it)."""
    r = _run(tmp_path, "goaltyping")
    assert r["COMPOSER"] == "NEWER draft", r
    assert r["QUEUE_HEAD"] == {"text": "ORIGINAL message", "files": ["original.txt"], "contId": "0123aaaa", "model": "m"}, r
    old = _run(tmp_path, "goaltyping", *_pre_fix(MESSAGES_SRC, UI_SRC))
    assert old["QUEUE"] == [] and all("contId" not in x for x in old["SAVED"]), (
        f"harness did not reproduce the dropped continuation: {old}")


def test_typing_gate_reproduces_the_lost_message_on_the_pre_fix_shape(tmp_path):
    r = _run(tmp_path, "typing", *_pre_fix(MESSAGES_SRC, UI_SRC))
    reachable = [m for m in r["TRANSCRIPT"] + [r["COMPOSER"]] + [s["text"] for s in r["SAVED"]]
                 if "ORIGINAL message" in m]
    assert reachable == [], f"harness did not reproduce the reviewer's loss: {r}"


def test_queued_follow_ups_stay_queued_and_the_original_is_restored(tmp_path):
    """Re-gate case 2: every queued follow-up would hit the same 409, so the
    queue must not drain into the restored composer."""
    r = _run(tmp_path, "queue")
    assert r["STARTS"] == 1, r
    assert r["COMPOSER"] == "ORIGINAL message", r
    assert r["TRAY"] == ["original.txt"], r
    assert r["QUEUE"] == ["FOLLOWUP one", "FOLLOWUP two"], r
    assert not any("ORIGINAL message" in m for m in r["TRANSCRIPT"]), r  # restored, so the bubble goes


def test_queue_gate_reproduces_the_drain_on_the_pre_fix_shape(tmp_path):
    r = _run(tmp_path, "queue", *_pre_fix(MESSAGES_SRC, UI_SRC))
    assert r["STARTS"] == 3 and r["QUEUE"] == [] and r["COMPOSER"] == "FOLLOWUP two", (
        f"harness did not reproduce the reviewer's queue collapse: {r}")


def test_rejected_attachments_get_the_re_attach_instruction(tmp_path):
    """Re-gate case 3: a reload restores the draft text but not the File objects,
    so a rejected send with attachments says they must be re-added."""
    r = _run(tmp_path, "files")
    assert r["COMPOSER"] == "ORIGINAL message" and r["TRAY"] == ["original.txt"], r
    assert r["STATUS"] == "stale_bundle_refresh_files", r
    assert r["SAVED"] and r["SAVED"][-1]["text"] == "ORIGINAL message", r  # text survives the reload
    plain = _run(tmp_path, "nofiles")
    assert plain["STATUS"] == "stale_bundle_refresh", plain
    assert not any("ORIGINAL message" in m for m in plain["TRANSCRIPT"]), plain
    assert plain["TRANSCRIPT"] == ["user:earlier question", "assistant:earlier answer"], plain


def test_switching_away_leaves_the_visible_session_alone(tmp_path):
    """Codex thread on dcc62d6c: a 409 for session A that lands while session B
    is shown must not clear B's busy state, drain B's queue or touch B's
    transcript; A's original is persisted as A's draft instead."""
    r = _run(tmp_path, "switch")
    assert r["BUSY"] is True, r
    assert r["QUEUE"] == ["B queued"] and r["STARTS"] == 1, r
    assert r["TRANSCRIPT"] == ["user:B question"], r
    assert {"sid": "sid-1", "text": "ORIGINAL message", "files": ["original.txt"]} in r["SAVED"], r


def test_switch_gate_reproduces_the_cross_session_cleanup_on_the_pre_fix_shape(tmp_path):
    r = _run(tmp_path, "switch", *_pre_fix(MESSAGES_SRC, UI_SRC))
    assert r["BUSY"] is False or r["QUEUE"] != ["B queued"], (
        f"harness did not reproduce the cross-session cleanup: {r}")


def test_a_goal_continuation_keeps_its_id_through_the_stale_restore(tmp_path):
    """Codex thread on a079e29: the server rejects before consuming the pending
    continuation, so the restored draft must be persisted WITH its ID (#7855) or
    the resend after the reload is scored as an ordinary turn."""
    r = _run(tmp_path, "goal")
    assert r["COMPOSER"] == "ORIGINAL message", r
    assert r["SAVED"] and r["SAVED"][-1] == {
        "sid": "sid-1", "text": "ORIGINAL message", "files": ["original.txt"], "contId": "0123aaaa"}, r


def test_goal_gate_reproduces_the_dropped_id_on_the_pre_fix_shape(tmp_path):
    r = _run(tmp_path, "goal", *_pre_fix(MESSAGES_SRC, UI_SRC))
    assert all("contId" not in s for s in r["SAVED"]), f"harness did not reproduce the dropped ID: {r}"


def test_new_stale_bundle_strings_exist_in_every_locale():
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    locale_blocks = i18n.count("regen_failed:")
    assert locale_blocks >= 15
    assert i18n.count("stale_bundle_refresh_files:") == locale_blocks


@pytest.mark.parametrize("mode", ["staging", "goalstaging"])
def test_newer_attachment_only_input_is_not_replaced(tmp_path, mode):
    r = _run(tmp_path, mode)
    assert r["TRAY"] == ["newer.txt"], r
    assert r["ORIGINAL_FILE_RETAINED"] and r["NEWER_FILE_RETAINED"], r
    assert r["SAVES_BEFORE_CLEAR"] == 0, r
    assert r["COMPOSER"] == "", r
    assert r["QUEUE"] == r["PERSISTED"] == ["ORIGINAL message", "FOLLOWUP one", "FOLLOWUP two"], r
    assert r["QUEUE_HEAD"] == {
        "text": "ORIGINAL message", "files": ["original.txt"], "model": "m",
        "contId": "0123aaaa" if mode == "goalstaging" else "",
    }, r
    assert r["SAVED"][-1] == {"sid": "sid-1", "text": "", "files": ["newer.txt"]}, r
    assert r["STARTS"] == 1 and r["BUSY"] is False, r
    assert r["TRANSCRIPT"] == ["user:earlier question", "assistant:earlier answer"], r


@pytest.mark.parametrize("mode", ["queuefiles", "composerfiles", "ownerqueuefiles-switch"])
def test_reload_warning_includes_the_owners_other_files(tmp_path, mode):
    r = _run(tmp_path, mode)
    assert r["STATUS"] == "stale_bundle_refresh_files", r
    assert r["TOASTS"][-1] == "stale_bundle_refresh_files", r
    assert r["STARTS"] == 1, r
    if mode == "queuefiles":
        assert r["COMPOSER"] == "ORIGINAL message", r
        assert r["QUEUE_HEAD"]["files"] == ["followup.txt"], r
    elif mode == "composerfiles":
        assert r["COMPOSER"] == "" and r["TRAY"] == ["newer.txt"], r
        assert r["QUEUE_HEAD"]["text"] == "ORIGINAL message", r
    else:
        assert r["BUSY"] is True and r["QUEUE"] == ["B queued"], r
        assert r["PERSISTED"] == ["FOLLOWUP with file"], r


def test_other_sessions_files_do_not_change_the_reload_warning(tmp_path):
    r = _run(tmp_path, "foreignfiles")
    assert r["STATUS"] == "stale_bundle_refresh", r
    assert r["TRAY"] == ["B-composer.txt"], r
    assert r["BUSY"] is True and r["STARTS"] == 1, r
    assert r["QUEUE"] == ["B queued", "B file queue"], r


@pytest.mark.parametrize("caller", ["ordinary", "rotation"])
@pytest.mark.parametrize("newer", ["empty", "files", "text"])
def test_legacy_recovery_callers_keep_their_baseline_behavior(tmp_path, caller, newer):
    """Scope control, NOT a safety claim: legacy file conflicts still overwrite
    the newer attachment. Neither caller opts into stale-only protection.
    The separate legacy newer-text loss is also unchanged and out of scope.
    """
    r = _run(tmp_path, f"{caller}-{newer}")
    sid = "child" if caller == "rotation" else "sid-1"
    assert r["SESSION"] == sid and r["STARTS"] == 1, r
    assert r["QUEUE"] == r["PERSISTED"] == [], r
    assert r["SAVES_BEFORE_CLEAR"] == 0, r
    if newer == "text":
        assert r["COMPOSER"] == "NEWER draft" and r["TRAY"] == [], r
        assert r["SAVED"] == [{"sid": sid, "text": "NEWER draft", "files": []}], r
        assert not r["ORIGINAL_FILE_RETAINED"], r
    else:
        assert r["COMPOSER"] == "ORIGINAL message" and r["TRAY"] == ["original.txt"], r
        assert r["SAVED"] == [{"sid": sid, "text": "ORIGINAL message",
                               "files": ["original.txt"], "contId": "0123aaaa"}], r
        assert r["ORIGINAL_FILE_RETAINED"] and not r["NEWER_FILE_RETAINED"], r


def test_stale_newer_file_navigation_does_not_persist_the_rejected_snapshot(tmp_path):
    r = _run(tmp_path, "staging-switch")
    assert r["SAVED"] == [{"sid": "sid-1", "text": "", "files": ["newer.txt"]}], r
    assert r["PERSISTED"] == ["ORIGINAL message", "FOLLOWUP one", "FOLLOWUP two"], r
    assert r["OWNER_QUEUE_FILES"] == [["original.txt"], [], []], r
    assert r["COMPOSER"] == "B draft" and r["STARTS"] == 1, r


@pytest.mark.parametrize("mode", ["reload-idle", "reload-error", "reload-typing-idle", "reload-typing-error"])
def test_reload_status_probe_does_not_drain_restored_queue(tmp_path, mode):
    r = _run(tmp_path, mode)
    assert r["STARTS"] == 1, r
    newer = mode.startswith("reload-typing")
    assert r["COMPOSER"] == ("NEWER draft" if newer else "ORIGINAL message"), r
    expected_queue = (["ORIGINAL message"] if newer else []) + ["FOLLOWUP one", "FOLLOWUP two"]
    assert r["QUEUE"] == r["PERSISTED"] == expected_queue, r
    if newer:
        assert r["QUEUE_HEAD"] == {"text": "ORIGINAL message", "files": [], "model": "m", "contId": "0123aaaa"}, r
    assert r["BUSY"] is False and r["APPLIED"] == [], r


@pytest.mark.parametrize("mode", ["reload-running", "reload-running-error"])
def test_real_resumed_compression_still_settles_and_drains(tmp_path, mode):
    r = _run(tmp_path, mode)
    assert r["RESUMED"] == {"busy": True, "lock": "sid-1", "running": True, "phase": "running"}, r
    assert r["SETTLED"] == {
        "busy": False, "lock": None, "running": False,
        "phase": "error" if mode.endswith("-error") else "done",
    }, r
    assert r["STARTS"] == 2, r  # completion drains one; the next 409 pauses it
    assert r["COMPOSER"] == "FOLLOWUP one", r
    assert r["QUEUE"] == r["PERSISTED"] == ["FOLLOWUP two"], r
    assert r["BUSY"] is False, r
    assert r["APPLIED"] == (["done"] if mode == "reload-running" else []), r


def test_resumed_compression_does_not_settle_a_different_session(tmp_path):
    r = _run(tmp_path, "reload-running-switch")
    assert r["STARTS"] == 1 and r["BUSY"] is True, r
    assert r["COMPOSER"] == "B draft" and r["QUEUE"] == ["B queued"], r
    assert r["PERSISTED"] == ["FOLLOWUP one", "FOLLOWUP two"], r
    assert r["APPLIED"] == [], r


@pytest.mark.parametrize("status", [403, 429, 400, 409, 404, 503])
@pytest.mark.parametrize("newer", [False, True], ids=["restored-original", "newer-draft"])
def test_initial_probe_error_keeps_recovery_unlocked_for_explicit_send(tmp_path, status, newer):
    """A failed probe did not resume a job: no error-card lock or queue settlement.

    The 409 is not session_profile_mismatch; 404/503 keep their ignored behavior.
    Use the real UI lock, queue, settlement and send functions, not a no-op proxy.
    """
    r = _run(tmp_path, f"reload-{'typing-' if newer else ''}probe-{status}")
    draft = "NEWER draft" if newer else "ORIGINAL message"
    queue = (["ORIGINAL message"] if newer else []) + ["FOLLOWUP one", "FOLLOWUP two"]
    assert r["BEFORE_EXPLICIT"] == {
        "busy": False, "lock": None, "running": False, "phase": None,
        "composer": draft, "starts": 1, "queue": queue, "persisted": queue,
    }, r
    assert r["STARTS"] == 2, r
    assert r["POSTED_TEXTS"] == [
        "ORIGINAL message\n\n[Attached files: /attachments/sid-1/original.txt]", draft,
    ], r
    assert r["QUEUE"] == r["PERSISTED"] == queue, r
    assert r["COMPOSER"] == draft and r["BUSY"] is False, r
    assert r["COMPRESSION"] == {"busy": False, "lock": None, "running": False, "phase": None}, r
