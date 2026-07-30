# Skills in Ithaka — audit t.o.v. Claude Code-skills (2026-07-30)

Conclusie vooraf: Ithaka heeft al een volwaardig skills-subsysteem, functioneel
vergelijkbaar met Claude Code-skills en op punten verder (usage-tracking,
teacher-escalation, auto-extractie). De echte gap was distributie/bundeling —
die dekt het nieuwe plugins-formaat (zie spec 2026-07-30).

## Wat er is (met vindplaatsen)

- **Formaat**: `SKILL.md` met YAML-frontmatter per skill, op schijf onder
  `data/skills/<categorie>/<naam>/SKILL.md` (`services/memory/skill_format.py`,
  `skills.py`). Zelfde mentale model als Claude Code.
- **Progressive disclosure**: skill-index (naam+beschrijving, Level 0) gaat de
  prompt in; volledige body wordt lazy geladen. Index en gematchte skills worden
  als *untrusted* context meegestuurd, nooit in het trusted system-message
  (`src/agent_loop.py` `_build_skills_context_message` r1968-2087) — security-
  scheiding die Claude Code niet eens maakt.
- **Automatische relevantie-match**: Jaccard-token-match per beurt
  (`SkillsManager.get_relevant_skills`), begrensd door prefs
  (`skill_max_injected`, `skill_min_confidence`).
- **Slash-commands**: elke skill is `/naam` in de chat-input
  (`static/js/slashAutocomplete.js` ← `GET /api/skills/slash-catalog`,
  `routes/skills_routes.py` r1126).
- **Agent-tool**: `manage_skills` (list/read/create/update) — het model kan
  skills beheren (`src/tools/system.py` r24-243).
- **Beheer-UI**: Brain-paneel → Skills-tab (`static/js/skills.js`): lijst,
  aan/uit-toggle, import vanaf GitHub-URL of skills.sh (map met SKILL.md).
- **Extra's zonder Claude Code-equivalent**: usage-tracking (`_usage.json`),
  auto-extractie uit gesprekken (`skill_extractor.py`), teacher-escalation-audit
  (`skills_routes.py`), per-user enablement, builtin-overrides.

## Gaps t.o.v. Claude Code

1. **Bundeling/distributie** — geen plugin-formaat (skills+MCP samen installeren).
   → Gedekt: plugins-implementatie van vandaag.
2. **Argument-doorgifte bij slash-invocatie** — `/naam <request>` plakt de rest
   als vrije tekst; Claude Code kent `$ARGUMENTS`-substitutie. Impact laag
   (vrije tekst werkt in de praktijk hetzelfde). Quick-win mogelijk maar niet nodig.
3. **Scoped skills per directory/project** — Claude Code laadt skills per repo;
   Ithaka is geen repo-tool, categorie+per-user dekt dit. Geen actie.
4. **Frontmatter-pariteit** — Ithaka-frontmatter is een eigen minimale parser
   (`parse_frontmatter`, geen volledige YAML). Complexe frontmatter (geneste
   lijsten) uit geïmporteerde Claude-skills kan info verliezen. Advies: bij
   plugin-import valideren en onbekende velden bewaren i.p.v. droppen.
5. **Hooks/commands** — Claude Code-plugins bevatten ook hooks en slash-commands
   met scripts. Ithaka-plugins v1 bewust alleen skills+MCP (YAGNI; hooks =
   arbitraire code-executie, aparte security-afweging).

## Advies

- Niets herbouwen; het systeem is compleet en veiliger ontworpen dan de
  Claude Code-tegenhanger (untrusted-context-scheiding).
- Distributie loopt voortaan via plugins (§4 van de spec).
- Overweeg later: frontmatter-preservatie bij import (gap 4) en een
  zichtbaarheids-verbetering (skills-teller op het dashboard).
