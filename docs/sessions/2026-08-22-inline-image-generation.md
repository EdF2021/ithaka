# Sessielog 2026-08-22 — inline beeldgeneratie in de chat

**Branch:** `feat/generate-image-inline-tool` · **Basis:** `e800398`

## Verzoek

Ed: "kan het er voor zorgen dat je in de chat ook images kan genereren." Gekozen scope
(afgestemd): **inline tool** (de assistent maakt zelf een beeld midden in een gesprek), backend
**OpenAI gpt-image-1**.

## Wat er al bleek te bestaan

Beeldgeneratie was verrassend ver bedraad:
- `do_generate_image` (`src/ai_interaction.py`) — werkt, live geverifieerd: gpt-image-1 gaf een
  echt 1024x1024 PNG terug.
- `mcp_servers/image_gen_server.py` — builtin MCP-server die `do_generate_image` omhult en de
  URL in stdout zet.
- `_call_mcp_tool` + `_build_mcp_args` + `_promote_image_fields` (`src/tool_execution.py`) —
  arg-parsing en het liften van de image-URL naar de velden die de agent-loop naar
  `buildImageBubble` forwardt.
- Een **sessiemodus** (`_is_image_generation_session`, `routes/chat_routes.py`): zet je hele
  sessiemodel op `gpt-image-1`/`dall-e-3` → elke boodschap wordt een beeld.

## Root cause waarom het inline niet werkte

Twee lagen:

1. **`generate_image` stond niet in `FUNCTION_TOOL_SCHEMAS`**, en `get_all_openai_schemas`
   (`src/mcp_manager.py:543`) slaat builtin Python MCP-servers over. Native
   function-calling-modellen (GPT/Claude/Gemini — wat Ed gebruikt) kregen de tool dus **nooit**
   aangeboden. Alleen XML-protocol-modellen konden hem via het fenced-block-pad bereiken, en dat
   pad is voor API-modellen bovendien uitgezet (`allow_fenced_for_api` staat alleen aan in
   finetune-modus).

2. **De tool-retrieval mist gangbare NL-formuleringen.** Deze instance draait op de
   fastembed-lane (de HTTP-embedding-lane resolvet naar een lege URL). Gemeten:
   "teken een zeilboot" en "maak een plaatje" → MISS (kregen serve_model/download); "maak een
   afbeelding van een kat" en "generate an image" → HIT. Inconsistent, dus zelfs na fix (1) zou
   de tool voor de helft van de verzoeken niet aangeboden worden.

## Fix

1. **`generate_image` toegevoegd aan `FUNCTION_TOOL_SCHEMAS`.** Verplichte property `prompt` —
   moet zo heten: `function_call_to_tool_block` json.dumpt de args en `_build_mcp_args` decodeert
   ze alleen omdat `prompt` in `_MCP_JSON_PRIMARY_KEYS` staat; een andere naam valt terug op de
   lineparser en zou de hele JSON als prompt behandelen. Geclassificeerd in
   `PUBLIC_ALLOWED_TOOLS` (zelfde klasse als `edit_image`; gegate op route-niveau via
   `can_generate_images` + `image_gen_enabled`).

2. **Keyword-hints in `_KEYWORD_HINTS`** (`src/tool_index.py`) — gekeyd op creatie-werkwoorden +
   werkwoord-frases (NL+EN: teken/draw/maak een afbeelding/genereer een plaatje/...), nadrukkelijk
   niet op kale zelfstandige naamwoorden, zodat beschrijf-intenties ("beschrijf deze afbeelding")
   de tool niet triggeren.

Executie en rendering zijn **niet gewijzigd** — die bestonden al (het XML-pad gebruikt ze):
native call → `function_call_to_tool_block` → `_call_mcp_tool` → image_gen MCP →
`do_generate_image` → `_promote_image_fields` → image-bubble.

## Verificatie

- **Modelgedrag, live op de docker-stack**: gpt-5.1 én gpt-5.4 emitteren allebei een
  `generate_image`-tool_call voor "teken een kat", met een nette NL-prompt in de args.
- **Backend, live**: gpt-image-1 → echt 1024x1024 PNG.
- **Seam** (unit): native call → `function_call_to_tool_block` → `_build_mcp_args` behoudt
  `prompt` (+ model/size/quality), volledig én minimaal.
- **Retrieval** (unit): 10 generatie-formuleringen HIT, 5 beschrijf-intenties vuren niet.
- **Suite: 5029 passed, 3 skipped.** Nieuwe tests met bijtende baseline (12 failures zonder de
  fix).

## Belangrijke caveat voor de gebruiker

De inline-tool vereist een **tool-native model**. Ed's default-chatmodel is `gemma4:latest`, dat
géén tools ondersteunt — daarmee werkt het niet (net zomin als web_search e.d.). Voor inline
beeldgeneratie moet de chat op een cloud-model staan (gpt-5.1, Claude, Gemini). Zeg
bijvoorbeeld "teken een kat" en het beeld verschijnt in de chat.

## Losstaand punt (buiten scope)

De HTTP-embedding-lane resolvet naar een lege URL ("Request URL is missing protocol"), waardoor
alle tool-retrieval op de zwakkere fastembed-lane draait. Los van deze feature, maar het verklaart
waarom de retrieval sowieso brak is; een aparte fix waard.

Ed de Feber, in nauwe samenwerking met Claude
