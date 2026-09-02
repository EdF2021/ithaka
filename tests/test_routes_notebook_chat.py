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
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import src.chat_processor as cp
from src.notebook_language import DUTCH_OUTPUT_RULE


def _mk_processor(hits):
    """Build a ChatProcessor with a stubbed rag manager.

    The constructor is a plain attribute assignment (no heavy deps), so the
    real ctor is used — mirroring tests/test_chat_processor_used_memories.py.
    """

    class _RagMgr:
        last = None

        def search(self, q, k=5, owner=None, notebook_id=None, source_ids=None):
            self.last = {"k": k, "owner": owner, "notebook_id": notebook_id, "source_ids": source_ids}
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

    assert ragmgr.last == {"k": 8, "owner": "ed", "notebook_id": "nb-1", "source_ids": None}
    assert rag_sources[0]["document_id"] == "doc-1"
    assert rag_sources[0]["index"] == 1
    # Existing keys unchanged.
    assert rag_sources[0]["filename"] == "a.pdf"
    assert rag_sources[0]["snippet"] == "chunk text"
    assert rag_sources[0]["similarity"] == 0.9
    assert "ONLY those sources" in _system_text(preface)


def test_notebook_retrieval_log_carries_notebook_and_source_ids(caplog):
    """#112 diagnosability: the RAG-above-threshold logline must carry
    notebook_id, source_ids and the used query so a prod log alone can show
    whether a turn was source-filtered, without reading request payloads."""
    hits = [{"document": "chunk text", "similarity": 0.9,
             "metadata": {"filename": "a.pdf", "document_id": "doc-1"}}]
    proc, _ = _mk_processor(hits)
    with caplog.at_level("INFO", logger="src.chat_processor"):
        _preface(proc, notebook_id="nb-1", source_ids=["doc-1", "doc-2"])

    rag_lines = [r.message for r in caplog.records if r.message.startswith("RAG:")]
    assert len(rag_lines) == 1
    assert "notebook_id='nb-1'" in rag_lines[0]
    assert "source_ids=['doc-1', 'doc-2']" in rag_lines[0]
    assert "query='what is X?'" in rag_lines[0]


def test_search_hint_anchors_condensation_fallback_on_llm_failure(monkeypatch, caplog):
    """#112 voorstel B: when condensation itself fails, the RAG query must
    fall back to the bare search_hint (e.g. a clicked mindmap node's label),
    not the raw templated message whose generic filler words ("bronnen",
    "notebook", "samenvatting") would otherwise skew the keyword score."""
    def failing_llm(*args, **kwargs):
        raise ValueError("LLM down")
    monkeypatch.setattr("src.llm_core.llm_call", failing_llm)

    hits = [{"document": "chunk", "similarity": 0.9, "metadata": {"filename": "a.pdf"}}]
    proc, _ = _mk_processor(hits)
    session = SimpleNamespace(endpoint_url="http://local", model="test", headers={}, history=[])
    templated_msg = (
        'Geef een samenvatting en uitleg over "Skills/integraties" op basis '
        'van de bronnen van dit notebook.'
    )

    with caplog.at_level("INFO", logger="src.chat_processor"):
        _preface(
            proc, notebook_id="nb-1", session=session,
            message=templated_msg, search_hint="Skills/integraties",
        )

    rag_lines = [r.message for r in caplog.records if r.message.startswith("RAG:")]
    assert len(rag_lines) == 1
    assert "query='Skills/integraties'" in rag_lines[0]


def test_search_hint_anchors_condensation_fallback_on_empty_response(monkeypatch, caplog):
    """Same as above, but condensation returns an empty string rather than
    raising — the other failure mode _condense_notebook_query handles."""
    monkeypatch.setattr("src.llm_core.llm_call", lambda *a, **k: "   ")

    hits = [{"document": "chunk", "similarity": 0.9, "metadata": {"filename": "a.pdf"}}]
    proc, _ = _mk_processor(hits)
    session = SimpleNamespace(endpoint_url="http://local", model="test", headers={}, history=[])
    templated_msg = (
        'Geef een samenvatting en uitleg over "Resultaten" op basis van de '
        'bronnen van dit notebook.'
    )

    with caplog.at_level("INFO", logger="src.chat_processor"):
        _preface(
            proc, notebook_id="nb-1", session=session,
            message=templated_msg, search_hint="Resultaten",
        )

    rag_lines = [r.message for r in caplog.records if r.message.startswith("RAG:")]
    assert len(rag_lines) == 1
    assert "query='Resultaten'" in rag_lines[0]


def test_no_search_hint_falls_back_to_raw_message_unchanged(monkeypatch, caplog):
    """Regression: without a search_hint, condensation failure must still
    fall back to the raw message exactly as before #112."""
    def failing_llm(*args, **kwargs):
        raise ValueError("LLM down")
    monkeypatch.setattr("src.llm_core.llm_call", failing_llm)

    hits = [{"document": "chunk", "similarity": 0.9, "metadata": {"filename": "a.pdf"}}]
    proc, _ = _mk_processor(hits)
    session = SimpleNamespace(endpoint_url="http://local", model="test", headers={}, history=[])

    with caplog.at_level("INFO", logger="src.chat_processor"):
        _preface(proc, notebook_id="nb-1", session=session, message="what is X?")

    rag_lines = [r.message for r in caplog.records if r.message.startswith("RAG:")]
    assert len(rag_lines) == 1
    assert "query='what is X?'" in rag_lines[0]


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


