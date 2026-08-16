# Richting 8 — "Dageraad" (cinematische opening, Apple-stijl)

Thesis: de app opent als een film. Een dageraad boven zee die in ~2,5s overvloeit in de
werkplek — daarna is alles kalm, groots en vloeiend. Apple-productpagina-esthetiek:
enorme typografie, veel lucht, alles beweegt met bedoeling en komt tot rust.

## De opening (dit is het hele punt — choreografeer exact)
t=0s: volledig scherm diep nachtblauw `#050B14`, alleen een dunne horizonlijn-glow onderin.
t=0.3-1.2s: de lucht "warmt op": radiale gradiënt (amber `#F0A868` → roze `#D77A8C` →
nachtblauw) stijgt langzaam vanaf de horizon (CSS transform/opacity, geen JS nodig).
t=0.8s: het woord **Ithaka** (72px+, licht gespatieerd) fade+rise in het midden, met
"Yours for the voyage." klein eronder.
t=1.6-2.5s: de titel schaalt terug en glijdt naar de sidebar-positie; tegelijk komen de
werkplek-panelen binnen: sidebar van links (60ms stagger per item), chat-thread-kaarten
rijzen op (12px, opacity, 80ms stagger), composer als laatste van onder.
Daarna: de dageraad-gradiënt blijft als subtiele ambient achtergrond bovenin het canvas.
`prefers-reduced-motion`: sla de sequence over, toon direct de eindstaat.
Herspeel-knop ("Replay intro", klein, in de header) zodat de reviewer het opnieuw kan zien.

## Tokens
- Canvas `#0A1220`, panelen `rgba(255,255,255,0.03)` + 1px `rgba(255,255,255,0.08)`,
  radius 18px. Tekst `#EDF2F8` / `#8FA0B5`.
- Dageraad-accent: amber `#F0A868` (primaire acties, actieve nav) en roze `#D77A8C`
  (spaarzaam: badges). Geen andere kleuren.

## Typografie
Groot en zelfverzekerd: sessietitel 32px/600/-0.03em; berichten 16px/1.65; system-ui.
Mono alleen voor tool-steps en het model-badge.

## Layout & micro-motion na de opening
Standaard shell (sidebar / thread / smalle rechterrail met vandaag+taken). Alles reageert
traag-vloeiend (transition 400-600ms cubic-bezier(0.22,1,0.36,1)): hover tilt kaarten
niet — ze líchten op; nav-wissel cross-fadet de contentzone (démo: Chat en een tweede
"Dashboard"-scene die echt wisselen bij klik); de composer krijgt focus-glow in amber.
Muis-parallax: de dageraad-achtergrond verschuift max 8px mee (traag, lerp in JS).

## Waak tegen
Kitsch (geen zon-schijf, geen meeuwen). De gradiënt is abstract licht, geen illustratie.
Na de opening moet het een serieuze werkplek zijn — de film zit in de overgangen.

Schrijf naar: mockup-8-dageraad.html (zelfde map). Zelfde inhoudseisen als SPEC.md
(inbox-triage-gesprek, tool-chips, model-badge, composer, nav-modules), <60KB.
