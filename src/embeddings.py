"""
embeddings.py

Embedding clients for RAG and memory vector search.

Priority order:
  1. HTTP API (Ollama / vLLM / llama.cpp) — set EMBEDDING_URL in .env
  2. Local fastembed (ONNX, ~50MB) — zero config fallback

Set EMBEDDING_URL in .env, e.g.:
  EMBEDDING_URL=http://localhost:11434/v1/embeddings   (ollama)
  EMBEDDING_URL=http://localhost:8000/v1/embeddings    (vllm / llama.cpp)
"""

import os

from src.constants import FASTEMBED_CACHE_DIR, EMBEDDING_ENDPOINT_FILE

# Windows: force HuggingFace/fastembed to COPY model files rather than symlink
# them. On a network-share/UNC cache dir Windows can't follow HF's symlinks
# ([WinError 1463] "symbolic link cannot be followed"), so ONNX fails to load the
# model and semantic memory dies. huggingface_hub reads this flag at import time,
# so it must be set before huggingface_hub is first imported — hence module-top.
# (app.py sets the same guard for the server entrypoint.)
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import logging
import numpy as np
import httpx
from typing import List, Optional

from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "all-minilm:l6-v2"
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class EmbeddingClient:
    """Drop-in replacement for SentenceTransformer.encode() using an HTTP API."""

    def __init__(self, url: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None):
        # `or` (not os.getenv's default arg) so a PRESENT-but-EMPTY value falls
        # back to the default. docker-compose.yml injects
        # `EMBEDDING_URL=${EMBEDDING_URL:-}` / `EMBEDDING_MODEL=${EMBEDDING_MODEL:-}`,
        # which sets the var to "" when the host hasn't defined it.
        # os.getenv(name, default) only substitutes default when the var is
        # ABSENT, so it would silently pass through "" (see issue #124: an
        # empty EMBEDDING_URL produced a misleading "missing http(s)://
        # protocol" boot warning instead of falling back cleanly).
        self.url = url or os.getenv("EMBEDDING_URL") or (
            f"http://{os.getenv('LLM_HOST', 'localhost')}:11434/v1/embeddings"
        )
        self.model = model or os.getenv("EMBEDDING_MODEL") or _DEFAULT_MODEL
        self.api_key = api_key or os.getenv("EMBEDDING_API_KEY")
        self._dim: Optional[int] = None
        # Short connect timeout so a DOWN embedding endpoint (e.g. Ollama not
        # running on :11434) fast-fails to the local FastEmbed fallback instead
        # of stalling startup ~30s per probe. Read stays generous for a real
        # endpoint (embedding a short string returns in well under a second).
        self._client = httpx.Client(timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0))
        self._batch_size = max(1, int(os.getenv("EMBEDDING_BATCH_SIZE", "8")))
        self._max_chars = max(200, int(os.getenv("EMBEDDING_MAX_CHARS", "900")))

    def get_sentence_embedding_dimension(self) -> int:
        """Probe the endpoint for embedding dimension if not yet known."""
        if self._dim is not None:
            return self._dim
        # Embed a single word to discover the dimension
        vec = self.encode(["hello"])
        self._dim = vec.shape[1]
        logger.info(f"Embedding dimension: {self._dim} (model={self.model})")
        return self._dim

    def encode(
        self, texts: List[str], normalize_embeddings: bool = True
    ) -> np.ndarray:
        """Encode texts via the API. Returns (N, dim) float32 array."""
        if not texts:
            return np.array([], dtype="float32")

        all_vecs = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            all_vecs.extend(self._embed_batch(batch))

        vecs = np.array(all_vecs, dtype="float32")

        if normalize_embeddings and vecs.size > 0:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1, norms)
            vecs = vecs / norms

        if self._dim is None and vecs.size > 0:
            self._dim = vecs.shape[1]

        return vecs

    def _embed_batch(self, batch: List[str]) -> List[List[float]]:
        try:
            return self._post_embeddings(batch)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else None
            if status != 400:
                raise
            if len(batch) > 1:
                vecs = []
                for text in batch:
                    vecs.extend(self._embed_batch([text]))
                return vecs
            text = batch[0]
            trimmed = text[: self._max_chars]
            if trimmed != text:
                logger.warning(
                    "Embedding input exceeded endpoint context; retrying with %d chars",
                    len(trimmed),
                )
                return self._post_embeddings([trimmed])
            raise

    def _post_embeddings(self, batch: List[str]) -> List[List[float]]:
        resp = self._client.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            json={"input": batch, "model": self.model},
        )
        resp.raise_for_status()
        data = resp.json()

        # OpenAI format: {"data": [{"embedding": [...], "index": 0}, ...]}
        embeddings = data.get("data", [])
        embeddings.sort(key=lambda e: e.get("index", 0))
        return [emb["embedding"] for emb in embeddings]


