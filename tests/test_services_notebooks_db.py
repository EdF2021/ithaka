"""Notebook/NotebookSource model + sessions.notebook_id column."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebooks-db")

import uuid
import core.database as db


def _fresh_session():
    db.Base.metadata.create_all(bind=db.engine)
    return db.SessionLocal()  # verify exact factory name at top of core/database.py; adjust if it differs


def test_notebook_and_source_roundtrip_and_cascade():
    s = _fresh_session()
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
    s.close()


def test_session_to_dict_exposes_notebook_id():
    s = _fresh_session()
    sess = db.Session(id=str(uuid.uuid4()), name="nb chat",
                       endpoint_url="http://example.test", model="test-model")
    sess.notebook_id = "nb-123"
    s.add(sess)
    s.commit()
    assert sess.to_dict().get("notebook_id") == "nb-123"
    s.close()
