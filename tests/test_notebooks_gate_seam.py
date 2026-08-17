"""Real-seam regression test for the Fase 2 notebook-artifact deadlock fix
(issue #8d).

Fase 2 fixed a self-deadlock in src/notebook_artifacts.py: generate_artifact
runs inside its own tracked-foreground POST request, and passes
workload="foreground" to task_llm_call_async so the local-model gate in
src/llm_core.py (_local_model_slot) does not wait on has_foreground_activity()
- which would otherwise never clear, because it is this very request's own
_ACTIVE_REQUESTS entry that keeps it True.

The only existing coverage of that fix
(tests/test_services_notebook_artifacts.py, tests/test_services_notebook_audio.py)
asserts the workload="foreground" kwarg against a *faked*
task_llm_call_async - it proves the argument is passed, not that the real
gate primitive (_local_model_slot in src/llm_core.py) actually honors it and
lets the call through instead of blocking. tests/test_llm_core_model_gate.py
exercises _local_model_slot directly but never through the notebook-artifact
codepath, so it doesn't prove the two are wired together correctly.

This test closes that gap: it runs the real chain
    generate_artifact -> task_llm_call_async -> llm_call_async_with_fallback
    -> llm_call_async -> _local_model_slot
with the real src.interactive_gate.track_interactive_request /
has_foreground_activity (the caller's own request simulated as active
foreground activity), and only fakes:
  - task_endpoint.resolve_task_candidates (so the test doesn't depend on
    admin-configured endpoints), returning one local candidate, and
  - the actual HTTP transport below _local_model_slot
    (llm_core.httpx_post_kimi_aware_async), so no network call happens.

Two assertions:
  1. workload="foreground" (the real generate_artifact codepath) completes
     promptly even while has_foreground_activity() is True - the fix.
  2. The inverse: the same "own request is active foreground activity"
     scenario with workload="background" (i.e. without the fix) genuinely
     waits on the gate's real while-loop in _local_model_slot instead of
     completing - proving the assertion in (1) is actually exercising the
     gate, not a no-op.

Hermetic: no network, temp sqlite via tests.helpers.sqlite_db (same
convention as tests/test_services_notebook_artifacts.py), and all
interactive-gate / llm_core module-level gate state is snapshotted and
restored around each test so this file can't leak state into others.
"""
import asyncio
import uuid

import pytest

import core.database as db
import src.interactive_gate as interactive_gate
import src.llm_core as llm_core
import src.notebook_artifacts as artifacts
import src.task_endpoint as task_endpoint
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)

FAKE_LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"


def make_notebook(session, owner="own", name="Testboek"):
    nb = db.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
    session.add(nb)
    session.commit()
    return nb


def make_source(session, notebook, filename="a.txt", content="brontekst", owner="own"):
    doc = db.Document(id=str(uuid.uuid4()), title=filename, owner=owner,
                       current_content=content)
    session.add(doc)
    session.commit()
    src = db.NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook.id,
                             document_id=doc.id, filename=filename,
                             status="indexed", chunk_count=1)
    session.add(src)
    session.commit()
    return src


class _FakeTransportResponse:
    """Stands in for the httpx.Response the real transport call would return.

    Only the attributes llm_call_async actually reads are implemented.
    """

    def __init__(self, content="# Gegenereerd"):
        self.is_success = True
        self.status_code = 200
        self.text = ""
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


@pytest.fixture(autouse=True)
def _reset_gate_state():
    """Snapshot/restore every module-level global these two gates touch.

    Both src.interactive_gate and src.llm_core keep their gate state as
    plain module globals (not per-test fixtures), so a test that drives the
    real gates must put them back exactly as found - otherwise a failure
    here would leak _ACTIVE_REQUESTS, a held lock, or a stale waiting-count
    into unrelated tests run later in the same process.
    """
    saved_gate = dict(
        active_requests=interactive_gate._ACTIVE_REQUESTS,
        last_activity=interactive_gate._LAST_ACTIVITY,
        last_browser_activity=interactive_gate._LAST_BROWSER_ACTIVITY,
        cond=interactive_gate._COND,
        cond_loop=interactive_gate._COND_LOOP,
    )
    # llm_call_async serves repeat (url, model, messages, ...) calls from this
    # module-global cache before ever reaching _local_model_slot - without
    # snapshotting it too, this file's own re-runs (or another test using the
    # same fake URL/model/messages) could silently skip the transport call
    # and start hitting the cached "# Gegenereerd" response instead.
    saved_response_cache = dict(llm_core._response_cache)
    llm_core._LOCAL_MODEL_WAITING_FOREGROUND = 0
    llm_core._LOCAL_MODEL_CURRENT.clear()
    yield
    interactive_gate._ACTIVE_REQUESTS = saved_gate["active_requests"]
    interactive_gate._LAST_ACTIVITY = saved_gate["last_activity"]
    interactive_gate._LAST_BROWSER_ACTIVITY = saved_gate["last_browser_activity"]
    interactive_gate._COND = saved_gate["cond"]
    interactive_gate._COND_LOOP = saved_gate["cond_loop"]
    llm_core._LOCAL_MODEL_WAITING_FOREGROUND = 0
    llm_core._LOCAL_MODEL_CURRENT.clear()
    llm_core._response_cache.clear()
    llm_core._response_cache.update(saved_response_cache)
    if llm_core._LOCAL_MODEL_LOCK.locked():
        llm_core._LOCAL_MODEL_LOCK.release()


