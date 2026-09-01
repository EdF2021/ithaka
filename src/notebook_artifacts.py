"""Generate text artifacts from the sources of a notebook.

One artifact = one generated markdown Document (so the viewer, versioning and
export come for free) plus a NotebookArtifact row that keeps it tied to its
notebook.

The source text is fed to the model as *untrusted* data: it is uploaded
material, so it goes through src.prompt_security.untrusted_context_message
(the same wrapper the chat pipeline uses for retrieved context) and never into
the system role. Only the hardcoded kind prompt is trusted framing.

The LLM call goes through task_llm_call_async, which resolves the
task -> utility -> default endpoint chain without needing a chat session.
It runs at workload="foreground" (not the task-call default of
"background") because generate_artifact is itself called synchronously
from a tracked foreground request; see the comment at the call site for
why the background workload would self-deadlock.
"""

from __future__ import annotations

import logging
import re
import uuid

from core.database import Document, Notebook, NotebookArtifact, NotebookSource
from src.event_bus import fire_event
from src.notebook_flashcards import validate_flashcards_markdown
from src.notebook_infographic import validate_infographic_markdown
from src.notebook_language import DUTCH_OUTPUT_RULE
from src.notebook_mindmap import validate_mindmap_markdown
from src.notebook_slides import extract_slide_deck
from src.prompt_security import (
    UNTRUSTED_CONTEXT_POLICY,
    _escape_guard_markers,
    untrusted_context_message,
)
from src.task_endpoint import task_llm_call_async

logger = logging.getLogger(__name__)

# Upper bound on the source payload handed to the model. Sources that fit
# their fair share of the budget are kept whole; the number is a
# whole-payload budget, headers and truncation markers included.
MAX_CONTEXT_CHARS = 60_000

_SOURCE_HEADER = "=== BRON: {filename} ==="
_TRUNCATION_MARKER = "\n(bron ingekort)"
_BLOCK_SEPARATOR = "\n\n"


def _strip_think_blocks(text: str) -> str:
    """Remove ``<think>...</think>`` reasoning blocks from a model answer.

    Kopie van agent_loop._strip_think_blocks (src/agent_loop.py) - not
    imported directly because agent_loop pulls in the full tool-execution
    import graph (src.agent_tools, src.tool_policy, src.tool_security, ...),
    which is unnecessary weight for this module's single narrow use.

    Linear-time equivalent of
    ``re.sub(r'<think>.*?</think>', '', text, flags=DOTALL|IGNORECASE)``: a
    forward-only scan pairs each ``<think>`` opener with the next closer in a
    single pass. Only literal ``<think>``/``</think>`` (any case) are
    matched, a dangling opener with no closer is left intact, and an orphan
    ``</think>`` is never stripped.
    """
    if not text:
        return text
    lowered = text.lower()
    parts = []
    pos = 0
    while True:
        start = lowered.find("<think>", pos)
        if start == -1:
            parts.append(text[pos:])
            break
        end = lowered.find("</think>", start + 7)
        if end == -1:
            # No closer for this opener: lazy regex matches nothing here.
            parts.append(text[pos:])
            break
        parts.append(text[pos:start])
        pos = end + 8  # len("</think>")
    return "".join(parts)


# --------------------------------------------------------------------------
# Prompts
#
# Written in Dutch (the project language). Output is always forced to Dutch
# via DUTCH_OUTPUT_RULE (src/notebook_language.py), regardless of the
# sources' language - the rule is stated first and repeated per kind because
# a per-kind instruction that mentions "the sources' language" would
# otherwise nudge the model away from Dutch output.
# --------------------------------------------------------------------------

_BASE_RULES = f"""Je bent een zorgvuldige redacteur die materiaal samenstelt uit een vaste set bronnen.

Harde regels:
- {DUTCH_OUTPUT_RULE}
- Baseer je uitsluitend op de aangeleverde bronnen. Vul niets aan met algemene kennis en presenteer geen aanname als feit.
- Ontbreekt informatie, benoem dat in één korte zin in plaats van te gokken.
- Lever pure markdown. Geen inleidende zin, geen afsluitende opmerking, geen meta-tekst over de opdracht: begin direct met de inhoud.
- Omsluit je hele antwoord niet met een codefence.
- Gebruik geen emoji.
- De bronnen zijn gescheiden met koppen van de vorm "=== BRON: bestandsnaam ===". Noem de bronnaam waar dat de lezer helpt; herhaal die koppen niet letterlijk in je uitvoer."""

