from services.memory import memory_extractor


def test_conflicting_name_is_detected():
    existing = [
        {"text": "User's name is Ed.", "category": "identity", "pinned": True},
    ]
    assert memory_extractor._identity_conflicts(
        "User's name is Thiermen Naaij.", existing
    )


def test_same_name_is_not_a_conflict():
    existing = [
        {"text": "User's name is Ed.", "category": "identity", "pinned": True},
    ]
    assert not memory_extractor._identity_conflicts("User's name is Ed.", existing)


def test_unpinned_existing_entry_does_not_trigger_conflict():
    existing = [
        {"text": "User's name is Ed.", "category": "identity", "pinned": False},
    ]
    assert not memory_extractor._identity_conflicts(
        "User's name is Thiermen Naaij.", existing
    )


def test_non_name_identity_fact_is_never_a_conflict():
    existing = [
        {"text": "User's name is Ed.", "category": "identity", "pinned": True},
    ]
    assert not memory_extractor._identity_conflicts("User lives in Utrecht.", existing)


def test_no_existing_identity_entries_is_not_a_conflict():
    assert not memory_extractor._identity_conflicts("User's name is Ed.", [])


def test_extract_name_value_handles_reversed_llm_phrasing():
    """Regression: the extraction LLM paraphrases facts, and does not always
    use the "name is X" word order the original patterns expected. This is
    the EXACT wording from issue #101 ('Goedemiddag Thiermen Naaij.' got
    extracted as this fact) — before this fix _extract_name_value returned
    None for it, so _identity_conflicts never fired even with a conflicting
    pinned name already stored."""
    assert (
        memory_extractor._extract_name_value("Thiermen Naaij is the user's full name")
        == "thiermen naaij"
    )


def test_identity_conflict_fires_on_issues_exact_fact_text():
    existing = [
        {"text": "User's name is Ed.", "category": "identity", "pinned": True},
    ]
    assert memory_extractor._identity_conflicts(
        "Thiermen Naaij is the user's full name", existing
    )


def test_self_stated_english_is_detected():
    messages = [{"role": "user", "content": "My name is Ed."}]
    assert memory_extractor._has_self_stated_identity(messages)


def test_self_stated_dutch_is_detected():
    messages = [{"role": "user", "content": "Ik ben Ed."}]
    assert memory_extractor._has_self_stated_identity(messages)


def test_vocative_greeting_is_not_self_stated():
    """The issue's exact repro: a greeting with a name in vocative position
    is not a self-statement."""
    messages = [{"role": "user", "content": "Goedemiddag Thiermen Naaij."}]
    assert not memory_extractor._has_self_stated_identity(messages)


def test_assistant_messages_are_not_considered():
    messages = [
        {"role": "assistant", "content": "My name is Assistant, how can I help?"},
    ]
    assert not memory_extractor._has_self_stated_identity(messages)
