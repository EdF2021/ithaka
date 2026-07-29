"""Request-scoped active-document state (src/agent_tools/document_tools.py).

Covers the concurrency contract introduced when the plain module global was
replaced: per-request isolation via the ContextVar holder, round-to-round
visibility through the shared holder object, and the process-wide rescue
pointer used by the chat_routes last-resort doc-injection fallback.
"""
import asyncio

import pytest

import src.agent_tools.document_tools as dt


@pytest.fixture(autouse=True)
def _reset_rescue_pointer():
    # Reset both scopes: an earlier test in the suite may have called
    # set_active_document() in the top-level test context, leaving a holder
    # that every create_task here would share (in production requests always
    # run in their own task, so the base context never carries a holder).
    dt._last_set_document_id = None
    dt._active_state.set(None)
    yield
    dt._last_set_document_id = None
    dt._active_state.set(None)


async def test_concurrent_requests_keep_their_own_active_document():
    seen = {}

    async def request(name, doc_id, delay):
        dt.set_active_document(doc_id)
        # Give the other request time to set its own doc; with a module
        # global this read would observe the other request's value.
        await asyncio.sleep(delay)
        seen[name] = dt.get_active_document()

    await asyncio.gather(
        asyncio.create_task(request("a", "doc-a", 0.03)),
        asyncio.create_task(request("b", "doc-b", 0.0)),
    )
    assert seen == {"a": "doc-a", "b": "doc-b"}


async def test_tool_task_write_is_visible_to_next_round():
    # A doc created by a tool in round 1 must be the active doc for round 2:
    # both tool tasks copy the request context and share the holder object.
    async def turn():
        dt.set_active_document(None)  # prompt build initializes the holder

        async def round_1():
            dt.set_active_document("created-doc")

        await asyncio.create_task(round_1())

        async def round_2():
            return dt.get_active_document()

        return await asyncio.create_task(round_2())

    assert await asyncio.create_task(turn()) == "created-doc"


async def test_rescue_pointer_serves_holderless_contexts():
    async def turn():
        dt.set_active_document("orphan-doc")

    await asyncio.create_task(turn())
    # This context never called set_active_document: no holder, so the
    # chat_routes rescue read falls back to the process-wide pointer.
    assert dt.get_active_document() == "orphan-doc"


async def test_docless_turn_does_not_erase_rescue_pointer():
    async def doc_turn():
        dt.set_active_document("doc-1")

    async def docless_turn():
        dt.set_active_document(None)

    await asyncio.create_task(doc_turn())
    await asyncio.create_task(docless_turn())
    assert dt.get_active_document() == "doc-1"


async def test_clear_active_document_matches_id_and_clears_rescue_pointer():
    async def turn():
        dt.set_active_document("doc-x")
        assert dt.clear_active_document("other-doc") is False
        assert dt.get_active_document() == "doc-x"
        assert dt.clear_active_document("doc-x") is True
        assert dt.get_active_document() is None

    await asyncio.create_task(turn())
    assert dt._last_set_document_id is None


def test_clear_from_foreign_context_clears_rescue_pointer():
    # Detach/delete routes run in their own request context (no holder);
    # clearing must still drop the process-wide pointer (#1160).
    dt._last_set_document_id = "closed-doc"
    assert dt.clear_active_document("closed-doc") is True
    assert dt.get_active_document() is None


async def test_active_model_is_request_scoped():
    seen = {}

    async def request(name, model, delay):
        dt.set_active_model(model)
        await asyncio.sleep(delay)
        seen[name] = dt._get_active_model()

    await asyncio.gather(
        asyncio.create_task(request("a", "model-a", 0.03)),
        asyncio.create_task(request("b", "model-b", 0.0)),
    )
    assert seen == {"a": "model-a", "b": "model-b"}
