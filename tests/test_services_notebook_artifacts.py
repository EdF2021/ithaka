"""NotebookArtifact model: roundtrip + cascade behavior.

Uses the suite's documented convention for a real, isolated DB
(tests.helpers.sqlite_db.make_temp_sqlite, see tests/TESTING_STANDARD.md and
tests/README.md) rather than the shared core.database.engine/SessionLocal —
see tests/test_services_notebooks_db.py for the rationale (the shared engine
lives for the whole pytest session; committing real rows there would leak
into every other test).

core.database registers a global `PRAGMA foreign_keys=ON` listener on the
SQLAlchemy Engine class (core/database.py, set_sqlite_pragma), so FK-level
ON DELETE CASCADE is enforced on this module's own sqlite engine too, once
core.database has been imported (it is, via the `import core.database as db`
below).

Fixtures (_TS/_ENGINE/_TMPDB, make_notebook, make_document) are kept at
module level / as plain helper functions so a later task in this feature can
extend this file with artifact-service tests against the same schema.
"""
import uuid

import core.database as db
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)


def make_notebook(session, owner="ed", name="Thesis"):
    nb = db.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
    session.add(nb)
    session.commit()
    return nb


def make_document(session, title="Report", owner="ed"):
    doc = db.Document(id=str(uuid.uuid4()), title=title, owner=owner)
    session.add(doc)
    session.commit()
    return doc


def test_notebook_artifact_roundtrip():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id="a1", notebook_id=nb.id, document_id=doc.id, kind="faq")
        s.add(art)
        s.commit()
        d = art.to_dict()
        assert d["kind"] == "faq"
        assert d["document_id"] == doc.id
        assert d["notebook_id"] == nb.id
        assert d["id"] == "a1"
        assert "created_at" in d
        assert "updated_at" in d
    finally:
        s.close()


def test_artifact_cascade_on_notebook_delete():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                  document_id=doc.id, kind="summary")
        s.add(art)
        s.commit()
        s.refresh(nb)
        assert len(nb.artifacts) == 1

        s.delete(nb)
        s.commit()

        assert s.query(db.NotebookArtifact).filter_by(notebook_id=nb.id).count() == 0
    finally:
        s.close()


def test_artifact_cascade_on_document_delete():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                  document_id=doc.id, kind="podcast")
        s.add(art)
        s.commit()
        artifact_id = art.id

        # DB-level ON DELETE CASCADE (not ORM cascade): delete the Document
        # row directly, bypassing the Notebook relationship.
        s.delete(doc)
        s.commit()

        assert s.query(db.NotebookArtifact).filter_by(id=artifact_id).count() == 0
    finally:
        s.close()
