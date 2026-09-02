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


def _install_fastembed_stub(monkeypatch, session_providers=None):
    """Install a fake fastembed module. When ``session_providers`` is given,
    the stub TextEmbedding exposes the same ``.model.model.get_providers()``
    shape as the real fastembed -> OnnxModel -> onnxruntime.InferenceSession
    chain (verified against the installed fastembed version), so
    _log_onnxruntime_providers can introspect it like the real thing."""
    fastembed = types.ModuleType("fastembed")

    class _FakeSession:
        def __init__(self, providers):
            self._providers = providers

        def get_providers(self):
            return self._providers

    class _FakeBackend:
        def __init__(self, providers):
            self.model = _FakeSession(providers) if providers is not None else None

    class TextEmbedding:
        def __init__(self, model_name=None, cache_dir=None):
            self.model_name = model_name
            if session_providers is not None:
                self.model = _FakeBackend(session_providers)

        def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    fastembed.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)


def _install_onnxruntime_stub(monkeypatch, available_providers=("CPUExecutionProvider",)):
    onnxruntime_stub = types.ModuleType("onnxruntime")
    onnxruntime_stub.preload_dlls = lambda: None
    onnxruntime_stub.get_available_providers = lambda: list(available_providers)
    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime_stub)
    return onnxruntime_stub


def _reset_preload_latch(monkeypatch):
    # Module-level "only preload once per process" latch — reset it so each
    # test observes its own call (or lack thereof).
    monkeypatch.setattr(embeddings_module, "_onnxruntime_dlls_preloaded", False)


def _reset_providers_logged_latch(monkeypatch):
    monkeypatch.setattr(embeddings_module, "_onnxruntime_providers_logged", False)


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


# --- Regression: providers actually in use must be visible, not silent -----
#
# A "GPU image + preload_dlls() called" does not guarantee onnxruntime
# actually picked CUDAExecutionProvider (a mismatched nvidia-* package
# version, a missing libcuda.so.1 driver mount, etc. all degrade silently to
# CPU otherwise) — per repo convention, a background feature like this needs
# a visible status rather than only being inferable from embedding latency.


def test_fastembed_client_logs_providers_once(monkeypatch, caplog):
    """The INFO line is emitted exactly once per process, even across
    repeated FastEmbedClient() construction — using the plain stub (no
    ``.model`` attribute) that exercises the "could not introspect session"
    fallback branch, which still logs the onnxruntime-reported available
    providers at INFO."""
    _install_fastembed_stub(monkeypatch)
    _reset_preload_latch(monkeypatch)
    _reset_providers_logged_latch(monkeypatch)
    _install_onnxruntime_stub(monkeypatch, available_providers=("CPUExecutionProvider",))

    with caplog.at_level("INFO", logger="src.embeddings"):
        FastEmbedClient()
        FastEmbedClient()

    info_records = [
        r for r in caplog.records
        if r.levelname == "INFO" and "fastembed onnxruntime providers" in r.message
    ]
    assert len(info_records) == 1


def test_fastembed_client_logs_session_providers(monkeypatch, caplog):
    """When the underlying InferenceSession is reachable, the INFO line
    reports its actual (not just onnxruntime's statically-available)
    providers."""
    _install_fastembed_stub(
        monkeypatch, session_providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
    )
    _reset_preload_latch(monkeypatch)
    _reset_providers_logged_latch(monkeypatch)
    _install_onnxruntime_stub(
        monkeypatch, available_providers=("CUDAExecutionProvider", "CPUExecutionProvider")
    )

    with caplog.at_level("INFO", logger="src.embeddings"):
        FastEmbedClient()

    info_records = [
        r for r in caplog.records
        if r.levelname == "INFO" and "fastembed onnxruntime providers" in r.message
    ]
    assert len(info_records) == 1
    assert "CUDAExecutionProvider" in info_records[0].message
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warning_records == []


def test_fastembed_client_warns_when_cuda_available_but_not_selected(monkeypatch, caplog):
    """CUDA shows up in onnxruntime's static provider list (the CUDA image
    is installed) but the session actually fell back to CPU (e.g. the
    driver's libcuda.so.1 isn't mounted) — this must not fail silently."""
    _install_fastembed_stub(monkeypatch, session_providers=["CPUExecutionProvider"])
    _reset_preload_latch(monkeypatch)
    _reset_providers_logged_latch(monkeypatch)
    _install_onnxruntime_stub(
        monkeypatch, available_providers=("CUDAExecutionProvider", "CPUExecutionProvider")
    )

    with caplog.at_level("INFO", logger="src.embeddings"):
        FastEmbedClient()

    warning_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "CUDA execution provider available but not selected" in r.message
    ]
    assert len(warning_records) == 1


def test_fastembed_client_tolerates_provider_logging_errors(monkeypatch):
    """A broken introspection (e.g. get_providers() itself raising) must not
    break embedding client construction."""
    fastembed = types.ModuleType("fastembed")

    class _BoomSession:
        def get_providers(self):
            raise RuntimeError("simulated introspection failure")

    class _BoomBackend:
        model = _BoomSession()

    class TextEmbedding:
        def __init__(self, model_name=None, cache_dir=None):
            self.model = _BoomBackend()

        def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    fastembed.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    _reset_preload_latch(monkeypatch)
    _reset_providers_logged_latch(monkeypatch)
    _install_onnxruntime_stub(monkeypatch)

    client = FastEmbedClient()

    assert client is not None
