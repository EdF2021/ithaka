"""Tests for ChatProcessor.build_context_preface's used_memories return value.

ChatProcessor is a single process-wide instance shared across concurrent
chats. build_context_preface used to stash the memories it injected into the
preface on ``self._last_used_memories`` — an instance attribute two calls
(even for different owners) would overwrite, leaking one owner's injected
memories into another owner's response (routes/chat_helpers.py read it back
via ``getattr(chat_processor, '_last_used_memories', [])``). The fix returns
used_memories as part of the method's return tuple instead of mutating
processor state. This file pins that fix.
"""
from src.chat_processor import ChatProcessor
from src.memory import MemoryManager


class _Docs:
    rag_manager = None


def test_build_context_preface_returns_used_memories_as_fourth_element(tmp_path):
    manager = MemoryManager(str(tmp_path))
    entry = manager.add_entry("User's favorite color is teal.", category="preference", owner="alice")
    entry["pinned"] = True
    manager.save([entry])

    processor = ChatProcessor(memory_manager=manager, personal_docs_manager=_Docs())

    preface, rag_sources, web_sources, used_memories = processor.build_context_preface(
        message="What's my favorite color?",
        session=None,
        owner="alice",
        use_web=False,
        use_rag=False,
        use_memory=True,
        use_skills=False,
    )

    assert rag_sources == []
    assert web_sources == []
    assert used_memories == [
        {"text": "User's favorite color is teal.", "category": "preference", "type": "pinned"}
    ]
    # And it was actually injected into the preface, not just tracked on the side.
    assert any("teal" in (msg.get("content") or "") for msg in preface)


def test_chat_processor_carries_no_last_used_memories_instance_state(tmp_path):
    """Regression guard for the cross-tenant leak: ChatProcessor must not
    stash used memories on ``self`` at all — a single processor instance is
    shared across concurrent requests from different owners."""
    manager = MemoryManager(str(tmp_path))
    entry = manager.add_entry("secret", category="fact", owner="alice")
    entry["pinned"] = True
    manager.save([entry])

    processor = ChatProcessor(memory_manager=manager, personal_docs_manager=_Docs())
    processor.build_context_preface(
        message="hi",
        session=None,
        owner="alice",
        use_web=False,
        use_rag=False,
        use_memory=True,
        use_skills=False,
    )

    assert not hasattr(processor, "_last_used_memories")


def test_sequential_calls_for_different_owners_do_not_leak_used_memories(tmp_path):
    """The bug: two chats sharing one ChatProcessor instance would overwrite
    each other's used_memories via instance state (even cross-owner). Two
    back-to-back calls for different owners must each report only their own
    owner's memories, and a result captured before the second call runs must
    stay intact afterwards (not be silently mutated in place by the later
    call)."""
    manager = MemoryManager(str(tmp_path))
    alice_entry = manager.add_entry("Alice's pet is a cat.", category="fact", owner="alice")
    alice_entry["pinned"] = True
    bob_entry = manager.add_entry("Bob's pet is a dog.", category="fact", owner="bob")
    bob_entry["pinned"] = True
    manager.save([alice_entry, bob_entry])

    processor = ChatProcessor(memory_manager=manager, personal_docs_manager=_Docs())

    _, _, _, used_memories_alice = processor.build_context_preface(
        message="What's my pet?",
        session=None,
        owner="alice",
        use_web=False,
        use_rag=False,
        use_memory=True,
        use_skills=False,
    )

    # Second call, different owner, same processor instance — simulates a
    # second concurrent chat sharing the process-wide ChatProcessor.
    _, _, _, used_memories_bob = processor.build_context_preface(
        message="What's my pet?",
        session=None,
        owner="bob",
        use_web=False,
        use_rag=False,
        use_memory=True,
        use_skills=False,
    )

    assert [m["text"] for m in used_memories_alice] == ["Alice's pet is a cat."]
    assert [m["text"] for m in used_memories_bob] == ["Bob's pet is a dog."]
    # Bob's call must not have mutated the list already handed back to alice's caller.
    assert [m["text"] for m in used_memories_alice] == ["Alice's pet is a cat."]
