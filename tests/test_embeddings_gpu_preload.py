"""Regression: FastEmbedClient must preload the CUDA/cuDNN shared libraries
from the nvidia-* pip packages (GPU image only; see
requirements-gpu-nvidia.txt) before constructing the fastembed TextEmbedding
session.

Installing onnxruntime-gpu plus the nvidia-cublas/nvidia-cudnn-cu13/...
packages is not sufficient by itself: those packages land under
site-packages/nvidia/... which is not on the dynamic linker's search path, so
onnxruntime's dlopen("libcublasLt.so.13") by bare soname finds nothing and
CUDA execution provider init silently falls back to CPU. This was verified
live against a running Ithaka container: onnxruntime-gpu 1.28.0 was
manually pip-installed into /app/.local, `get_available_providers()` already
listed CUDAExecutionProvider, and it still failed at runtime with
"libcublasLt.so.13: cannot open shared object file" because nothing had
loaded the library first. `onnxruntime.preload_dlls()` is what makes those
libraries resolvable at dlopen() time.

These tests mock onnxruntime so they run on the plain CPU dev environment
(no GPU, no nvidia-* packages installed) and pin the calling contract rather
than actual CUDA behaviour, which can only be verified against a real image
+ GPU (see docs/setup.md's NVIDIA GPU overlay section).
"""

from __future__ import annotations

import sys
import types

import src.embeddings as embeddings_module
from src.embeddings import FastEmbedClient


def _install_fastembed_stub(monkeypatch):
    fastembed = types.ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, model_name=None, cache_dir=None):
            self.model_name = model_name

        def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    fastembed.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)


def _reset_preload_latch(monkeypatch):
    # Module-level "only preload once per process" latch — reset it so each
    # test observes its own call (or lack thereof).
    monkeypatch.setattr(embeddings_module, "_onnxruntime_dlls_preloaded", False)


def test_fastembed_client_calls_preload_dlls(monkeypatch):
    """The happy path: onnxruntime.preload_dlls() is called before the
    fastembed TextEmbedding session is constructed."""
    _install_fastembed_stub(monkeypatch)
    _reset_preload_latch(monkeypatch)

    calls = []
    onnxruntime_stub = types.ModuleType("onnxruntime")
    onnxruntime_stub.preload_dlls = lambda: calls.append(True)
    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime_stub)

    FastEmbedClient()

    assert calls == [True]


def test_fastembed_client_preloads_only_once_per_process(monkeypatch):
    """Repeated FastEmbedClient() construction must not re-preload every
    time — preload_dlls() is a real (if idempotent) dlopen() call per shared
    library, not free."""
    _install_fastembed_stub(monkeypatch)
    _reset_preload_latch(monkeypatch)

    calls = []
    onnxruntime_stub = types.ModuleType("onnxruntime")
    onnxruntime_stub.preload_dlls = lambda: calls.append(True)
    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime_stub)

    FastEmbedClient()
    FastEmbedClient()
    FastEmbedClient()

    assert calls == [True]


def test_fastembed_client_tolerates_missing_preload_dlls(monkeypatch):
    """Older/CPU-only onnxruntime builds may lack preload_dlls() entirely —
    must not break client construction."""
    _install_fastembed_stub(monkeypatch)
    _reset_preload_latch(monkeypatch)

    onnxruntime_stub = types.ModuleType("onnxruntime")  # no preload_dlls attr
    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime_stub)

    client = FastEmbedClient()

    assert client is not None


def test_fastembed_client_tolerates_preload_dlls_raising(monkeypatch):
    """A preload_dlls() failure (e.g. a malformed nvidia-* install) must
    degrade to CPU, not break embedding client construction."""
    _install_fastembed_stub(monkeypatch)
    _reset_preload_latch(monkeypatch)

    def _boom():
        raise OSError("simulated dlopen failure")

    onnxruntime_stub = types.ModuleType("onnxruntime")
    onnxruntime_stub.preload_dlls = _boom
    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime_stub)

    client = FastEmbedClient()

    assert client is not None
