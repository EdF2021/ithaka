"""NotebookArtifact.audio_path + NOTEBOOK_AUDIO_DIR (notebooks Fase 3, Task 2).

Own file per controller ruling: tests/test_services_notebook_audio.py is created in
parallel by Task 1 (TTS layer) and extended by Task 3 (audio module) — colliding on
that file at cherry-pick time is exactly what this split avoids.

Model tests mirror tests/test_services_notebook_artifacts.py's convention: a real,
isolated DB via tests.helpers.sqlite_db.make_temp_sqlite (not the shared
core.database.engine/SessionLocal). The migration test mirrors
tests/test_session_search.py::test_chat_messages_fts_migration_backfills_and_tracks_inserts
(tmp_path + monkeypatch.setattr(cdb, "DATABASE_URL", ...) + raw sqlite3 table_info asserts).
"""
import sqlite3
import uuid

import core.database as db
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)


def make_notebook(session, owner="ed", name="Thesis"):
    nb = db.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
    session.add(nb)
    session.commit()
    return nb


def make_document(session, title="Report", owner="ed", content=""):
    doc = db.Document(id=str(uuid.uuid4()), title=title, owner=owner,
                      current_content=content)
    session.add(doc)
    session.commit()
    return doc


def test_notebook_artifact_audio_path_roundtrip():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id="a-audio", notebook_id=nb.id, document_id=doc.id,
                                  kind="podcast", audio_path="deadbeefdeadbeefdeadbeefdeadbeef.wav")
        s.add(art)
        s.commit()

        reloaded = s.query(db.NotebookArtifact).filter_by(id="a-audio").one()
        assert reloaded.audio_path == "deadbeefdeadbeefdeadbeefdeadbeef.wav"

        d = reloaded.to_dict()
        assert d["audio_path"] == "deadbeefdeadbeefdeadbeefdeadbeef.wav"
    finally:
        s.close()


def test_notebook_artifact_to_dict_audio_path_defaults_to_none():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id="a-text", notebook_id=nb.id, document_id=doc.id, kind="faq")
        s.add(art)
        s.commit()

        d = art.to_dict()
        assert d["audio_path"] is None
    finally:
        s.close()


def test_migrate_add_notebook_artifact_audio_path_column(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE notebook_artifacts (
            id TEXT PRIMARY KEY,
            notebook_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at DATETIME,
            updated_at DATETIME
        );
        INSERT INTO notebook_artifacts(id, notebook_id, document_id, kind)
        VALUES ('a1', 'n1', 'd1', 'faq');
        """
    )
    conn.close()

    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{db_path}")

    conn = sqlite3.connect(db_path)
    try:
        columns_before = [row[1] for row in conn.execute("PRAGMA table_info(notebook_artifacts)")]
    finally:
        conn.close()
    assert "audio_path" not in columns_before

    db._migrate_add_notebook_artifact_audio_path_column()

    conn = sqlite3.connect(db_path)
    try:
        columns_after = [row[1] for row in conn.execute("PRAGMA table_info(notebook_artifacts)")]
        assert "audio_path" in columns_after

        row = conn.execute("SELECT audio_path FROM notebook_artifacts WHERE id = 'a1'").fetchone()
        assert row == (None,)
    finally:
        conn.close()

    # Idempotent: running it again on an already-migrated DB must not raise.
    db._migrate_add_notebook_artifact_audio_path_column()


def test_migrate_add_notebook_artifact_audio_path_column_missing_db_is_noop(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist.db"
    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{missing_path}")

    # No DB file at all yet (fresh install) — must not raise.
    db._migrate_add_notebook_artifact_audio_path_column()


def test_notebook_audio_dir_constant_under_data_dir():
    import os

    from src.constants import DATA_DIR, NOTEBOOK_AUDIO_DIR

    assert NOTEBOOK_AUDIO_DIR == os.path.join(DATA_DIR, "notebook_audio")