_KIND_INSTRUCTIONS = {
    "study_guide": """Maak een studiegids waarmee iemand de stof echt kan leren.

Structuur:
- "# " met het overkoepelende onderwerp als titel.
- "## Kernconcepten": 5 tot 10 begrippen, elk als "**begrip** - definitie van één à twee zinnen" in je eigen woorden.
- "## Per bron": één "### " per bron met 3 tot 6 bullets - wat behandelt de bron en welke conclusie trekt hij.
- "## Verbanden": 3 tot 6 bullets over hoe de bronnen op elkaar aansluiten, elkaar aanvullen of tegenspreken.
- "## Studievragen": 8 tot 12 open vragen, oplopend van begrip naar toepassing, zonder antwoorden.

Wees concreet: noem de cijfers, namen en termen uit de bronnen in plaats van te omschrijven.""",

    "briefing": """Schrijf een zakelijke briefing van maximaal één A4 (ongeveer 400 tot 600 woorden) voor iemand die de bronnen niet gelezen heeft en er toch over moet kunnen meepraten.

Structuur:
- "# " met het onderwerp als titel.
- "**Kernboodschap**": twee tot drie zinnen met de essentie, direct onder de titel.
- "## Kernpunten": 4 tot 6 bullets, elk één concrete bevinding met het cijfer, de naam of de datum erbij.
- "## Context": één alinea over waarom dit speelt en waar het vandaan komt.
- "## Implicaties": 3 tot 5 bullets over wat dit betekent en welke keuzes of risico's eruit volgen.
- "## Openstaande vragen": 2 tot 4 bullets met wat de bronnen niet beantwoorden.

Schrijf zakelijk en stellig. Vermijd vulwoorden en formuleringen die niets toevoegen.""",

    "faq": """Stel een FAQ samen van 8 tot 12 vraag-en-antwoordparen die de bronnen daadwerkelijk beantwoorden.

Structuur:
- "# " met een titel die "veelgestelde vragen" uitdrukt in het Nederlands.
- Per paar een "### " met de vraag, daaronder het antwoord als gewone alinea van twee tot vijf zinnen.

Regels:
- Formuleer de vraag zoals een lezer hem echt zou stellen, als volledige vraagzin met vraagteken.
- Begin met de meest voor de hand liggende vragen en werk toe naar detail en randgevallen.
- Geen twee vragen die in de kern hetzelfde beantwoorden.
- Antwoord volledig maar bondig: het antwoord moet op zichzelf te lezen zijn.""",

    "quiz": """Maak een toets van 8 tot 10 vragen waarmee iemand kan nagaan of hij de stof beheerst.

Structuur:
- "# " met een titel die "toets" of "quiz" uitdrukt in het Nederlands.
- Daarna de genummerde vragen, 1 tot en met N, elk als "**1.** vraagtekst".
- Meerkeuzevragen krijgen de opties eronder als bullets "A) ...", "B) ...", "C) ...", "D) ...".
- Sluit af met een kop "## " gevolgd door het woord voor "antwoorden" in het Nederlands, met daaronder de genummerde antwoorden, elk met één zin toelichting waarom dat het antwoord is.

Regels:
- Varieer tussen meerkeuze en open vragen en toets begrip en toepassing, niet alleen losse feitjes.
- Zet het antwoord nooit bij de vraag zelf: alle antwoorden staan onderaan in de antwoordsectie.
- Maak de afleiders bij meerkeuze plausibel; geen opties die er duidelijk naast zitten.
- Gebruik uitsluitend markdown, geen HTML-tags.""",

    "mindmap": """Maak een mindmap van de bronnen als één mermaid-diagram.

Lever exact twee dingen, in deze volgorde en niets anders:
1. Eén codefence met taalaanduiding "mermaid", waarvan de eerste regel in de fence "mindmap" is.
2. Onder de fence één regel gewone tekst die in één zin zegt wat de mindmap toont.

Regels voor het diagram:
- Wortel: "root((Onderwerp))" met een onderwerp van één tot drie woorden.
- Maximaal drie niveaus onder de wortel: 4 tot 8 hoofdtakken, elk met 2 tot 5 subtakken.
- Labels zijn kort: één tot vier woorden, geen hele zinnen.
- Gebruik in labels uitsluitend letters, cijfers, spaties en koppeltekens. Geen haakjes, dubbele punten, komma's, punten, puntkomma's, aanhalingstekens, accolades, slashes of ampersands: die breken de mermaid-parser.
- De hiërarchie komt uitsluitend uit inspringing met spaties. Geen tabs, geen streepjes, geen opsommingstekens.

Voorbeeld van de vorm (niet van de inhoud):

```mermaid
mindmap
  root((Onderwerp))
    Eerste tak
      Detail een
      Detail twee
    Tweede tak
      Detail drie
```""",

    "infographic": """Maak een infographic: een compacte, visueel scanbare pagina met de kern van de bronnen in cijfers en korte feiten.

Structuur, exact in deze volgorde en met exact deze koppen (de renderer parst op deze structuur):
- "# " met een pakkende titel in het Nederlands.
- "## Key numbers": 3 tot 5 bullets, elk exact in de vorm "- **<getal, percentage of korte metric>** — <label van maximaal 8 woorden>". Zijn er geen cijfers in de bronnen, gebruik dan een telwoord of kort feit als "getal" (bijvoorbeeld "3 panelen" of "geen vermeld") - verzin nooit een cijfer dat niet in de bronnen staat.
- Daarna 3 tot 4 gewone secties, elk "## <sectiekop>" met 2 tot 4 korte bullet-feiten.
- Afsluitend één blockquote-regel "> " met één kernboodschap in één zin.

Regels:
- Elk "key number" en elk bullet-feit moet herleidbaar zijn tot de bronnen; geen verzonnen cijfers of aannames.
- Houd bullets kort en concreet - geen volledige alinea's.
- Gebruik uitsluitend de koppen "## Key numbers" en de overige sectiekoppen; geen extra kopniveaus (geen "###").""",

    "flashcards": """Maak 10 tot 15 flashcards waarmee iemand de kernbegrippen uit de bronnen kan oefenen.

Structuur (de renderer parst op deze vorm):
- "# " met een titel in het Nederlands.
- Per kaart een "### " met de voorzijde (een vraag of begrip), daaronder de achterzijde als één of twee gewone alinea's.

Regels:
- De voorzijde is kort: één vraagzin of één begrip, geen opsommingen.
- De achterzijde is op zichzelf te begrijpen: twee tot vier zinnen, geen verwijzingen als "zie boven".
- Dek de belangrijkste begrippen, cijfers en verbanden uit de bronnen; geen twee kaarten over hetzelfde.
- Gebruik uitsluitend "# " en "### " als koppen; geen bullets op de achterzijde, geen HTML.""",

    "data_table": """Maak een gegevenstabel: de concrete feiten, cijfers en kenmerken uit de bronnen als overzichtelijke markdown-tabellen.

Structuur:
- "# " met een titel in het Nederlands.
- Eén of meer markdown-tabellen, elk voorafgegaan door een "## " sectiekop die zegt wat de tabel toont.
- Kies kolommen die bij het brontype passen (bijvoorbeeld indicator/waarde/bron, of begrip/definitie/voorbeeld).
- Sluit af met één regel gewone tekst die de belangrijkste observatie uit de tabellen benoemt.

Regels:
- Elke celwaarde moet herleidbaar zijn tot de bronnen; ontbreekt een waarde, schrijf dan een streepje "-", verzin niets.
- Houd cellen kort: geen volledige zinnen in cellen, toelichting hoort in de sectiekop of de slotregel.
- Gebruik uitsluitend markdown-tabellen met "|"-syntax; geen HTML-tabellen.""",

    "slide_deck": """Maak een diapresentatie van 6 tot 12 slides die de kern van de bronnen presenteert.

Lever exact één codefence met taalaanduiding "json" en daarin één JSON-object, niets anders. Schema:

{
  "title": "presentatietitel in het Nederlands",
  "slides": [
    {
      "title": "slidetitel",
      "bullets": ["punt een", "punt twee"],
      "notes": "sprekersnotitie van twee tot vier zinnen"
    }
  ]
}

Regels:
- Alle tekstvelden in het Nederlands.
- 6 tot 12 slides; de eerste slide introduceert het onderwerp, de laatste vat samen of concludeert.
- Per slide 2 tot 5 bullets van elk maximaal 12 woorden; geen volledige zinnen met punt erachter.
- "notes" is de uitgeschreven toelichting die een spreker bij de slide zou vertellen - op zichzelf begrijpelijk.
- Elk feit moet herleidbaar zijn tot de bronnen; verzin niets.
- Geen markdown of HTML binnen de JSON-strings; alleen platte tekst.""",

    "report": """Maak een rapport op basis van de bronnen.

Structuur:
- "# " met een titel in het Nederlands die bij het onderwerp past.
- Volg de indeling-instructie die in het bericht hierna is meegegeven voor structuur, secties, stijl en toon. Geeft die geen duidelijke sectie-indeling, kies dan zelf een heldere indeling met "## "-koppen die recht doet aan de bronnen.
- Gebruik doorlopende alinea's; gebruik bullets of een tabel alleen waar dat de leesbaarheid echt dient.

Bronverwijzingen:
- Voor dit rapport zijn de bronnen genummerd met koppen van de vorm "=== BRON [n]: bestandsnaam ===" (in plaats van de ongenummerde koppen hierboven).
- Citeer elke bewering die op een bron steunt met "[n]", waarbij n het bronnummer uit die kop is - direct achter de zin of het zinsdeel dat de bewering bevat.
- Sluit het rapport af met een kop "## Bronnen" met daaronder één regel per gebruikte bron, in de vorm "[n] bestandsnaam".

Regels:
- Is er geen indeling-instructie meegegeven, schrijf dan een overzichtelijk, zakelijk rapport van 500 tot 900 woorden.""",
}

