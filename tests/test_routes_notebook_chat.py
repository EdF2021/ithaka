"""Strict notebook chat: scoped retrieval, grounding prompt, no-sources branch,
enriched rag_sources, and suppression of memory/web.

A session bound to a notebook must answer ONLY from that notebook's sources.
That means retrieval is notebook-scoped (Chroma filtered on ``notebook_id``,
wider ``k``), the injected context blocks are numbered so the model can cite
them as ``[n]``, and an empty result set produces an explicit refusal
instruction instead of a silently context-free turn — which would otherwise
let the model answer from general knowledge, exactly what a notebook forbids.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import src.chat_processor as cp


def _mk_processor(hits):
    """Build a ChatProcessor with a stubbed rag manager.

    The constructor is a plain attribute assignment (no heavy deps), so the
    real ctor is used — mirroring tests/test_chat_processor_used_memories.py.
    """

    class _RagMgr:
        last = None

        def search(self, q, k=5, owner=None, notebook_id=None):
            self.last = {"k": k, "owner": owner, "notebook_id": notebook_id}
            return hits

    class _PDM:
        pass

    pdm = _PDM()
    pdm.rag_manager = _RagMgr()
    proc = cp.ChatProcessor(memory_manager=None, personal_docs_manager=pdm)
    return proc, pdm.rag_manager


def _system_text(preface):
    return " ".join(m["content"] for m in preface if m["role"] == "system")


def _preface(proc, **kwargs):
    kwargs.setdefault("message", "what is X?")
    kwargs.setdefault("session", None)
    kwargs.setdefault("use_rag", True)
    kwargs.setdefault("use_memory", False)
    kwargs.setdefault("use_skills", False)
    kwargs.setdefault("owner", "ed")
    return proc.build_context_preface(**kwargs)


def test_notebook_search_is_scoped_and_k8():
    hits = [{"document": "chunk text", "similarity": 0.9,
             "metadata": {"filename": "a.pdf", "document_id": "doc-1"}}]
    proc, ragmgr = _mk_processor(hits)
    preface, rag_sources, _, _ = _preface(proc, notebook_id="nb-1")

    assert ragmgr.last == {"k": 8, "owner": "ed", "notebook_id": "nb-1"}
    assert rag_sources[0]["document_id"] == "doc-1"
    assert rag_sources[0]["index"] == 1
    # Existing keys unchanged.
    assert rag_sources[0]["filename"] == "a.pdf"
    assert rag_sources[0]["snippet"] == "chunk text"
    assert rag_sources[0]["similarity"] == 0.9
    assert "ONLY those sources" in _system_text(preface)


def test_notebook_context_blocks_are_numbered_for_citation():
    """Citations only work if the injected blocks carry the same [n] numbers."""
    hits = [
        {"document": "first chunk", "similarity": 0.9,
         "metadata": {"filename": "a.pdf", "document_id": "doc-1"}},
        {"document": "second chunk", "similarity": 0.8,
         "metadata": {"filename": "b.pdf", "document_id": "doc-2"}},
    ]
    proc, _ = _mk_processor(hits)
    preface, rag_sources, _, _ = _preface(proc, notebook_id="nb-1")

    blob = " ".join(m["content"] for m in preface)
    assert "[1] a.pdf\nfirst chunk" in blob
    assert "[2] b.pdf\nsecond chunk" in blob
    # The legacy unnumbered header must NOT survive in notebook mode, or the
    # model has no number to cite.
    assert "[a.pdf]" not in blob
    assert [s["index"] for s in rag_sources] == [1, 2]
    # Retrieved snippets stay out of the system role (KV-cache rule).
    assert "first chunk" not in _system_text(preface)


def test_document_id_degrades_to_none_when_metadata_lacks_it():
    hits = [{"document": "chunk", "similarity": 0.9, "metadata": {"filename": "a.pdf"}}]
    proc, _ = _mk_processor(hits)
    _, rag_sources, _, _ = _preface(proc, notebook_id="nb-1")

    assert rag_sources[0]["document_id"] is None


def test_notebook_empty_results_injects_refusal_prompt():
    proc, _ = _mk_processor([])
    preface, rag_sources, _, _ = _preface(proc, notebook_id="nb-1")

    joined = _system_text(preface)
    assert "do not cover" in joined.lower() or "No notebook source" in joined
    assert rag_sources == []


def test_notebook_below_threshold_results_count_as_no_sources():
    """Hits that lose the similarity filter must trigger the refusal too."""
    hits = [{"document": "chunk", "similarity": 0.01, "metadata": {"filename": "a.pdf"}}]
    proc, _ = _mk_processor(hits)
    preface, rag_sources, _, _ = _preface(proc, notebook_id="nb-1")

    assert rag_sources == []
    assert "No notebook source" in _system_text(preface)


def test_notebook_refusal_not_injected_when_sources_found():
    hits = [{"document": "chunk", "similarity": 0.9, "metadata": {"filename": "a.pdf"}}]
    proc, _ = _mk_processor(hits)
    preface, _, _, _ = _preface(proc, notebook_id="nb-1")

    assert "No notebook source" not in _system_text(preface)


def test_notebook_refusal_fires_when_rag_disabled_for_the_turn():
    """A notebook turn that skips retrieval entirely must still refuse, not
    fall back to general knowledge."""
    proc, ragmgr = _mk_processor([{"document": "x", "similarity": 0.9,
                                   "metadata": {"filename": "a.pdf"}}])
    preface, rag_sources, _, _ = _preface(proc, notebook_id="nb-1", use_rag=False)

    assert ragmgr.last is None
    assert rag_sources == []
    assert "No notebook source" in _system_text(preface)


def test_non_notebook_path_unchanged():
    """Ordinary RAG chat must be byte-for-byte what it was before notebooks:
    same k, no notebook filter, no injected prompts, and — the easy one to
    miss — the legacy unnumbered "[filename]" block header, since changing
    that text would invalidate the KV-cache prefix of every existing chat."""
    hits = [{"document": "chunk", "similarity": 0.9, "metadata": {"filename": "a"}}]
    proc, ragmgr = _mk_processor(hits)
    preface, rag_sources, _, _ = _preface(proc, message="q")

    assert ragmgr.last["notebook_id"] is None
    assert ragmgr.last["k"] == 5
    joined = _system_text(preface)
    assert "ONLY those sources" not in joined
    assert "No notebook source" not in joined

    blob = " ".join(m["content"] for m in preface)
    assert "Relevant documents:\n\n[a]\nchunk" in blob
    assert "[1]" not in blob
    # Enrichment is harmless outside notebooks but must not drop existing keys.
    assert set(rag_sources[0]) >= {"filename", "snippet", "similarity"}


def test_non_notebook_empty_results_stay_silent():
    proc, _ = _mk_processor([])
    preface, rag_sources, _, _ = _preface(proc)

    assert rag_sources == []
    assert "No notebook source" not in _system_text(preface)


# --- session binding --------------------------------------------------------

def test_session_notebook_id_helper():
    """Exported for the tool-lockdown task; must tolerate a missing attribute."""
    from routes.chat_helpers import _session_notebook_id

    class _S:
        pass

    s = _S()
    assert _session_notebook_id(s) is None      # attribute missing entirely
    s.notebook_id = None
    assert _session_notebook_id(s) is None
    s.notebook_id = ""                          # empty string is not a binding
    assert _session_notebook_id(s) is None
    s.notebook_id = "nb-1"
    assert _session_notebook_id(s) == "nb-1"


def test_session_dataclass_carries_notebook_id():
    """The binding must survive on the in-memory session, not just the DB row."""
    from core.models import Session

    sess = Session(id="s1", name="n", endpoint_url="", model="m", notebook_id="nb-1")
    assert sess.notebook_id == "nb-1"
    assert Session(id="s2", name="n", endpoint_url="", model="m").notebook_id is None


# --- tool lockdown ----------------------------------------------------------
#
# Two independent guards keep tools away from a notebook-bound session:
#   (a) the chat -> agent auto-escalation is vetoed, so chat_mode stays "chat"
#       and the request takes the tool-free chat branch;
#   (b) if such a session reaches the tool-policy block anyway -- the user can
#       still pick agent mode explicitly, which (a) deliberately does not
#       override -- every known tool name is denylisted before the policy is
#       composed.


class _Sess:
    """Minimal stand-in for a chat session row."""

    def __init__(self, notebook_id=None):
        if notebook_id is not None:
            self.notebook_id = notebook_id


def test_notebook_session_never_escalates_to_agent():
    import routes.chat_routes as cr

    assert cr._should_escalate_to_agent(
        _Sess("nb-1"), "chat",
        intent_detected=True, wants_web=True, contextual_followup=True,
    ) is False


def test_normal_session_still_escalates_on_intent():
    import routes.chat_routes as cr

    assert cr._should_escalate_to_agent(_Sess(), "chat", intent_detected=True) is True


def test_normal_session_still_escalates_on_web_and_followup():
    import routes.chat_routes as cr

    assert cr._should_escalate_to_agent(_Sess(), "chat", wants_web=True) is True
    assert cr._should_escalate_to_agent(
        _Sess(), "chat", contextual_followup=True,
    ) is True


def test_escalation_helper_preserves_chat_mode_gate():
    """Only a literal "chat" mode escalates -- unchanged from the inline code."""
    import routes.chat_routes as cr

    assert cr._should_escalate_to_agent(_Sess(), "agent", intent_detected=True) is False
    assert cr._should_escalate_to_agent(_Sess(), "", intent_detected=True) is False
    assert cr._should_escalate_to_agent(_Sess(), "chat") is False


def test_escalation_helper_tolerates_unloaded_session():
    """The first two escalation sites run before the session is loaded."""
    import routes.chat_routes as cr

    assert cr._should_escalate_to_agent(None, "chat", intent_detected=True) is True
    assert cr._should_escalate_to_agent(None, "chat") is False


def test_notebook_lockdown_denylists_every_known_tool():
    import routes.chat_routes as cr
    from src.tool_policy import known_tool_names

    disabled = {"bash"}
    assert cr._notebook_tool_lockdown(_Sess("nb-1"), disabled) is True
    # The set handed to build_effective_tool_policy must cover everything the
    # policy layer can name -- not just a hand-picked shortlist.
    assert known_tool_names() <= disabled
    for tool in ("bash", "python", "web_search", "web_fetch",
                 "read_file", "write_file", "manage_notes"):
        assert tool in disabled


def test_notebook_lockdown_is_a_noop_for_normal_sessions():
    import routes.chat_routes as cr

    disabled = {"bash"}
    assert cr._notebook_tool_lockdown(_Sess(), disabled) is False
    assert disabled == {"bash"}


def test_notebook_lockdown_blocks_through_the_real_policy():
    """End-to-end over the composition the endpoint actually performs."""
    import routes.chat_routes as cr
    from src.tool_policy import build_effective_tool_policy

    disabled = set()
    cr._notebook_tool_lockdown(_Sess("nb-1"), disabled)
    policy = build_effective_tool_policy(
        disabled_tools=disabled, last_user_message="what is X?",
    )
    for tool in ("bash", "web_search", "manage_notes", "generate_image"):
        assert policy.blocks(tool) is True
