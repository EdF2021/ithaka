"""Mindmap-artifactvalidatie (validator-seam in generate_artifact).

Twee productie-artifacts van 2026-08-20 bewezen dat modellen het gevraagde
mermaid-mindmap-format kunnen negeren (vrije proza, geen fence) — de preview
toonde dan onggerenderde markdown. Zelfde retry-seam als slide_deck (PR #37),
infographic en flashcards.
"""
import pytest

from src.notebook_mindmap import validate_mindmap_markdown

_VALID_MINDMAP_MD = """```mermaid
mindmap
  root((SamenWijzer))
    Doel
      Studiesucces verhogen
      Eigen leerpad
    Pijnpunten
      Gefragmenteerde informatie
      Weinig begeleidingstijd
```
De mindmap toont doelen en pijnpunten van SamenWijzer.
"""


def test_validate_accepts_documented_mindmap():
    validate_mindmap_markdown(_VALID_MINDMAP_MD)  # geen exception


def test_validate_rejects_free_prose():
    with pytest.raises(ValueError, match="mermaid"):
        validate_mindmap_markdown(
            "Dit document fungeert als een zeer uitgebreid strategisch advies "
            "om de integratie van AI te begeleiden."
        )


def test_validate_rejects_fence_without_mindmap_keyword():
    md = _VALID_MINDMAP_MD.replace("mindmap\n", "graph TD\n", 1)
    with pytest.raises(ValueError, match="mindmap"):
        validate_mindmap_markdown(md)


def test_validate_rejects_missing_root():
    md = _VALID_MINDMAP_MD.replace("  root((SamenWijzer))\n", "")
    with pytest.raises(ValueError, match="root"):
        validate_mindmap_markdown(md)


def test_validate_rejects_too_few_branches():
    md = """```mermaid
mindmap
  root((Onderwerp))
    Enige tak
```
Eén zin.
"""
    with pytest.raises(ValueError, match="takken"):
        validate_mindmap_markdown(md)


def test_mindmap_registered_in_kind_validators():
    from src.notebook_artifacts import _KIND_VALIDATORS
    assert _KIND_VALIDATORS.get("mindmap") is validate_mindmap_markdown
