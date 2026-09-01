"""Tests for the /api/email/unread-state TTL + live-fallback freshness logic.

The local `email_message_index` table is only filled lazily when the mail
list UI is opened, so without a freshness check the dashboard widget could
show an arbitrarily stale unread count. `unread_state` now trusts an index
row only while it's younger than `_UNREAD_STATE_TTL_S`; otherwise it does a
live IMAP UNSEEN recount, falling back to the stale cached value (never a
5xx) if that live check itself fails.
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


def _setup(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    # Make sure no fixture-email file short-circuits unread-state before it
    # ever reaches the index/TTL logic under test.
    monkeypatch.setattr(email_routes, "DATA_DIR", str(tmp_path))
    email_helpers._init_scheduled_db()

    router = email_routes.setup_email_routes()
    endpoint = _route_endpoint(router, "/api/email/unread-state", "GET")
    return email_routes, db_path, endpoint


@pytest.mark.asyncio
async def test_fresh_index_uses_cache_and_skips_imap(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    now_iso = datetime.utcnow().isoformat() + "Z"
    _insert_index_row(db_path, owner="alice", uid="201", flags="", updated_at=now_iso)
    _insert_index_row(db_path, owner="alice", uid="202", flags="\\Seen", updated_at=now_iso)

    search_calls = []
    connect_calls = []
    monkeypatch.setattr(email_routes, "_imap_uid_search", lambda conn, criteria: search_calls.append(criteria) or ("OK", [b""]))
    monkeypatch.setattr(email_routes, "_imap_connect", lambda account_id=None, owner="": connect_calls.append(1) or _FakeImapConn())

    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 1
    assert result["max_uid"] == 201
    assert result["fresh"] is True
    assert result["sync"]["source"] == "index"
    assert search_calls == []
    assert connect_calls == []


@pytest.mark.asyncio
async def test_stale_index_triggers_live_imap_recount(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    stale_iso = (
        datetime.utcnow() - timedelta(seconds=email_routes._UNREAD_STATE_TTL_S + 30)
    ).isoformat() + "Z"
    # Stale cached count says 1 unread; live IMAP will say 3.
    _insert_index_row(db_path, owner="alice", uid="201", flags="", updated_at=stale_iso)
    _insert_index_row(db_path, owner="alice", uid="202", flags="\\Seen", updated_at=stale_iso)

    search_calls = []

    def fake_search(conn, criteria):
        search_calls.append(criteria)
        return "OK", [b"101 102 103"]

    monkeypatch.setattr(email_routes, "_imap_uid_search", fake_search)
    monkeypatch.setattr(email_routes, "_imap_connect", lambda account_id=None, owner="": _FakeImapConn())

    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 3
    assert result["fresh"] is True
    assert result["sync"]["source"] == "imap_fallback"
    assert search_calls, "expected the live UID SEARCH UNSEEN helper to be invoked"
    assert "UNSEEN" in search_calls[0]


@pytest.mark.asyncio
async def test_stale_index_plus_imap_failure_falls_back_to_stale_cache(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    stale_iso = (
        datetime.utcnow() - timedelta(seconds=email_routes._UNREAD_STATE_TTL_S + 30)
    ).isoformat() + "Z"
    _insert_index_row(db_path, owner="alice", uid="201", flags="", updated_at=stale_iso)
    _insert_index_row(db_path, owner="alice", uid="202", flags="\\Seen", updated_at=stale_iso)

    def broken_connect(account_id=None, owner=""):
        raise ConnectionError("IMAP unreachable")

    monkeypatch.setattr(email_routes, "_imap_connect", broken_connect)

    # Should not raise / should not produce a 5xx — must fall back to the
    # stale-but-known index value instead.
    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 1
    assert result["fresh"] is False
    assert result["sync"]["source"] == "index"


@pytest.mark.asyncio
async def test_empty_index_still_goes_straight_to_live_path(tmp_path, monkeypatch):
    email_routes, db_path, unread_state = _setup(tmp_path, monkeypatch)

    search_calls = []

    def fake_search(conn, criteria):
        search_calls.append(criteria)
        return "OK", [b"55"]

    monkeypatch.setattr(email_routes, "_imap_uid_search", fake_search)
    monkeypatch.setattr(email_routes, "_imap_connect", lambda account_id=None, owner="": _FakeImapConn())

    result = await unread_state(folder="INBOX", account_id=None, owner="alice")

    assert result["unread_count"] == 1
    assert result["fresh"] is True
    assert result["sync"]["source"] == "imap_fallback"
    assert search_calls, "empty index must still fall through to the live IMAP path"
