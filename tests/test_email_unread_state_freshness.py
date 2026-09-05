"""Tests for the /api/email/unread-state live-count + in-memory TTL cache.

#119: `email_message_index` only ever holds messages the mail-list UI has
paged through, so counting UNSEEN rows *within* that partial subset
structurally undercounts on a large mailbox (a live 1143-row index showed
907 unread while the real INBOX had 2140+). `unread_state` now always
counts live via UID SEARCH UNSEEN and caches that count in a module-level
in-memory dict for `_UNREAD_STATE_TTL_S`, so the dashboard poll still costs
at most one IMAP round-trip per account/folder per TTL window. On IMAP
failure it falls back to the last known in-memory value (stale, never a
5xx); with no in-memory value yet it falls back once to the old index-COUNT
path; with neither, it returns a zero count.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _insert_index_row(db_path, *, owner, uid, flags, updated_at, account_key="default", folder="INBOX"):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO email_message_index
            (owner, account_key, folder, uid, message_id, subject, flags, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (owner, account_key, folder, uid, f"<{uid}@example.com>", f"Subject {uid}", flags, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


class _FakeImapConn:
    """Minimal stand-in for the pooled IMAP connection.

    Only `.select()` is implemented — `_list_emails_sync` uses the
    module-level `_imap_uid_search`/`_imap_uid_fetch` helpers (which the
    tests monkeypatch directly) rather than calling search/fetch on the
    connection object itself.
    """

    def select(self, mailbox, readonly=False):
        return "OK", [b"1"]


@pytest.fixture(autouse=True)
def _reset_unread_state_cache():
    """The in-memory unread-count cache is module-level state — clear it
    before and after every test so runs (and other test files exercising
    this route) don't leak counts across owners/accounts/folders."""
    import routes.email_routes as email_routes

    email_routes._unread_state_cache.clear()
    yield
    email_routes._unread_state_cache.clear()