_onnxruntime_dlls_preloaded = False  # process-level latch: preload_dlls() once


def _preload_onnxruntime_cuda_dlls() -> None:
    """Best-effort: load the CUDA/cuDNN shared libraries from the nvidia-*
    pip packages (only installed in the GPU image; see
    requirements-gpu-nvidia.txt) before onnxruntime tries to initialize the
    CUDAExecutionProvider.

    Installing those pip packages alone is not enough — they land under
    site-packages/nvidia/... which is not on the dynamic linker's search
    path, so onnxruntime's dlopen("libcublasLt.so.13") by bare soname finds
    nothing and CUDA EP init fails, silently falling back to CPU (the
    ~5x slower path this exists to avoid). onnxruntime.preload_dlls() loads
    each library by its known path first so the linker's later dlopen finds
    it already resident.

    Harmless no-op on the CPU image and on hosts without a GPU: the plain
    `onnxruntime` package's build carries no CUDA version metadata, so
    preload_dlls() returns immediately without doing anything (verified
    empirically — its cuda_version detection is blank for that build).
    """
    global _onnxruntime_dlls_preloaded
    if _onnxruntime_dlls_preloaded:
        return
    _onnxruntime_dlls_preloaded = True
    try:
        import onnxruntime
        if hasattr(onnxruntime, "preload_dlls"):
            onnxruntime.preload_dlls()
    except Exception as e:
        logger.debug("onnxruntime.preload_dlls() skipped: %s", e)


class FastEmbedClient:
    """Local embedding client using fastembed (ONNX). No external service needed."""

    def __init__(self, model: Optional[str] = None):
        _preload_onnxruntime_cuda_dlls()
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise RuntimeError(
                "Local fastembed is not installed. Either install it "
                "(pip install fastembed) or point the app at a remote "
                "embeddings server."
            ) from e

        # See the matching comment in EmbeddingClient.__init__: `or`, not
        # os.getenv's default arg, so a PRESENT-but-EMPTY FASTEMBED_MODEL
        # (docker-compose.yml's `${FASTEMBED_MODEL:-...}`) falls back to the
        # deliberate multilingual default instead of an empty model_name.
        self.model = model or os.getenv("FASTEMBED_MODEL") or DEFAULT_FASTEMBED_MODEL
        # Persistent cache under data/ so the model survives reboots and so
        # the download lands exactly where the admin panel's _is_downloaded()
        # check looks (both default to this same path).
        cache_dir = FASTEMBED_CACHE_DIR
        os.makedirs(cache_dir, exist_ok=True)
        # Windows self-heal: the HuggingFace-hub cache stores model files as
        # symlinks (snapshots/<rev>/model.onnx -> ../../blobs/<hash>). On a
        # network-share / UNC data dir Windows refuses to follow them
        # ([WinError 1463] "symbolic link cannot be followed because its type is
        # disabled"), and a cache copied between machines can carry dead symlinks
        # too. Either way fastembed tries to load a broken symlink and fails
        # *without* re-downloading, leaving semantic memory degraded. Detect a
        # broken-symlink model in the cache and drop the contaminated hub dir so
        # fastembed re-fetches (it falls back to its CDN tarball of real files,
        # which load fine). Best-effort; only ever removes a verifiably dead link.
        if os.name == "nt":
            try:
                import glob, shutil
                for _onnx in glob.glob(os.path.join(cache_dir, "**", "*.onnx"), recursive=True):
                    if os.path.islink(_onnx) and not os.path.exists(_onnx):
                        _root = _onnx
                        while os.path.basename(_root) and not os.path.basename(_root).startswith("models--"):
                            _parent = os.path.dirname(_root)
                            if _parent == _root:
                                break
                            _root = _parent
                        if os.path.basename(_root).startswith("models--"):
                            logger.warning(
                                "Embedding cache has a broken symlink (%s); clearing %s "
                                "so fastembed re-downloads real files", _onnx, _root,
                            )
                            shutil.rmtree(_root, ignore_errors=True)
            except Exception as _e:
                logger.debug("embedding cache symlink-heal skipped: %s", _e)
        kwargs = {"model_name": self.model, "cache_dir": cache_dir}
        self._embedding = TextEmbedding(**kwargs)
        self._dim: Optional[int] = None
        self.url = "local://fastembed"
        logger.info(f"FastEmbed loaded model={self.model}")

    def get_sentence_embedding_dimension(self) -> int:
        if self._dim is not None:
            return self._dim
        vec = self.encode(["hello"])
        self._dim = vec.shape[1]
        logger.info(f"Embedding dimension: {self._dim} (model={self.model})")
        return self._dim

    def encode(
        self, texts: List[str], normalize_embeddings: bool = True
    ) -> np.ndarray:
        """Encode texts locally. Returns (N, dim) float32 array."""
        if not texts:
            return np.array([], dtype="float32")

        vecs = np.array(list(self._embedding.embed(texts)), dtype="float32")

        if normalize_embeddings and vecs.size > 0:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1, norms)
            vecs = vecs / norms

        if self._dim is None and vecs.size > 0:
            self._dim = vecs.shape[1]

        return vecs