_KIND_LABELS = {
    "study_guide": "Studiegids",
    "briefing": "Briefing",
    "faq": "FAQ",
    "quiz": "Quiz",
    "mindmap": "Mindmap",
    "infographic": "Infographic",
    "flashcards": "Flashcards",
    "data_table": "Gegevenstabel",
    "slide_deck": "Diapresentatie",
    "report": "Rapport",
}

# Post-generation format validators: kind -> callable that raises ValueError
# on a format miss. generate_artifact retries (with the error fed back) up to
# _VALIDATION_ATTEMPTS times before giving up - same recovery shape as the
# podcast script-format retry in src/notebook_audio.py.
_KIND_VALIDATORS = {
    "slide_deck": extract_slide_deck,
    # Free-prose / wrong-heading model output used to be stored as-is and
    # then surfaced as raw markdown (infographic fallback card, one-card
    # flashcard deck, unrendered mindmap) — 2026-08-20..23 production
    # regressions with format-ignoring models.
    "infographic": validate_infographic_markdown,
    "flashcards": validate_flashcards_markdown,
    "mindmap": validate_mindmap_markdown,
}
_VALIDATION_ATTEMPTS = 3

# kind -> {label, prompt}. Insertion order is the order the UI lists them in.
ARTIFACT_KINDS = {
    kind: {
        "label": _KIND_LABELS[kind],
        "prompt": f"{_BASE_RULES}\n\n{instruction}",
    }
    for kind, instruction in _KIND_INSTRUCTIONS.items()
}


