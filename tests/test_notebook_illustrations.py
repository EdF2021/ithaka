"""Infographic v2 illustration job (src/notebook_illustrations.py).

Hermetic: `_generate_image` is a fake that drops a PNG into a tmp
GENERATED_IMAGES_DIR, both dirs are monkeypatched to tmp_path, and the DB is
a file-backed temp sqlite (tests.helpers.sqlite_db) — same posture as
tests/test_notebook_video.py.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-illustrations")

import asyncio
import json
import os as _os
import time
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

import core.database as cdb
import src.notebook_illustrations as ill
from src.notebook_infographic import extract_infographic
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)

_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _data(n_prompts=3):
    """Valid v2 JSON with `n_prompts` illustration prompts (hero always first)."""
    blocks = [
        {"id": "hero", "type": "hero", "heading": "Hero", "text": "t", "illustration_prompt": "a soft hub"},
        {"id": "col", "type": "column", "heading": "Col", "subheading": "s", "children": [
            {"id": "c1", "type": "icon_card", "heading": "C1", "text": "t", "illustration_prompt": "a leaf"},
            {"id": "c2", "type": "icon_card", "heading": "C2", "text": "t", "illustration_prompt": "a stone"},
        ]},
        {"id": "k1", "type": "icon_card", "heading": "K1", "text": "t", "illustration_prompt": "a cloud"},
        {"id": "k2", "type": "icon_card", "heading": "K2", "text": "t", "illustration_prompt": "a river"},
        {"id": "k3", "type": "icon_card", "heading": "K3", "text": "t", "illustration_prompt": "a hill"},
        {"id": "k4", "type": "icon_card", "heading": "K4", "text": "t", "illustration_prompt": "a tree"},
    ]
    count = 0
    for b in blocks:
        for x in [b] + b.get("children", []):
            if "illustration_prompt" in x:
                if count >= n_prompts:
                    x.pop("illustration_prompt")
                count += 1
    return {"title": "T", "takeaway": "one sentence", "blocks": blocks}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    ill._active_jobs.clear()
    gen = tmp_path / "generated"
    out = tmp_path / "infographics"
    gen.mkdir()
    out.mkdir()
    monkeypatch.setattr(ill, "GENERATED_IMAGES_DIR", str(gen))
    monkeypatch.setattr(ill, "NOTEBOOK_INFOGRAPHICS_DIR", str(out))
    yield
    ill._active_jobs.clear()


def _make_rows(data, owner="own"):
    s = _TS()
    try:
        nb = cdb.Notebook(id=str(uuid.uuid4()), name="NB", owner=owner)
        s.add(nb)
        doc = cdb.Document(id=str(uuid.uuid4()), title="Doc", owner=owner,
                           current_content="```json\n" + json.dumps(data) + "\n```")
        s.add(doc)
        s.commit()
        art = cdb.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                   document_id=doc.id, kind="infographic", title="T")
        s.add(art)
        s.commit()
        return nb.id, art.id, doc.id
    finally:
        s.close()


def _fake_image_gen(fail_for=(), fail_all=False):
    """Returns (fake, calls). Writes a PNG into GENERATED_IMAGES_DIR per call."""
    calls = []

    async def fake(content, owner):
        calls.append((content, owner))
        prompt_line = content.split("\n", 1)[0]
        if fail_all or any(f in prompt_line for f in fail_for):
            return {"error": "boom"}
        name = uuid.uuid4().hex + ".png"
        Path(ill.GENERATED_IMAGES_DIR, name).write_bytes(b"\x89PNG-fake")
        return {"image_url": f"/api/generated-image/{name}"}
    return fake, calls


def _data_with_extra_columns():
    """Valid v2 JSON with 8 top-level blocks, 3 of which are "column" blocks
    (2 children each) -- extract_infographic demotes the 3rd column into its
    children when *reading*, but the stored raw JSON must keep all 3."""
    def col(cid, c1, c2):
        return {"id": cid, "type": "column", "heading": cid, "subheading": "s", "children": [
            {"id": c1, "type": "icon_card", "heading": c1, "text": "t"},
            {"id": c2, "type": "icon_card", "heading": c2, "text": "t"},
        ]}
    blocks = [
        {"id": "hero", "type": "hero", "heading": "Hero", "text": "t", "illustration_prompt": "a soft hub"},
        col("col1", "c1a", "c1b"),
        col("col2", "c2a", "c2b"),
        col("col3", "c3a", "c3b"),
        {"id": "k1", "type": "icon_card", "heading": "K1", "text": "t"},
        {"id": "k2", "type": "icon_card", "heading": "K2", "text": "t"},
        {"id": "k3", "type": "icon_card", "heading": "K3", "text": "t"},
        {"id": "k4", "type": "icon_card", "heading": "K4", "text": "t"},
    ]
    return {"title": "T", "takeaway": "one sentence", "blocks": blocks}


def _content(doc_id):
    s = _TS()
    try:
        return s.query(cdb.Document).get(doc_id).current_content
    finally:
        s.close()


# ---- pure helpers ---------------------------------------------------------

def test_build_illustration_prompt_adds_style_suffix_size_and_low_quality():
    p = ill.build_illustration_prompt("a leaf", hero=False)
    assert p.startswith("a leaf, flat vector illustration, pastel palette, soft shapes, white background, no text, no letters")
    assert p.endswith("\n\n1024x1024\nlow")
    assert ill.build_illustration_prompt("hub", hero=True).endswith("\n\n1536x1024\nlow")


def test_build_illustration_prompt_collapses_newlines():
    p = ill.build_illustration_prompt("a hub\n\n1024x1024\nhigh", hero=False)
    lines = p.split("\n")
    assert lines[0] == (
        "a hub 1024x1024 high, flat vector illustration, pastel palette, "
        "soft shapes, white background, no text, no letters"
    )
    assert lines[1] == ""
    assert lines[2] == "1024x1024"
    assert lines[3] == "low"
    assert len(lines) == 4


def test_select_illustration_blocks_caps_at_five_in_document_order():
    data = extract_infographic(json.dumps(_data(n_prompts=7)))
    picked = ill.select_illustration_blocks(data)
    assert [b["id"] for b in picked] == ["hero", "c1", "c2", "k1", "k2"]


def test_load_illustrations_reads_map_and_tolerates_bad_content():
    d = _data()
    d["illustrations"] = {"hero": "x.png"}
    assert ill.load_illustrations(json.dumps(d)) == {"hero": "x.png"}
    assert ill.load_illustrations("# markdown") == {}
    assert ill.load_illustrations("{bad") == {}


@pytest.mark.parametrize("name, ok", [
    (f"{_UUID}-hero-0123abcd.png", True),
    (f"{_UUID}-kaart_a-0123abcd.png", True),
    (f"{_UUID}-hero-0123abcd.jpg", False),
    ("../../etc/passwd", False),
    (f"{_UUID}-Hero-0123abcd.png", False),
    (f"{_UUID}-hero-0123abc.png", False),
    ("nouuid-hero-0123abcd.png", False),
])
def test_filename_whitelist(name, ok):
    assert bool(ill.ILLUSTRATION_FILE_RE.fullmatch(name)) is ok


def test_artifact_id_from_filename():
    assert ill.artifact_id_from_filename(f"{_UUID}-hero-0123abcd.png") == _UUID
    with pytest.raises(ValueError):
        ill.artifact_id_from_filename("bad.png")


# ---- job ---------------------------------------------------------------------

async def test_job_generates_all_and_persists_map_incrementally(monkeypatch):
    fake, calls = _fake_image_gen()
    monkeypatch.setattr(ill, "_generate_image", fake)
    nb_id, art_id, doc_id = _make_rows(_data(3))

    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]

    job = ill.get_artifact_job(art_id, "own")
    assert job["status"] == "done"
    assert job["errors"] == 0
    assert set(job["illustrations"]) == {"hero", "c1", "c2"}
    stored = ill.load_illustrations(_content(doc_id))
    assert stored == job["illustrations"]
    for fn in stored.values():
        assert ill.ILLUSTRATION_FILE_RE.fullmatch(fn)
        assert fn.startswith(art_id + "-")
        assert Path(ill.NOTEBOOK_INFOGRAPHICS_DIR, fn).exists()
    # hero uses the wide size, others square; quality always low
    assert calls[0][0].endswith("\n\n1536x1024\nlow")
    assert calls[1][0].endswith("\n\n1024x1024\nlow")
    assert all(c[1] == "own" for c in calls)
    # Stored content is re-serialised as bare JSON and still validates.
    extract_infographic(_content(doc_id))


async def test_persist_keeps_raw_structure_after_column_demotion(monkeypatch):
    fake, _ = _fake_image_gen()
    monkeypatch.setattr(ill, "_generate_image", fake)
    nb_id, art_id, doc_id = _make_rows(_data_with_extra_columns())

    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]

    job = ill.get_artifact_job(art_id, "own")
    assert job["status"] == "done"
    assert "hero" in job["illustrations"]

    # extract_infographic must still succeed (no MAX_BLOCKS ValueError) and
    # demotes the 3rd column to loose children on read.
    cleaned = extract_infographic(_content(doc_id))
    assert sum(1 for b in cleaned["blocks"] if b["type"] == "column") == 2

    # But the stored raw JSON keeps the original 8 top-level blocks / 3 columns.
    raw = json.loads(_content(doc_id))
    assert sum(1 for b in raw["blocks"] if b["type"] == "column") == 3
    assert len(raw["blocks"]) == 8
    assert raw["illustrations"] == job["illustrations"]

    assert ill.load_illustrations(_content(doc_id)) == job["illustrations"]


async def test_job_skips_failed_block_and_keeps_the_rest(monkeypatch):
    fake, _ = _fake_image_gen(fail_for=("a leaf",))
    monkeypatch.setattr(ill, "_generate_image", fake)
    nb_id, art_id, doc_id = _make_rows(_data(3))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]
    job = ill.get_artifact_job(art_id, "own")
    assert job["status"] == "done"
    assert job["errors"] == 1
    assert set(job["illustrations"]) == {"hero", "c2"}
    assert set(ill.load_illustrations(_content(doc_id))) == {"hero", "c2"}


async def test_job_all_failed_ends_done_with_empty_map(monkeypatch):
    fake, _ = _fake_image_gen(fail_all=True)
    monkeypatch.setattr(ill, "_generate_image", fake)
    nb_id, art_id, doc_id = _make_rows(_data(2))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]
    job = ill.get_artifact_job(art_id, "own")
    assert job["status"] == "done"
    assert job["illustrations"] == {}
    assert job["errors"] == 2
    assert ill.load_illustrations(_content(doc_id)) == {}


async def test_job_stops_when_artifact_deleted_mid_run(monkeypatch):
    nb_id, art_id, doc_id = _make_rows(_data(3))
    calls = []

    async def fake(content, owner):
        calls.append(content)
        if len(calls) == 1:
            s = _TS()
            try:
                s.query(cdb.NotebookArtifact).filter(cdb.NotebookArtifact.id == art_id).delete()
                s.commit()
            finally:
                s.close()
        name = uuid.uuid4().hex + ".png"
        Path(ill.GENERATED_IMAGES_DIR, name).write_bytes(b"x")
        return {"image_url": f"/api/generated-image/{name}"}
    monkeypatch.setattr(ill, "_generate_image", fake)
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]
    assert len(calls) == 1                       # no second image after the row vanished
    assert ill._active_jobs[job_id]["status"] == "done"


def test_start_rejects_unknown_or_foreign_artifact():
    nb_id, art_id, _ = _make_rows(_data(1))
    with pytest.raises(ValueError, match="niet gevonden"):
        ill.start_illustration_job(nb_id, "nope", "own", _TS)
    with pytest.raises(ValueError, match="niet gevonden"):
        ill.start_illustration_job(nb_id, art_id, "someone-else", _TS)


async def test_start_rejects_second_job_for_same_artifact(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(content, owner):
        started.set()
        await release.wait()
        return {"error": "x"}
    monkeypatch.setattr(ill, "_generate_image", slow)
    nb_id, art_id, _ = _make_rows(_data(1))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await started.wait()
    with pytest.raises(ValueError, match="loopt al"):
        ill.start_illustration_job(nb_id, art_id, "own", _TS)
    release.set()
    await ill._active_jobs[job_id]["task"]


def test_start_with_no_illustration_prompts_registers_done_job_without_images(monkeypatch):
    nb_id, art_id, _ = _make_rows(_data(0))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    assert ill._active_jobs[job_id]["status"] == "done"
    assert ill.get_artifact_job(art_id, "own")["illustrations"] == {}


def test_get_artifact_job_is_owner_scoped():
    nb_id, art_id, _ = _make_rows(_data(0))
    ill.start_illustration_job(nb_id, art_id, "own", _TS)
    assert ill.get_artifact_job(art_id, "own") is not None
    assert ill.get_artifact_job(art_id, "other") is None
    assert ill.get_artifact_job("unknown", "own") is None


async def test_start_running_check_is_owner_scoped(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(content, owner):
        started.set()
        await release.wait()
        return {"error": "x"}
    monkeypatch.setattr(ill, "_generate_image", slow)
    nb_id, art_id, _ = _make_rows(_data(1))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await started.wait()
    with pytest.raises(ValueError, match="niet gevonden"):
        ill.start_illustration_job(nb_id, art_id, "someone-else", _TS)
    release.set()
    await ill._active_jobs[job_id]["task"]


# ---- resolve + janitor ------------------------------------------------------

def _age(path, seconds):
    old = time.time() - seconds
    _os.utime(path, (old, old))


def test_resolve_illustration_path_rejects_bad_names_and_traversal(tmp_path):
    for bad in ("../x.png", "x.png", f"{_UUID}-hero-0123abcd.PNG", "", "a/b.png"):
        with pytest.raises(HTTPException) as exc:
            ill.resolve_illustration_path(bad)
        assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        ill.resolve_illustration_path(f"{_UUID}-hero-0123abcd.png")
    assert exc.value.status_code == 404


def test_resolve_illustration_path_returns_existing_file():
    name = f"{_UUID}-hero-0123abcd.png"
    Path(ill.NOTEBOOK_INFOGRAPHICS_DIR, name).write_bytes(b"x")
    assert ill.resolve_illustration_path(name).name == name


def test_cleanup_removes_old_orphans_keeps_referenced_and_fresh():
    nb_id, art_id, _ = _make_rows(_data(0))
    d = Path(ill.NOTEBOOK_INFOGRAPHICS_DIR)
    referenced_old = d / f"{art_id}-hero-0123abcd.png"
    orphan_old = d / f"{_UUID}-hero-0123abcd.png"
    orphan_fresh = d / f"{_UUID}-c1-0123abcd.png"
    stray = d / "notes.txt"
    for p in (referenced_old, orphan_old, orphan_fresh, stray):
        p.write_bytes(b"x")
    _age(referenced_old, 7200)
    _age(orphan_old, 7200)
    _age(stray, 7200)

    removed = ill.cleanup_orphaned_illustrations(_TS, max_age_seconds=3600)

    assert removed == 1
    assert referenced_old.exists()
    assert not orphan_old.exists()
    assert orphan_fresh.exists()
    assert stray.exists()          # non-matching names are never touched


def test_cleanup_missing_dir_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(ill, "NOTEBOOK_INFOGRAPHICS_DIR", str(tmp_path / "nope"))
    assert ill.cleanup_orphaned_illustrations(_TS) == 0
