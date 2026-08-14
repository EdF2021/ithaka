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