@pytest.fixture(autouse=True)
def _local_transport(monkeypatch):
    """Fake only the actual network call, below _local_model_slot.

    Everything above this (candidate resolution -> fallback wrapper ->
    llm_call_async -> the _local_model_slot gate itself) runs for real.
    Baseline/safety-net stub: the foreground test below re-patches
    httpx_post_kimi_aware_async itself so it can additionally observe gate
    state at call time; the background test never reaches transport at all
    (it times out inside the gate's wait loop before acquiring the slot).
    """
    async def _fake_post(client, url, headers, **kwargs):
        return _FakeTransportResponse()

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", _fake_post)
    monkeypatch.setattr(llm_core, "_local_model_gate_enabled", lambda: True)
    monkeypatch.setattr(interactive_gate, "_enabled", lambda: True)
    # Real is_local_endpoint already returns True for 127.0.0.1 (loopback),
    # but pin it so this test doesn't depend on that classification logic.
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda url: url == FAKE_LOCAL_URL)


@pytest.fixture(autouse=True)
def _fixed_task_candidates(monkeypatch):
    """Stand in for admin-configured endpoint resolution only.

    This is *above* _local_model_slot (it decides which URL/model to call,
    not whether the call may proceed) - task_llm_call_async itself, and
    everything below it including the gate, still runs unmodified.
    """
    def _fake_candidates(**kwargs):
        return [(FAKE_LOCAL_URL, "test-model", {})]

    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", _fake_candidates)


def _seed_generatable_notebook():
    s = _TS()
    nb = make_notebook(s)
    make_source(s, nb)
    return s, nb


async def test_foreground_workload_does_not_deadlock_on_own_active_request(monkeypatch):
    """The Fase 2 fix: generate_artifact's own request counts as foreground
    activity (via track_interactive_request), and its LLM call must still
    complete promptly instead of waiting on that same activity."""
    s, nb = _seed_generatable_notebook()
    try:
        observed_foreground_activity = []
        observed_slot_state = []

        async def _fake_post(client, url, headers, **kwargs):
            # Prove the scenario is real: the gate sees foreground activity
            # from the caller's own request at the moment the call runs.
            observed_foreground_activity.append(interactive_gate.has_foreground_activity())
            # Prove the real _local_model_slot actually took the local-
            # endpoint branch and entered it as "foreground": this dict is
            # only populated inside the slot after acquiring it (llm_core.py
            # _local_model_slot), so it stays empty if the gate short-
            # circuited (e.g. is_local_endpoint not matching target_url).
            observed_slot_state.append(dict(llm_core._LOCAL_MODEL_CURRENT))
            return _FakeTransportResponse()

        # Overrides the autouse _local_transport fixture's stub for this test
        # only, so we can additionally observe has_foreground_activity() at
        # the moment the (fake) transport call runs.
        monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", _fake_post)

        async with interactive_gate.track_interactive_request(
            "/api/notebooks/x/artifacts", "POST"
        ):
            assert interactive_gate.has_foreground_activity() is True

            art = await asyncio.wait_for(
                artifacts.generate_artifact(nb.id, "own", "faq", s),
                timeout=5,
            )

        assert art.kind == "faq"
        doc = s.get(db.Document, art.document_id)
        assert doc.current_content == "# Gegenereerd"
        # The call really did run while foreground activity was active -
        # otherwise this test wouldn't be exercising the fix at all.
        assert observed_foreground_activity == [True]
        # And it really did go through the real _local_model_slot as a
        # foreground call against our fake local endpoint - not a
        # short-circuited yield that never entered the gate at all.
        assert len(observed_slot_state) == 1
        assert observed_slot_state[0].get("workload") == "foreground"
        assert observed_slot_state[0].get("url") == FAKE_LOCAL_URL
    finally:
        s.close()


async def test_background_workload_waits_on_active_foreground_request():
    """Inverse guard: without the fix (workload="background"), the same
    "own request is active foreground activity" scenario must genuinely wait
    on _local_model_slot's real background while-loop instead of completing.

    This proves assertion (1) above is actually exercising the gate: if the
    gate were a no-op, this call would also complete immediately.
    """
    async def _acquire_background_slot():
        async with llm_core._local_model_slot(FAKE_LOCAL_URL, "test-model", workload="background"):
            pass  # pragma: no cover - must not be reached within the timeout

    async with interactive_gate.track_interactive_request(
        "/api/notebooks/x/artifacts", "POST"
    ):
        assert interactive_gate.has_foreground_activity() is True
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_acquire_background_slot(), timeout=1.0)

    # No lock/leak left behind: wait_for cancels the inner coroutine on
    # timeout, before it ever reaches (and acquires) the actual slot lock.
    assert not llm_core._LOCAL_MODEL_LOCK.locked()