# --------------------------------------------------------------------------
# Request-timeout exemption
#
# generate_artifact runs synchronously inside the artifacts-POST request and
# can legitimately take longer than app.py's REQUEST_HARD_TIMEOUT (45s), so
# that route needs an exemption from _RequestTimeoutMiddleware. Deliberately
# narrow (this route only, not a broad "/api/notebooks" prefix): a prefix
# exemption would also cover source upload/ingest, which should keep the
# hard timeout. Lives here (not in app.py) so it stays unit-testable without
# importing the full app module.
# --------------------------------------------------------------------------

ARTIFACTS_GENERATE_PATH_RE = re.compile(r"^/api/notebooks/[^/]+/artifacts$")


def is_artifacts_generate_request(method: str, path: str) -> bool:
    """True only for POST /api/notebooks/{id}/artifacts."""
    return (method or "").upper() == "POST" and bool(ARTIFACTS_GENERATE_PATH_RE.match(path or ""))


# --------------------------------------------------------------------------
# Source collection
# --------------------------------------------------------------------------

def _source_entries(notebook: Notebook, db_session) -> list[tuple[str, str]]:
    """Return [(filename, text)] for the notebook's usable sources.

    Only sources that were indexed successfully *and* still have a backing
    Document with content qualify - a failed upload or a source whose Document
    was deleted from the Library has no full text to summarize.
    """
    rows = (
        db_session.query(NotebookSource)
        .filter(
            NotebookSource.notebook_id == notebook.id,
            NotebookSource.status == "indexed",
            NotebookSource.document_id.isnot(None),
        )
        .order_by(NotebookSource.created_at, NotebookSource.filename)
        .all()
    )
    entries = []
    for src in rows:
        doc = db_session.get(Document, src.document_id)
        if doc is None:
            continue
        text = (doc.current_content or "").strip()
        if not text:
            continue
        entries.append((src.filename, text))
    return entries