def test_grounding_prompt_carries_dutch_output_rule():
    """Notebook chat must always answer in Dutch, regardless of source or
    question language — enforced via the shared DUTCH_OUTPUT_RULE constant."""
    assert DUTCH_OUTPUT_RULE in cp.NOTEBOOK_GROUNDING_PROMPT


def test_no_sources_prompt_carries_dutch_output_rule():
    """The refusal branch must also stay in Dutch."""
    assert DUTCH_OUTPUT_RULE in cp.NOTEBOOK_NO_SOURCES_PROMPT


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


def test_notebook_veto_reverts_our_own_escalation():
    """The veto -- not the helper -- is what stops the intent/web triggers."""
    import routes.chat_routes as cr
    from src.action_intents import ToolIntent

    intent = ToolIntent(True, "web", "web lookup")
    chat_mode, auto_escalated, tool_intent = cr._apply_notebook_veto(
        _Sess("nb-1"), "agent", True, intent,
    )
    assert chat_mode == "chat"
    assert auto_escalated is False
    # Clearing the intent is load-bearing: a "web" category otherwise sets
    # _explicit_web_intent, which un-disables web_search/web_fetch from the
    # global denylist and force-steers the agent loop toward them.
    assert tool_intent is None


def test_notebook_veto_leaves_a_user_picked_agent_mode_alone():
    import routes.chat_routes as cr
    from src.action_intents import ToolIntent

    intent = ToolIntent(True, "web", "web lookup")
    # auto_escalated is False when the user picked agent mode by hand.
    assert cr._apply_notebook_veto(_Sess("nb-1"), "agent", False, intent) == (
        "agent", False, intent,
    )


def test_notebook_veto_is_a_noop_for_normal_sessions():
    import routes.chat_routes as cr
    from src.action_intents import ToolIntent

    intent = ToolIntent(True, "notes", "add a todo")
    assert cr._apply_notebook_veto(_Sess(), "agent", True, intent) == (
        "agent", True, intent,
    )


def test_notebook_lockdown_blocks_every_known_tool():
    import routes.chat_routes as cr
    from src.tool_policy import build_effective_tool_policy, known_tool_names

    policy = cr._apply_notebook_tool_lockdown(
        _Sess("nb-1"),
        build_effective_tool_policy(last_user_message="what is X?"),
    )
    # Hidden as well as disabled, so the prompt builder drops the schemas --
    # all_disabled_names() is what stream_agent_loop reads.
    assert known_tool_names() <= policy.all_disabled_names()
    assert known_tool_names() <= set(policy.hidden_tools)
    for tool in ("bash", "python", "web_search", "web_fetch",
                 "read_file", "write_file", "manage_notes", "generate_image"):
        assert policy.blocks(tool) is True


def test_notebook_lockdown_blocks_mcp_tools():
    """known_tool_names() cannot enumerate namespaced MCP tools, so a denylist
    alone leaves them callable. block_all_tool_calls is what closes that."""
    import routes.chat_routes as cr
    from src.tool_policy import build_effective_tool_policy

    policy = cr._apply_notebook_tool_lockdown(
        _Sess("nb-1"),
        build_effective_tool_policy(last_user_message="what is X?"),
    )
    assert policy.blocks("mcp__anything__tool") is True
    assert policy.block_all_tool_calls is True
    # ... and the agent loop drops the MCP manager entirely on this flag, so
    # the schemas are never offered in the first place.
    assert policy.disable_mcp is True


def test_notebook_lockdown_keeps_earlier_denylist_and_reasons():
    import routes.chat_routes as cr
    from src.tool_policy import build_effective_tool_policy

    base = build_effective_tool_policy(
        disabled_tools={"send_email"}, last_user_message="what is X?",
    )
    policy = cr._apply_notebook_tool_lockdown(_Sess("nb-1"), base)
    assert "send_email" in policy.all_disabled_names()
    assert policy.reason_for("web_search") == cr._NOTEBOOK_TOOL_REASON


def test_notebook_lockdown_is_a_noop_for_normal_sessions():
    import routes.chat_routes as cr
    from src.tool_policy import build_effective_tool_policy

    base = build_effective_tool_policy(last_user_message="what is X?")
    policy = cr._apply_notebook_tool_lockdown(_Sess(), base)
    assert policy is base
    assert policy.blocks("web_search") is False
    assert policy.blocks("mcp__anything__tool") is False
    assert policy.block_all_tool_calls is False


def test_notebook_lockdown_does_not_claim_guide_only_mode():
    """guide_only injects a directive telling the model the USER forbade tools;
    a notebook turn is not that, so the mode must stay untouched."""
    import routes.chat_routes as cr
    from src.tool_policy import build_effective_tool_policy

    policy = cr._apply_notebook_tool_lockdown(
        _Sess("nb-1"),
        build_effective_tool_policy(last_user_message="what is X?"),
    )
    assert policy.mode == "normal"