def _setup(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    # Make sure no fixture-email file short-circuits unread-state before it
    # ever reaches the live-count/cache logic under test.
    monkeypatch.setattr(email_routes, "DATA_DIR", str(tmp_path))
    email_helpers._init_scheduled_db()

    router = email_routes.setup_email_routes()
    endpoint = _route_endpoint(router, "/api/email/unread-state", "GET")
    return email_routes, db_path, endpoint


@pytest.mark.asyncio
async def test_first_call_counts_live(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    search_calls = []

    def fake_search(conn, criteria):
        search_calls.append(criteria)
        return "OK", [b"101 102 103"]

    monkeypatch.setattr(email_routes, "_imap_uid_search", fake_search)
    monkeypatch.setattr(email_routes, "_imap_connect", lambda account_id=None, owner="": _FakeImapConn())

    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 3
    assert result["fresh"] is True
    assert result["sync"]["source"] == "live"
    assert search_calls, "expected the live UID SEARCH UNSEEN helper to be invoked"
    assert "UNSEEN" in search_calls[0]


@pytest.mark.asyncio
async def test_second_call_within_ttl_uses_memory_cache_not_imap(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    search_calls = []
    connect_calls = []

    def fake_search(conn, criteria):
        search_calls.append(criteria)
        return "OK", [b"101 102 103"]

    monkeypatch.setattr(email_routes, "_imap_uid_search", fake_search)
    monkeypatch.setattr(
        email_routes, "_imap_connect",
        lambda account_id=None, owner="": connect_calls.append(1) or _FakeImapConn(),
    )

    first = await unread_state(folder="INBOX", account_id=None, owner="alice")
    assert first["sync"]["source"] == "live"
    assert len(connect_calls) == 1

    second = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert second["unread_count"] == first["unread_count"] == 3
    assert second["max_uid"] == first["max_uid"]
    assert second["fresh"] is True
    assert second["sync"]["source"] == "memory"
    # No second IMAP round-trip within the TTL window.
    assert len(connect_calls) == 1
    assert len(search_calls) == 1


@pytest.mark.asyncio
async def test_call_after_ttl_expiry_recounts_live(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    search_calls = []

    def fake_search(conn, criteria):
        search_calls.append(criteria)
        if len(search_calls) == 1:
            return "OK", [b"101 102 103"]
        return "OK", [b"201 202 203 204"]

    monkeypatch.setattr(email_routes, "_imap_uid_search", fake_search)
    monkeypatch.setattr(email_routes, "_imap_connect", lambda account_id=None, owner="": _FakeImapConn())

    first = await unread_state(folder="INBOX", account_id=None, owner="alice")
    assert first["unread_count"] == 3

    # Force the cached entry to look TTL-expired.
    cache_key = next(iter(email_routes._unread_state_cache.keys()))
    count, max_uid, ts = email_routes._unread_state_cache[cache_key]
    email_routes._unread_state_cache[cache_key] = (
        count, max_uid, ts - email_routes._UNREAD_STATE_TTL_S - 30,
    )

    second = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert second["unread_count"] == 4
    assert second["fresh"] is True
    assert second["sync"]["source"] == "live"
    assert len(search_calls) == 2


@pytest.mark.asyncio
async def test_imap_failure_with_prior_memory_value_returns_stale_not_5xx(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    def fake_search(conn, criteria):
        return "OK", [b"101 102"]

    monkeypatch.setattr(email_routes, "_imap_uid_search", fake_search)
    monkeypatch.setattr(email_routes, "_imap_connect", lambda account_id=None, owner="": _FakeImapConn())

    first = await unread_state(folder="INBOX", account_id=None, owner="alice")
    assert first["unread_count"] == 2

    def broken_connect(account_id=None, owner=""):
        raise ConnectionError("IMAP unreachable")

    monkeypatch.setattr(email_routes, "_imap_connect", broken_connect)

    # Expire the cached entry so the route attempts (and fails) a live recount.
    cache_key = next(iter(email_routes._unread_state_cache.keys()))
    count, max_uid, ts = email_routes._unread_state_cache[cache_key]
    email_routes._unread_state_cache[cache_key] = (
        count, max_uid, ts - email_routes._UNREAD_STATE_TTL_S - 30,
    )

    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 2
    assert result["fresh"] is False
    assert result["sync"]["source"] == "memory_stale"


@pytest.mark.asyncio
async def test_imap_failure_with_no_memory_value_falls_back_to_index(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    now_iso = datetime.utcnow().isoformat() + "Z"
    _insert_index_row(db_path, owner="alice", uid="201", flags="", updated_at=now_iso)
    _insert_index_row(db_path, owner="alice", uid="202", flags="\\Seen", updated_at=now_iso)

    def broken_connect(account_id=None, owner=""):
        raise ConnectionError("IMAP unreachable")

    monkeypatch.setattr(email_routes, "_imap_connect", broken_connect)

    # No prior call, so the in-memory cache is empty — must fall back to
    # the old index-COUNT path rather than 5xx-ing.
    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 1
    assert result["sync"]["source"] == "index_fallback"
    assert result["fresh"] is True


@pytest.mark.asyncio
async def test_imap_failure_with_no_memory_and_no_index_returns_zero_not_5xx(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    def broken_connect(account_id=None, owner=""):
        raise ConnectionError("IMAP unreachable")

    monkeypatch.setattr(email_routes, "_imap_connect", broken_connect)

    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 0
    assert result["fresh"] is False
    assert result["sync"]["source"] == "unavailable"


@pytest.mark.asyncio
async def test_unread_state_reads_the_same_account_key_the_mail_ui_writes(tmp_path, monkeypatch):
    """Account-key mismatch fix (kept from #120).

    The mail-list UI (emailLibrary.js `_loadAccounts`) always auto-selects
    and sends an explicit `account_id` on `/api/email/list` — even for a
    single default account, per its own comment ("The 'Default' chip is
    gone"). So `_email_index_upsert` writes index rows under that concrete
    account id, never under the literal "default" bucket that
    `_account_cache_key(None, owner)` used to assume for the no-param
    dashboard-widget call. Without resolving `account_id=None` to the same
    concrete default id `_get_email_config` would hand the mail-list flow,
    the cache key (and the index-fallback lookup) would never line up with
    the rows the mail-list UI actually wrote.
    """
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    now_iso = datetime.utcnow().isoformat() + "Z"
    _insert_index_row(db_path, owner="alice", uid="301", flags="", updated_at=now_iso, account_key="acct-real-default-id")

    monkeypatch.setattr(
        email_routes, "_get_email_config",
        lambda account_id=None, owner="": {"account_id": "acct-real-default-id"},
    )

    def broken_connect(account_id=None, owner=""):
        raise ConnectionError("IMAP unreachable")

    monkeypatch.setattr(email_routes, "_imap_connect", broken_connect)

    # Widget call — no account_id, same as dashboard.js `_renderMail()`.
    # Live IMAP fails and there is no in-memory value yet, so this exercises
    # the index-fallback path — which must resolve to the concrete
    # "acct-real-default-id" bucket, not the literal "default" one.
    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 1
    assert result["sync"]["source"] == "index_fallback"
