"""Stale-only ownership regression; real send, restore and draft persistence.

The start and clear boundaries are held independently. DOM/upload/network are
controlled doubles; save, serialization, local fallback, queue and token logic
are shipped functions. Successful draft POSTs retain file metadata, not File
objects (existing behavior); pending/failed POSTs keep the live owner fallback.
"""

import json

import pytest

from tests import test_8156_stale_bundle_recovery as gate

pytestmark = gate.pytestmark

_PERSISTENCE = "\n".join(gate._extract_function(gate.SESSIONS_SRC, name) for name in (
    "_saveComposerDraftNow", "_composerDraftFilesForPersist",
    "_composerDraftFileSignature", "_composerDraftPayloadSignature",
    "_composerDraftRevision", "_noteComposerDraftPayload", "_bumpComposerDraftRevision",
    "_clearComposerDraftRestoreSuppression", "_isComposerDraftRestoreSuppressed",
    "_composerDraftHasPayload", "_sessionComposerDraftHasPayload",
    "_setComposerDraftLocalIntent", "_postComposerDraftPayload", "_safeDraftSessionId",
    "_parseSessionMovedDraftError", "_composerDraftResponseSessionId",
    "_rememberComposerDraftPayloadState", "_handleComposerDraftPostFailure",
))
_PERSISTENCE += "\n" + gate._extract_function(gate.UI_SRC, "addFiles")
_PERSISTENCE += "\n" + gate._extract_function(gate.UI_SRC, "_pendingFileOrigName")

_SETUP = r"""
Object.assign(target, {
  _draftSaveTimer:null, _composerDraftIntentSequence:0,
  _composerDraftKnownPayloadSessions:new Set(),
  _composerDraftRestoreSuppressedUntilBySid:new Map(),
  _composerDraftRevBySid:new Map(), _composerDraftLastPayloadBySid:new Map(),
  _composerDraftLatestIntentSequenceBySid:new Map(), _composerDraftPostPromisesBySid:new Map(),
  MAX_UPLOAD_BYTES:1000000,
});
"""

_SCENARIO = r"""
const [caller, owner, newer, outcome] = MODE.split('-');
let SETTLE_SAVE = null;
const DRAFT_POSTS = [];
const originalApi = target.api;
target.api = async (url, opts) => {
  if (url !== '/api/session/draft') return originalApi(url, opts);
  const body = JSON.parse(opts.body);
  DRAFT_POSTS.push(body);
  return new Promise((resolve, reject) => {
    SETTLE_SAVE = () => outcome === 'failure'
      ? reject(Object.assign(new Error('draft unavailable'), {status:503}))
      : resolve({session_id:body.session_id});
  });
};
const showNewer = (matchingSid) => {
  target.S.session = owner === 'null' ? null : {session_id:owner === 'foreign' ? 'sid-2' : matchingSid};
  target.__msg.value = newer === 'text' ? 'NEWER draft' : '';
  target.S.pendingFiles = [];
  target.addFiles([NEWER_FILE]);
};
if (caller === 'rotation') target.loadSession = async sid => showNewer(sid);
const snapshot = () => {
  const queue = target._getSessionQueue('sid-1', false);
  return {
    session:target.S.session && target.S.session.session_id,
    text:target.__msg.value, newerFile:target.S.pendingFiles.includes(NEWER_FILE),
    originalVisibleFile:target.S.pendingFiles.includes(ORIGINAL_FILE),
    visibleToken:target.__msg.dataset.goalContinuationId || '',
    queue:queue.map(e => ({text:e.text, originalFile:e.files.includes(ORIGINAL_FILE),
      token:e.goal_continuation_id || '', model:e.model, provider:e.model_provider, profile:e.profile})),
    persistedQueue:target._readPersistedSessionQueue('sid-1').map(e => ({text:e.text, token:e.goal_continuation_id || ''})),
    fallback:[...target._composerDraftLocalFallbackBySid].map(([sid, draft]) => ({sid,
      text:draft.text, originalFile:draft.files.includes(ORIGINAL_FILE), newerFile:draft.files.includes(NEWER_FILE)})),
    posts:DRAFT_POSTS.map(p => ({...p, files:p.files.map(({lastModified, ...file}) => file)})),
    starts:POSTED.length,
  };
};
(async () => {
  target.__msg.value = 'ORIGINAL message';
  target.S.pendingFiles = [ORIGINAL_FILE];
  const first = target.send({goalContinuationId:'0123aaaa'});
  for (let i = 0; i < 500 && !HOLD; i++) await tick(1);
  if (!HOLD || !FINISH_CLEAR) throw new Error('start/clear was not held');
  if (caller !== 'rotation') showNewer('sid-1');
  HOLD();
  await first;
  const beforeClear = snapshot();
  FINISH_CLEAR();
  await tick(10);
  const pendingSave = snapshot();
  if (SETTLE_SAVE) SETTLE_SAVE();
  await tick(10);
  // Exercise the real setBusy timer; rejected originals must not auto-replay.
  await new Promise(r => setTimeout(r, 180));
  console.log('OBSERVATIONS=' + JSON.stringify({beforeClear, pendingSave, settled:snapshot(), posted:POSTED}));
  console.log('RELOADED=' + JSON.stringify(!!target.RELOADED));
})().catch(e => console.log('HARNESS_ERR ' + (e && e.stack || e)));
"""


