"""Regression: embedding env vars must tolerate a PRESENT-but-EMPTY value.

docker-compose.yml injects ``EMBEDDING_URL=${EMBEDDING_URL:-}``,
``EMBEDDING_MODEL=${EMBEDDING_MODEL:-}`` and
``FASTEMBED_MODEL=${FASTEMBED_MODEL:-...}`` — all of which set the variable to
``""`` in the container when the host has not defined it (or, for
FASTEMBED_MODEL, when the compose file's own fallback was accidentally the
English-only model — see issue #124). ``os.getenv(name, default)`` only uses
``default`` when the variable is ABSENT, so a present-but-empty value silently
wins over the intended default:

- ``EMBEDDING_URL=""`` produced the misleading boot warning "Request URL is
  missing an 'http://' or 'https://' protocol" instead of falling back to the
  Ollama default.
- ``EMBEDDING_MODEL=""`` would send an empty model name to the embedding API.
- ``FASTEMBED_MODEL=""`` would fail to construct FastEmbed's TextEmbedding
  with an empty model_name instead of using the deliberate multilingual
  default (``DEFAULT_FASTEMBED_MODEL``).

These tests pin the fix: an empty env value is treated like an absent one and
falls back to the code default, while an explicit non-empty override is still
honoured (mirrors the existing FASTEMBED_CACHE_PATH fix in
tests/test_fastembed_cache_path.py / src/constants.py).
"""

from __future__ import annotations

import sys
import types

from src.embeddings import EmbeddingClient, FastEmbedClient, DEFAULT_FASTEMBED_MODEL, _DEFAULT_MODEL


def _install_fastembed_stub(monkeypatch):
    """Install a fake fastembed module that records the model_name it was
    constructed with, without downloading or loading a real ONNX model."""
    fastembed = types.ModuleType("fastembed")
    calls = []

    class TextEmbedding:
        def __init__(self, model_name=None, cache_dir=None):
            calls.append(model_name)
            self.model_name = model_name

        def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    fastembed.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    return calls


class TestEmbeddingUrlEmptyEnvFallback:
    def test_empty_embedding_url_env_falls_back_to_default(self, monkeypatch):
        """The bug: EMBEDDING_URL="" (exactly what Docker injects) must fall
        back to the Ollama default, never the empty string."""
        monkeypatch.setenv("EMBEDDING_URL", "")
        monkeypatch.delenv("LLM_HOST", raising=False)

        client = EmbeddingClient()

        assert client.url, "empty env must not yield an empty URL"
        assert client.url == "http://localhost:11434/v1/embeddings"

    def test_unset_embedding_url_env_uses_default(self, monkeypatch):
        """Sanity: an absent variable also resolves to the default."""
        monkeypatch.delenv("EMBEDDING_URL", raising=False)
        monkeypatch.delenv("LLM_HOST", raising=False)

        client = EmbeddingClient()

        assert client.url == "http://localhost:11434/v1/embeddings"

    def test_explicit_embedding_url_env_is_respected(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_URL", "http://custom:9999/v1/embeddings")

        client = EmbeddingClient()

        assert client.url == "http://custom:9999/v1/embeddings"


class TestEmbeddingModelEmptyEnvFallback:
    def test_empty_embedding_model_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_MODEL", "")

        client = EmbeddingClient(url="http://test:11434/v1/embeddings")

        assert client.model == _DEFAULT_MODEL

    def test_explicit_embedding_model_env_is_respected(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_MODEL", "custom-model")

        client = EmbeddingClient(url="http://test:11434/v1/embeddings")

        assert client.model == "custom-model"


class TestFastembedModelEmptyEnvFallback:
    def test_empty_fastembed_model_env_falls_back_to_multilingual_default(self, monkeypatch):
        """The bug: FASTEMBED_MODEL="" must fall back to the deliberate
        multilingual default, never an empty model_name."""
        calls = _install_fastembed_stub(monkeypatch)
        monkeypatch.setenv("FASTEMBED_MODEL", "")

        client = FastEmbedClient()

        assert client.model == DEFAULT_FASTEMBED_MODEL
        assert calls == [DEFAULT_FASTEMBED_MODEL]

    def test_unset_fastembed_model_env_uses_multilingual_default(self, monkeypatch):
        calls = _install_fastembed_stub(monkeypatch)
        monkeypatch.delenv("FASTEMBED_MODEL", raising=False)

        client = FastEmbedClient()

        assert client.model == DEFAULT_FASTEMBED_MODEL
        assert calls == [DEFAULT_FASTEMBED_MODEL]

    def test_explicit_fastembed_model_env_is_respected(self, monkeypatch):
        calls = _install_fastembed_stub(monkeypatch)
        monkeypatch.setenv("FASTEMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

        client = FastEmbedClient()

        assert client.model == "sentence-transformers/all-MiniLM-L6-v2"
        assert calls == ["sentence-transformers/all-MiniLM-L6-v2"]
