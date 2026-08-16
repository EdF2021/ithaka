"""Notebook/NotebookSource model + sessions.notebook_id column.

Uses the suite's documented convention for a real, isolated DB
(tests.helpers.sqlite_db.make_temp_sqlite, see tests/TESTING_STANDARD.md and
tests/README.md) rather than the shared core.database.engine/SessionLocal.
The shared engine is bound once at collection time (conftest.py imports
core.database with DATABASE_URL=sqlite:///:memory: before any test module
loads, and core.database runs init_db() at import time), so it lives for the
whole pytest session — committing real, un-torn-down rows there would leak
into every other test that queries sessions/notebooks broadly.
"""
import uuid

import core.database as db
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)


def test_notebook_and_source_roundtrip_and_cascade():
    s = _TS()
    try:
        nb = db.Notebook(id=str(uuid.uuid4()), owner="ed", name="Thesis")
        s.add(nb)
        s.commit()
        src = db.NotebookSource(id=str(uuid.uuid4()), notebook_id=nb.id,
                                filename="a.pdf", status="indexed", chunk_count=3)
        s.add(src)
        s.commit()
        d = nb.to_dict()
        assert d["name"] == "Thesis" and d["archived"] is False
        sd = src.to_dict()
        assert sd["status"] == "indexed" and sd["chunk_count"] == 3 and sd["document_id"] is None
        # cascade: deleting the notebook removes its sources
        s.delete(nb)
        s.commit()
        assert s.query(db.NotebookSource).filter_by(notebook_id=nb.id).count() == 0
    finally:
        s.close()


def test_session_to_dict_exposes_notebook_id():
    s = _TS()
    try:
        sess = db.Session(id=str(uuid.uuid4()), name="nb chat",
                           endpoint_url="http://example.test", model="test-model")
        sess.notebook_id = "nb-123"
        s.add(sess)
        s.commit()
        assert sess.to_dict().get("notebook_id") == "nb-123"
    finally:
        s.close()