def _assemble_source_blocks(headers: list[str], entries: list[tuple[str, str]]) -> str:
    """Water-filling assembly shared by gather_source_text and
    gather_source_text_numbered, capped at MAX_CONTEXT_CHARS.

    Joins each header with its entry's document text. When the total exceeds
    the cap, only the sources that would not fit their fair share of the
    remaining budget are truncated (and marked with "(bron ingekort)"); a
    source that fits is kept complete and unmarked, even while a larger
    sibling gets cut. This is a water-filling pass over sources sorted
    ascending by length: each source in turn either keeps its full text
    (consuming only what it needs, so leftover budget grows for the sources
    still to come) or is cut to that step's fair share.

    `headers` and `entries` must be the same length and in the same order —
    the truncation math only looks at `entries`' text lengths, so the two
    callers differ solely in what header string they hand in per source
    (unnumbered "=== BRON: ... ===" vs. numbered "=== BRON [n]: ... ===");
    factored out here so that difference can't let the shared truncation
    logic drift between them.
    """
    # The cap covers the whole payload, so headers and separators are spent
    # before any text budget is handed out.
    overhead = sum(len(h) for h in headers) + len(_BLOCK_SEPARATOR) * (len(entries) - 1)
    total_text = sum(len(text) for _, text in entries)

    if overhead + total_text <= MAX_CONTEXT_CHARS:
        return _BLOCK_SEPARATOR.join(
            header + text for header, (_, text) in zip(headers, entries)
        )

    # Budget left for text (+ truncation markers) once headers/separators are
    # paid for. Can be <= 0 when the overhead alone already meets or exceeds
    # the cap (many/long filenames); clamp so nothing downstream goes
    # negative.
    text_budget = max(MAX_CONTEXT_CHARS - overhead, 0)
    marker_cost = len(_TRUNCATION_MARKER)

    limits = [None] * len(entries)  # None = keep full text
    marked = [False] * len(entries)
    order = sorted(range(len(entries)), key=lambda i: len(entries[i][1]))
    remaining_budget = text_budget
    remaining_count = len(entries)
    for idx in order:
        text_len = len(entries[idx][1])
        fair_share = remaining_budget // remaining_count if remaining_count else 0
        if text_len <= fair_share:
            # Fits its fair share whole: no truncation, no marker. Only the
            # text it actually needs is spent, so the rest is redistributed
            # to the sources still waiting their turn.
            remaining_budget -= text_len
        else:
            marked[idx] = True
            limits[idx] = max(fair_share - marker_cost, 0)
            remaining_budget -= fair_share
        remaining_count -= 1

    blocks = []
    for header, (idx, (_, text)) in zip(headers, enumerate(entries)):
        if marked[idx]:
            blocks.append(header + text[: limits[idx]] + _TRUNCATION_MARKER)
        else:
            blocks.append(header + text)
    result = _BLOCK_SEPARATOR.join(blocks)

    # Defensive final clamp: guards the degenerate case (overhead alone at or
    # above the cap, or floor-division rounding) so the function never hands
    # back more than the configured budget.
    if len(result) > MAX_CONTEXT_CHARS:
        result = result[:MAX_CONTEXT_CHARS]
    return result


