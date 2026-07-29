"""do_generate_image downloads a provider-returned image URL server-side; that
URL is controlled by the upstream image API, so it must go through the
SSRF-safe fetch helper (policy check + DNS pin + per-hop redirect
revalidation) instead of a bare httpx.get."""
from src import ai_interaction


class _GenerationResponse:
    status_code = 200
    text = ""

    def __init__(self, image_url):
        self._image_url = image_url

    def json(self):
        return {"data": [{"url": self._image_url}]}


class _DownloadResponse:
    status_code = 503
    content = b""


def _patch_generation(monkeypatch, image_url):
    async def _post(self, url, json, headers):
        return _GenerationResponse(image_url)

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        post = _post

    import httpx
    import src.settings as settings

    monkeypatch.setattr(settings, "load_settings", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(
        ai_interaction,
        "_resolve_model",
        lambda model_spec, owner=None: (
            "https://api.openai.example/v1/chat/completions",
            "dall-e-3",
            {"Authorization": "Bearer test"},
        ),
    )


async def test_generate_image_downloads_provider_url_via_safe_fetch(monkeypatch):
    import src.url_safety as url_safety

    provider_url = "https://images.example.com/generated.png?sig=abc"
    events = []
    _patch_generation(monkeypatch, provider_url)

    def _safe_fetch(method, url, *, timeout=None, block_private=False, **kw):
        events.append(("fetch", method, url, timeout, block_private))
        return _DownloadResponse()

    monkeypatch.setattr(url_safety, "safe_httpx_request", _safe_fetch)

    result = await ai_interaction.do_generate_image("draw a chair\ndall-e-3")

    # 503 download → falls back to the external URL, matching prior behavior.
    assert result["image_url"] == provider_url
    assert events == [("fetch", "GET", provider_url, 60, False)]


async def test_generate_image_rejects_unsafe_provider_url_without_download(monkeypatch):
    import src.url_safety as url_safety

    unsafe_url = "http://169.254.169.254/latest/meta-data"
    events = []
    _patch_generation(monkeypatch, unsafe_url)

    def _safe_fetch(method, url, *, timeout=None, block_private=False, **kw):
        events.append(("fetch", method, url))
        raise url_safety.UnsafeOutboundURL(
            "link-local address blocked (SSRF metadata risk): 169.254.169.254"
        )

    monkeypatch.setattr(url_safety, "safe_httpx_request", _safe_fetch)

    result = await ai_interaction.do_generate_image("draw a chair\ndall-e-3")

    assert result["error"] == (
        "Image API returned unsafe image URL: "
        "link-local address blocked (SSRF metadata risk): 169.254.169.254"
    )
    assert events == [("fetch", "GET", unsafe_url)]