def _load_persisted_endpoint() -> dict:
    """Load the custom embedding endpoint saved from the admin panel."""
    try:
        endpoint_file = EMBEDDING_ENDPOINT_FILE
        if os.path.exists(endpoint_file):
            import json
            data = json.loads(open(endpoint_file, encoding="utf-8").read())
            if data.get("url"):
                return data
    except Exception:
        pass
    return {}


_http_embed_down = False  # process-level latch: skip re-probing a dead endpoint


def reset_http_embed_state():
    """Clear the 'HTTP embedding endpoint is down' latch so the next
    get_embedding_client() re-probes. Call this when the embedding endpoint
    setting changes (e.g. the user starts Ollama and saves the endpoint) —
    otherwise a latch tripped at startup would keep us on FastEmbed for the
    whole process even after the endpoint comes back."""
    global _http_embed_down
    _http_embed_down = False


def get_embedding_client():
    """Factory: try HTTP API first, fall back to local fastembed."""
    global _http_embed_down

    # Check for a persisted custom endpoint (saved from admin panel)
    persisted = _load_persisted_endpoint()
    if persisted.get("url"):
        url = persisted["url"]
        model = persisted.get("model", "")
        api_key = persisted.get("api_key", "")
        # Also set in env so other code sees it
        os.environ["EMBEDDING_URL"] = url
        if model:
            os.environ["EMBEDDING_MODEL"] = model
        if api_key:
            from src.secret_storage import decrypt
            os.environ["EMBEDDING_API_KEY"] = decrypt(api_key)
    # Try the HTTP embedding API — unless we already found it down this process
    # (avoids paying the connect timeout again on every RAG/memory/tool probe).
    if not _http_embed_down:
        try:
            client = EmbeddingClient()
            client.get_sentence_embedding_dimension()  # health check
            logger.info(f"Using HTTP embedding API: {client.url} model={client.model}")
            return client
        except Exception as e:
            _http_embed_down = True
            logger.warning(f"HTTP embedding API unavailable ({e}); using local FastEmbed for the rest of this process")

    # Fall back to local fastembed
    try:
        client = FastEmbedClient()
        client.get_sentence_embedding_dimension()
        logger.info(f"Using local FastEmbed: model={client.model}")
        return client
    except ImportError:
        logger.error("fastembed not installed — run: pip install fastembed")
    except Exception as e:
        logger.error(f"FastEmbed init failed: {e}")

    return None
