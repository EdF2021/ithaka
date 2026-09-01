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
