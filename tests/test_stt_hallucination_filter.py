"""Whisper hallucinates subtitle-credit phrases ("Ondertiteld door de
Amara.org gemeenschap", "Thanks for watching", ...) when fed silence or
noise — training-data artifacts from YouTube subtitles. In continuous voice
mode those phantom transcripts auto-send as chat messages, so the STT
service must strip them before returning. Silence itself is additionally
suppressed at the source for the local provider via faster-whisper's
built-in VAD filter (tested below via the transcribe kwargs).
"""

from types import SimpleNamespace

from services.stt.stt_service import STTService, strip_stt_hallucinations


# ---- phrase filter -------------------------------------------------------

def test_known_dutch_amara_phrases_become_empty():
    for phrase in (
        "Ondertitels ingediend door de Amara.org gemeenschap",
        "Ondertiteld door de Amara.org gemeenschap",
        "Ondertiteling door de Amara.org gemeenschap",
    ):
        assert strip_stt_hallucinations(phrase) == ""


def test_known_foreign_phrases_become_empty():
    for phrase in (
        "Subtitles by the Amara.org community",
        "Thanks for watching!",
        "Thank you for watching.",
        "Untertitelung des ZDF für funk, 2017",
        "Sous-titres réalisés para la communauté d'Amara.org",
    ):
        assert strip_stt_hallucinations(phrase) == ""


def test_filter_is_case_punctuation_and_whitespace_tolerant():
    assert strip_stt_hallucinations("  ondertiteld door de amara.org gemeenschap.  ") == ""
    assert strip_stt_hallucinations("ONDERTITELS INGEDIEND DOOR DE AMARA.ORG GEMEENSCHAP!") == ""


def test_phrase_appended_to_real_speech_is_stripped():
    got = strip_stt_hallucinations(
        "Zet de schoonmaak in mijn agenda. Ondertiteld door de Amara.org gemeenschap"
    )
    assert got == "Zet de schoonmaak in mijn agenda."


def test_normal_text_is_untouched():
    for text in (
        "Plan een afspraak voor morgen om tien uur",
        "Wat staat er vandaag in mijn agenda?",
        # mentions Amara mid-sentence in real speech — not a credit line
        "Ik las een artikel over de Amara.org gemeenschap en hun werk",
    ):
        assert strip_stt_hallucinations(text) == text


def test_empty_and_none_are_safe():
    assert strip_stt_hallucinations("") == ""
    assert strip_stt_hallucinations(None) == ""


# ---- wiring: local provider ---------------------------------------------

class _FakeWhisper:
    def __init__(self, text):
        self._text = text
        self.seen_kwargs = None

    def transcribe(self, path, **kwargs):
        self.seen_kwargs = kwargs
        info = SimpleNamespace(language="nl", language_probability=0.9)
        segments = [SimpleNamespace(text=self._text)]
        return segments, info


def test_local_transcribe_enables_vad_filter():
    service = STTService()
    fake = _FakeWhisper("hallo daar")
    service._get_whisper = lambda: fake
    assert service._transcribe_local(b"dummy") == "hallo daar"
    assert fake.seen_kwargs.get("vad_filter") is True


def test_local_transcribe_strips_hallucination_to_empty():
    service = STTService()
    fake = _FakeWhisper("Ondertitels ingediend door de Amara.org gemeenschap")
    service._get_whisper = lambda: fake
    assert service._transcribe_local(b"dummy") == ""


# ---- wiring: API-endpoint provider --------------------------------------

def test_api_transcribe_strips_hallucination(monkeypatch):
    service = STTService()

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "Ondertiteld door de Amara.org gemeenschap"}

    import services.stt.stt_service as mod
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _FakeResp())

    class _FakeEp:
        base_url = "http://fake"
        api_key = ""

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return _FakeEp()

    class _FakeDb:
        def query(self, *a, **k):
            return _FakeQuery()

        def close(self):
            pass

    import src.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _FakeDb())

    assert service._transcribe_api(b"dummy", "ep1", "whisper-1", "nl") == ""
