"""Skill URL importer — GitHub path parsing."""
import pytest

from services.memory.skill_importer import (
    ResolvedSource,
    SkillImportError,
    _assert_github_url,
    _fetch_bytes,
    _list_github_dir,
    parse_skill_source,
)


def test_parse_github_blob_skill_md():
    src = parse_skill_source(
        "https://github.com/anthropics/skills/blob/main/skills/pdf/SKILL.md"
    )
    assert src.owner == "anthropics"
    assert src.repo == "skills"
    assert src.ref == "main"
    assert src.path.endswith("skills/pdf/SKILL.md")


def test_parse_github_tree_directory():
    src = parse_skill_source(
        "https://github.com/example/my-skills/tree/develop/caveman-skill"
    )
    assert src.owner == "example"
    assert src.repo == "my-skills"
    assert src.ref == "develop"
    assert src.path == "caveman-skill"


def test_parse_raw_github():
    src = parse_skill_source(
        "https://raw.githubusercontent.com/o/r/main/path/SKILL.md"
    )
    assert src.owner == "o"
    assert src.repo == "r"
    assert src.ref == "main"
    assert src.path == "path/SKILL.md"


def test_rejects_non_github():
    with pytest.raises(SkillImportError):
        parse_skill_source("https://example.com/skill.md")


def test_fetch_bytes_rejects_cross_host_redirect(monkeypatch):
    """The safe fetch helper follows redirects manually; a hop that leaves the
    GitHub host allowlist must be refused before it is fetched."""
    import httpx

    real_client = httpx.Client

    def handler(request):
        # DNS is pinned, so the hostname travels in the Host header.
        assert request.headers.get("host") == "raw.githubusercontent.com"
        return httpx.Response(302, headers={"location": "https://evil.example/secret"})

    def fake_client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), follow_redirects=False)

    monkeypatch.setattr(httpx, "Client", fake_client)
    monkeypatch.setattr("src.url_safety._default_resolver", lambda host: ["93.184.216.34"])
    with pytest.raises(SkillImportError, match="redirect target"):
        _fetch_bytes("https://raw.githubusercontent.com/o/r/main/SKILL.md")


def test_assert_github_url_allows_api_host():
    _assert_github_url(
        "https://api.github.com/repos/o/r/contents?ref=main",
        context="redirect target",
    )


def test_list_github_dir_accepts_api_github_response(monkeypatch):
    monkeypatch.setattr(
        "services.memory.skill_importer._fetch_text",
        lambda url: "# skill\n",
    )

    class _Resp:
        url = "https://api.github.com/repos/o/r/contents?ref=main"
        status_code = 200
        headers: dict = {}

        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "name": "SKILL.md",
                "type": "file",
                "download_url": "https://raw.githubusercontent.com/o/r/main/SKILL.md",
            }]

    _mock_httpx_client(monkeypatch, _Resp())

    out = {}
    src = ResolvedSource(owner="o", repo="r", ref="main", path="")
    _list_github_dir(src, "", out)
    assert "SKILL.md" in out


def _mock_httpx_client(monkeypatch, response):
    """Stub the safe fetch helper's httpx.Client and hold its URL policy open
    (the mocked hostnames don't resolve in the test environment)."""
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, **kwargs):
            return response

    monkeypatch.setattr("src.url_safety.httpx.Client", _Client)
    monkeypatch.setattr(
        "src.url_safety.check_outbound_url",
        lambda url, **kwargs: (True, "ok"),
    )


def test_list_github_dir_surfaces_rate_limit(monkeypatch):
    class _Resp:
        url = "https://api.github.com/repos/o/r/contents?ref=main"
        status_code = 403
        headers: dict = {}

        def json(self):
            return {"message": "API rate limit exceeded for 203.0.113.1"}

    _mock_httpx_client(monkeypatch, _Resp())
    src = ResolvedSource(owner="o", repo="r", ref="main", path="")
    with pytest.raises(SkillImportError, match="rate limit"):
        _list_github_dir(src, "", {})


def test_fetch_bytes_surfaces_github_error_detail(monkeypatch):
    class _Resp:
        url = "https://raw.githubusercontent.com/o/r/main/SKILL.md"
        status_code = 403
        content = b""
        headers: dict = {}

        def json(self):
            return {"message": "Forbidden"}

    _mock_httpx_client(monkeypatch, _Resp())
    with pytest.raises(SkillImportError, match="GitHub request failed \\(403\\): Forbidden"):
        _fetch_bytes("https://raw.githubusercontent.com/o/r/main/SKILL.md")
