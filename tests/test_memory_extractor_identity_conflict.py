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


# --- fix round: findings from the coordinator's independent review of PR #142 ---


def test_fuller_name_is_not_a_conflict_with_pinned_first_name():
    """Finding #2: existing pinned "User's name is Ed." + a later, fuller
    extraction "Ed de Feber is the user's full name" is a first-name vs.
    full-name elaboration of the SAME person, not a second conflicting
    identity."""
    existing = [
        {"text": "User's name is Ed.", "category": "identity", "pinned": True},
    ]
    assert not memory_extractor._identity_conflicts(
        "Ed de Feber is the user's full name", existing
    )


def test_fuller_name_is_not_a_conflict_reverse_direction():
    existing = [
        {"text": "Ed de Feber is the user's full name", "category": "identity", "pinned": True},
    ]
    assert not memory_extractor._identity_conflicts("User's name is Ed.", existing)


def test_genuinely_different_name_still_conflicts_with_fuller_pinned_name():
    """Compatibility must not swallow a truly different name just because
    it happens to be longer."""
    existing = [
        {"text": "Ed de Feber is the user's full name", "category": "identity", "pinned": True},
    ]
    assert memory_extractor._identity_conflicts(
        "Thiermen Naaij is the user's full name", existing
    )


def test_reversed_pattern_does_not_over_capture_leading_user_word():
    """Finding #3: "The user Thiermen Naaij is the user's full name" must
    yield "thiermen naaij", not "the user thiermen naaij" / "user thiermen
    naaij"."""
    assert (
        memory_extractor._extract_name_value(
            "The user Thiermen Naaij is the user's full name"
        )
        == "thiermen naaij"
    )


def test_curly_apostrophe_im_is_self_stated():
    """Finding #4: U+2019 curly apostrophe variant of "I'm"."""
    messages = [{"role": "user", "content": "I’m Ed."}]
    assert memory_extractor._has_self_stated_identity(messages)


def test_dutch_naam_hier_is_self_stated():
    messages = [{"role": "user", "content": "Hallo, Ed hier."}]
    assert memory_extractor._has_self_stated_identity(messages)


def test_dutch_aangenaam_is_self_stated():
    messages = [{"role": "user", "content": "Ed de Feber, aangenaam."}]
    assert memory_extractor._has_self_stated_identity(messages)


def test_dutch_mag_me_noemen_is_self_stated():
    messages = [{"role": "user", "content": "je mag me Ed noemen"}]
    assert memory_extractor._has_self_stated_identity(messages)


def test_english_you_can_call_me_is_self_stated():
    messages = [{"role": "user", "content": "you can call me Ed"}]
    assert memory_extractor._has_self_stated_identity(messages)


def test_mid_sentence_hier_does_not_false_positive():
    """"hier" only counts as the Dutch self-intro idiom when it closes the
    message; a mid-sentence "kom hier" is unrelated."""
    messages = [{"role": "user", "content": "Kom hier alsjeblieft, ik moet iets laten zien."}]
    assert not memory_extractor._has_self_stated_identity(messages)


# --- coordinator finding 0: gate must not depend on parsing a name out of
# fact_text -- a real deployed model (gpt-oss, verified live on :7001) emits
# "Name: Thiermen Naaij" / "Works at CEDA", shapes the old NAME_PATTERNS did
# not cover at all. ---


def test_extract_name_value_handles_name_colon_shape():
    assert memory_extractor._extract_name_value("Name: Thiermen Naaij") == "thiermen naaij"


def test_identity_conflict_fires_on_name_colon_shape():
    existing = [{"text": "Name: Ed", "category": "identity", "pinned": True}]
    assert memory_extractor._identity_conflicts("Name: Thiermen Naaij", existing)


def test_name_colon_fuller_form_is_not_a_conflict():
    existing = [{"text": "Name: Ed", "category": "identity", "pinned": True}]
    assert not memory_extractor._identity_conflicts("Name: Ed de Feber", existing)


def test_ik_werk_is_self_stated():
    messages = [{"role": "user", "content": "Ik werk bij CEDA."}]
    assert memory_extractor._has_self_stated_identity(messages)


def test_i_work_is_self_stated():
    messages = [{"role": "user", "content": "I work at CEDA."}]
    assert memory_extractor._has_self_stated_identity(messages)