def _run_owner(tmp_path, monkeypatch, caller, owner, newer="files", outcome="success", source=gate.MESSAGES_SRC):
    # Reuse only the existing controlled browser boundary, not its scenario or
    # save double. Appended function declarations replace the save/serialization
    # stubs before send executes; no production source is rewritten.
    harness = gate._HARNESS[:gate._HARNESS.index("const report = () => {")]
    harness = harness.replace("const SRC  = __SRC__;", "const SRC = __SRC__ + " + json.dumps("\n" + _PERSISTENCE) + ";")
    harness = harness.replace("vm.createContext(ctx);", _SETUP + "\nvm.createContext(ctx);")
    with monkeypatch.context() as patch:
        patch.setattr(gate, "_HARNESS", harness + _SCENARIO)
        result = gate._run(tmp_path, f"{caller}-{owner}-{newer}-{outcome}", messages_src=source)
    observed = result["OBSERVATIONS"]
    (tmp_path / f"observations-{caller}-{owner}-{newer}-{outcome}.json").write_text(json.dumps(observed, indent=2))
    return observed


@pytest.mark.parametrize("owner", ["null", "foreign", "matching"])
@pytest.mark.parametrize("newer", ["files", "text"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_stale_visible_owner_preserves_both_payloads(tmp_path, monkeypatch, owner, newer, outcome):
    r = _run_owner(tmp_path, monkeypatch, "stale", owner, newer, outcome)
    before, pending, settled = (r[k] for k in ("beforeClear", "pendingSave", "settled"))
    expected_text = "NEWER draft" if newer == "text" else ""
    assert before["posts"] == [] and before["fallback"] == [], r
    assert r["posted"][0]["session_id"] == "sid-1", r
    assert r["posted"][0]["goal_continuation_id"] == "0123aaaa", r
    for state in (before, pending, settled):
        assert state["session"] == {"null": None, "foreign": "sid-2", "matching": "sid-1"}[owner], r
        assert state["text"] == expected_text and state["newerFile"], r
        assert not state["originalVisibleFile"] and state["visibleToken"] == "", r
        assert state["starts"] == 1, r
    if owner == "matching":
        assert settled["queue"] == [{"text": "ORIGINAL message", "originalFile": True,
            "token": "0123aaaa", "model": "m", "provider": "p", "profile": "default"}], r
        assert settled["persistedQueue"] == [{"text": "ORIGINAL message", "token": "0123aaaa"}], r
        expected_fallback = {"sid": "sid-1", "text": expected_text, "originalFile": False, "newerFile": True}
        expected_post = {"session_id": "sid-1", "text": expected_text, "goal_continuation_id": "",
                         "files": [{"name": "newer.txt", "path": "", "size": 13, "type": "text/plain"}]}
    else:
        assert settled["queue"] == settled["persistedQueue"] == [], r
        expected_fallback = {"sid": "sid-1", "text": "ORIGINAL message", "originalFile": True, "newerFile": False}
        expected_post = {"session_id": "sid-1", "text": "ORIGINAL message", "goal_continuation_id": "0123aaaa",
                         "files": [{"name": "original.txt", "path": "", "size": 16, "type": "text/plain"}]}
    assert pending["fallback"] == [expected_fallback], r
    assert pending["posts"] == settled["posts"] == [expected_post], r
    assert settled["fallback"] == ([expected_fallback] if outcome == "failure" else []), r
