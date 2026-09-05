"""Route-level guard for POST /api/session's notebook_id field (#112).

Prod evidence (2026-09-02): a session's RAG lookup logged
notebook_id='undefined' — the Chroma `where` filter then matched nothing
because "undefined" is not a real notebook id, so retrieval silently
returned zero results instead of falling back to unscoped chat. The value
had to have been persisted onto sessions.notebook_id via POST /api/session's
notebook_id form field, since that column is the only source
_session_notebook_id() (routes/chat_helpers.py) ever reads from.

_clean_notebook_id_form_value() is the write-time half of the fix: it
normalises the posted form value the same way _session_notebook_id()
normalises it on read, so a broken client binding degrades to "no notebook"
at both ends, and a warning is logged either way a bad value shows up.
"""
import routes.session_routes as sr


def test_clean_notebook_id_passes_through_a_real_id():
    assert sr._clean_notebook_id_form_value("nb-1") == "nb-1"
    assert sr._clean_notebook_id_form_value("  nb-1  ") == "nb-1"


def test_clean_notebook_id_treats_missing_as_none():
    assert sr._clean_notebook_id_form_value(None) is None
    assert sr._clean_notebook_id_form_value("") is None
    assert sr._clean_notebook_id_form_value("   ") is None


def test_clean_notebook_id_rejects_stringified_undefined_and_null(caplog):
    with caplog.at_level("WARNING", logger="routes.session_routes"):
        for bad in ("undefined", "null", "Undefined", "NULL", "  undefined  "):
            assert sr._clean_notebook_id_form_value(bad) is None, bad

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 5
    assert all("broken client binding" in w for w in warnings)


def test_clean_notebook_id_does_not_swallow_a_real_id_containing_the_word():
    # A real (if oddly named) notebook id must not be caught by the sentinel
    # check — only an exact (trimmed, case-insensitive) match is rejected.
    assert sr._clean_notebook_id_form_value("undefined-behavior-notebook") == (
        "undefined-behavior-notebook"
    )
