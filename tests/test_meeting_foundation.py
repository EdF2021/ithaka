import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-meeting-foundation")

import core.database as db
from src import constants, upload_limits
from tests.helpers.sqlite_db import make_temp_sqlite


def test_meeting_audio_dir_under_data_dir():
    assert constants.MEETING_AUDIO_DIR == os.path.join(constants.DATA_DIR, "meeting_audio")


def test_meeting_limits_defaults():
    assert upload_limits.MEETING_CHUNK_MAX_BYTES == 10 * 1024 * 1024
    assert upload_limits.MEETING_AUDIO_MAX_BYTES == 500 * 1024 * 1024


def test_meeting_row_roundtrip():
    SessionLocal, engine, tmp = make_temp_sqlite(db.Base.metadata)
    s = SessionLocal()
    try:
        s.add(db.Meeting(id="m1", owner="ed", title="Weekly"))
        s.commit()
        row = s.query(db.Meeting).filter_by(id="m1").one()
        assert row.status == "recording" and row.bytes_total == 0 and row.document_id is None
    finally:
        s.close(); tmp.close()