def gather_source_text(notebook: Notebook, db_session) -> str:
    """Build the source payload for the model, capped at MAX_CONTEXT_CHARS.

    Blocks are "=== BRON: <filename> ===" headers followed by the document's
    full text; the water-filling truncation strategy is
    _assemble_source_blocks (see its docstring). Returns "" when the
    notebook has no usable sources.
    """
    entries = _source_entries(notebook, db_session)
    if not entries:
        return ""

    headers = [_SOURCE_HEADER.format(filename=name) + "\n" for name, _ in entries]
    return _assemble_source_blocks(headers, entries)


# --------------------------------------------------------------------------
# Report-kind source collection (numbered headers for citations)
#
# Only kind="report" uses this. Every other kind keeps calling
# gather_source_text/_SOURCE_HEADER above, whose output stays byte-for-byte
# unchanged - the two share only the water-filling truncation math
# (_assemble_source_blocks); each builds its own header strings.
# --------------------------------------------------------------------------

_SOURCE_HEADER_NUMBERED = "=== BRON [{n}]: {filename} ==="


def gather_source_text_numbered(notebook: Notebook, db_session) -> tuple[str, int]:
    """Report-kind variant of gather_source_text: numbered source headers.

    Same water-filling truncation, cap (MAX_CONTEXT_CHARS) and skip rules as
    gather_source_text (both delegate to _assemble_source_blocks), but
    headers are "=== BRON [n]: filename ===" (n = 1-based position in source
    order, matching _source_entries' ordering) instead of the shared
    "=== BRON: filename ===", so the report kind's citation instruction
    (_KIND_INSTRUCTIONS["report"]) can point "[n]" back at a specific
    numbered source.

    Returns (payload, source_count): source_count is the number of sources
    included (0 when the notebook has no usable sources, mirroring
    gather_source_text's "" return). validate_report_markdown uses it to
    bound citation numbers.
    """
    entries = _source_entries(notebook, db_session)
    if not entries:
        return "", 0

    headers = [
        _SOURCE_HEADER_NUMBERED.format(n=i, filename=name) + "\n"
        for i, (name, _) in enumerate(entries, start=1)
    ]
    return _assemble_source_blocks(headers, entries), len(entries)


# --------------------------------------------------------------------------
# Report-kind citation validator
# --------------------------------------------------------------------------

# A bare "[n]" (digits only) not immediately followed by "(" - excludes
# markdown links "[tekst](url)" and numeric-text links "[1](url)". Footnote
# syntax "[^1]" is excluded too, since "^" isn't a digit so \d+ never matches
# there.
_CITATION_RE = re.compile(r"\[(\d+)\](?!\()")


def validate_report_markdown(text: str, source_count: int) -> str | None:
    """Check that every "[n]" citation in a report artifact points at an
    existing numbered source.

    Report kind only. gather_source_text_numbered numbers sources 1..N in
    generation order; the report instruction (_KIND_INSTRUCTIONS["report"])
    asks the model to cite claims as "[n]" against that numbering. This only
    bounds those citation numbers - it does NOT require any citations and
    does NOT require a "## Bronnen" section, because a layout_instruction
    may legitimately steer the report away from either, and the retry loop
    (_VALIDATION_ATTEMPTS, see generate_artifact) must not get stuck
    demanding something the instruction told the model to skip.

    Unlike the other validate_*_markdown functions (which raise ValueError
    and are registered directly in _KIND_VALIDATORS), this one returns
    instead of raising: source_count is per-call (the notebook's actual
    source count), not known at module-import time, so generate_artifact
    wraps this in a small closure for the retry loop rather than registering
    it in _KIND_VALIDATORS.

    Returns None when every citation number is within 1..source_count (or
    there are none at all), otherwise a Dutch error string naming the
    out-of-range number(s) - fed back to the model on retry, same shape as
    the other validators' ValueError messages.
    """
    numbers = {int(m.group(1)) for m in _CITATION_RE.finditer(text or "")}
    bad = sorted(n for n in numbers if not (1 <= n <= source_count))
    if not bad:
        return None
    bad_list = ", ".join(f"[{n}]" for n in bad)
    return (
        f"citatie(s) {bad_list} verwijzen naar een bron die niet bestaat "
        f"(er zijn {source_count} genummerde bronnen; gebruik alleen "
        f"[1] t/m [{source_count}])"
    )


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

async def generate_artifact(
    notebook_id: str, owner: str, kind: str, db_session, focus: str | None = None,
    layout_instruction: str | None = None,
) -> NotebookArtifact:
    """Generate one artifact for `notebook_id` and return its NotebookArtifact.

    Rows are written only after the model answered: a failed or empty LLM call
    leaves no Document and no artifact behind. Regenerating the same kind adds
    a new artifact rather than overwriting the old one.

    `layout_instruction` is only used when kind="report" (the "Rapport
    maken" flow in src/notebook_report_layouts.py): a fixed template's, an
    AI-recommended layout's, or a user-typed instruction describing the
    report's structure, style and tone. It is appended as extra instruction
    in the user role (not the system role), escaped via
    _escape_guard_markers first since it can carry LLM-generated text traced
    back to untrusted source content.

    Raises ValueError for an unknown kind, an unknown/foreign notebook, or a
    notebook without usable sources; RuntimeError when the model returns
    nothing. Endpoint failures propagate from task_llm_call_async.

    Note: db_session is the caller's pooled connection (SessionLocal from
    routes/notebook_routes.py) and is deliberately held open across the LLM
    call below, not closed/reopened around it. This is a single-user app on
    SQLAlchemy's default QueuePool (5 + 10 overflow), so one connection
    parked for the duration of a generation request is not a starvation risk.
    """
    spec = ARTIFACT_KINDS.get(kind)
    if spec is None:
        raise ValueError(f"Onbekend artifact-type: {kind}")

    notebook = (
        db_session.query(Notebook)
        .filter(Notebook.id == notebook_id, Notebook.owner == owner)
        .first()
    )
    if notebook is None:
        raise ValueError("Notebook niet gevonden")

    if kind == "report":
        # Numbered "=== BRON [n]: ... ===" headers so the report's citation
        # instruction can point "[n]" at a specific source; source_count
        # bounds validate_report_markdown's accepted citation range below.
        # Every other kind keeps gather_source_text/_SOURCE_HEADER unchanged.
        source_text, source_count = gather_source_text_numbered(notebook, db_session)
    else:
        source_text = gather_source_text(notebook, db_session)
        source_count = None
    if not source_text:
        raise ValueError("Geen geïndexeerde bronnen")

    user_msg = untrusted_context_message(f"notebook-bronnen: {notebook.name}", source_text)
    if focus and focus.strip():
        focus_instruction = (
            f"\n\nAanvullende instructie: focus de mindmap op het volgende "
            f"onderwerp of aspect: {focus.strip()}. Pas de structuur en "
            f"inhoud van de mindmap aan zodat dit aspect centraal staat, "
            f"maar behoud het mermaid-mindmap formaat."
        )
        user_msg = {"role": user_msg["role"], "content": user_msg["content"] + focus_instruction}
    if kind == "report" and layout_instruction and layout_instruction.strip():
        # layout_instruction isn't always user-typed: it can be an
        # AI-recommended layout's `instruction` field, itself LLM output
        # generated from untrusted notebook source content (see
        # src/notebook_report_layouts.py), cached, and posted back verbatim.
        # Escape guard-marker literals before it lands in this trusted zone
        # of the message, or a malicious source document could steer the
        # suggestion call into laundering guard markers into the
        # report-generation call below.
        safe_instruction = _escape_guard_markers(layout_instruction.strip())
        layout_instruction_text = (
            f"\n\nIndeling-instructie voor dit rapport: {safe_instruction} "
            f"Volg deze instructie voor de structuur, stijl en toon van het rapport."
        )
        user_msg = {"role": user_msg["role"], "content": user_msg["content"] + layout_instruction_text}
    messages = [
        {"role": "system", "content": f"{UNTRUSTED_CONTEXT_POLICY}\n\n{spec['prompt']}"},
        user_msg,
    ]
    # This call runs inside the artifacts-POST request itself, which the
    # interactive-activity middleware already counts as a tracked foreground
    # request (app.py's _InteractiveActivityMiddleware). The default gate in
    # task_llm_call_async waits for foreground traffic to go quiet before
    # running a background-task LLM call - but that wait would be waiting on
    # this very request's own _ACTIVE_REQUESTS entry to clear, which never
    # happens until this call returns. wait_for_quiet=False skips that gate:
    # the gate is for genuine background jobs (scheduler, email pollers),
    # not for an in-request caller like this one.
    #
    # A second, deeper gate lives in _local_model_slot (src/llm_core.py):
    # task_llm_call_async defaults kwargs["workload"] to "background", and for
    # LOCAL endpoints that makes the call wait `while has_foreground_activity()`
    # (interactive_gate.py), which is also True for the whole lifetime of this
    # request - a second self-deadlock (capped at 600s) on top of the first.
    # workload="foreground" tells the local-model slot this is a synchronous
    # user-facing call, not a genuine background job, so it does not wait on
    # its own request's activity flag.
    if kind == "report":
        # validate_report_markdown returns str|None (not raise) and needs
        # source_count, which isn't known until generation time - so it
        # can't sit in the static _KIND_VALIDATORS dict like the others.
        # This closure adapts it to the raise-based contract the retry loop
        # below expects, keeping that loop uniform across every kind.
        def validator(text: str) -> None:
            error = validate_report_markdown(text, source_count)
            if error:
                raise ValueError(error)
    else:
        validator = _KIND_VALIDATORS.get(kind)
    content = ""
    last_error = ""
    for attempt in range(_VALIDATION_ATTEMPTS):
        attempt_messages = list(messages)
        if attempt > 0:
            # Same shape as the podcast script-format retry (PR #32): feed the
            # rejected answer plus the validation error back so the model can
            # correct the format instead of guessing blind.
            attempt_messages.append({"role": "assistant", "content": content})
            attempt_messages.append({
                "role": "user",
                "content": (
                    "Je vorige antwoord voldeed niet aan het gevraagde formaat "
                    f"({last_error}). Lever het antwoord opnieuw, exact volgens de "
                    "instructie hierboven."
                ),
            })
        content = await task_llm_call_async(
            attempt_messages, owner=owner, wait_for_quiet=False, workload="foreground"
        )
        content = _strip_think_blocks(content or "").strip()
        if not content:
            last_error = "leeg antwoord"
            continue
        if validator is None:
            break
        try:
            validator(content)
            break
        except ValueError as e:
            last_error = str(e)
            logger.info(
                "Artifact %s: formaat-misser op poging %d/%d: %s",
                kind, attempt + 1, _VALIDATION_ATTEMPTS, last_error,
            )
            continue
    else:
        raise RuntimeError(
            f"Het model leverde geen geldig antwoord na {_VALIDATION_ATTEMPTS} pogingen"
            + (f" ({last_error})" if last_error else "")
        )
    if not content:
        raise RuntimeError("Het model gaf een leeg antwoord terug")

    document_id = str(uuid.uuid4())
    document_title = f"{notebook.name} — {spec['label']}"
    db_session.add(Document(
        id=document_id,
        title=document_title,
        owner=owner,
        language="markdown",
        current_content=content,
        session_id=None,
    ))
    artifact = NotebookArtifact(
        id=str(uuid.uuid4()),
        notebook_id=notebook.id,
        document_id=document_id,
        kind=kind,
        # Own title, seeded from the same value as the Document's — a fixed,
        # renamable title from the start rather than a NULL that only ever
        # falls back to the (also renamable, but conceptually separate)
        # Document title. See NotebookArtifact.title in core/database.py.
        title=document_title,
    )
    db_session.add(artifact)
    db_session.commit()

    # After the commit: the Library refresh must not be able to roll back or
    # lose a stored artifact.
    try:
        fire_event("document_created", owner)
    except Exception as exc:
        logger.warning("document_created event failed for artifact %s: %s", artifact.id, exc)
    return artifact
