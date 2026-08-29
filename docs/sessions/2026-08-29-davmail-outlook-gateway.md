# 2026-08-29 — DavMail-gateway: Outlook.com mail + agenda in Ithaka

**Doel:** ed.de.feber@outlook.com (mail + agenda) via één lokale gateway in
Ithaka, per het aggregatievoorstel (`docs/email-calendar-aggregatie-voorstel.md`).

## Uitkomst

E2E werkend: Outlook-INBOX (388 mails) zichtbaar in het Ithaka-mailpaneel
naast Gmail, SMTP-auth OK, agenda-sync haalt 2 kalenders ("Calendar" +
"Feestdagen in Nederland") en 15 events binnen die in de Calendar-UI renderen.
UI-smoke desktop + 360px mobiel groen, 0 console-errors.

## De weg ernaartoe (voor wie dit ooit opnieuw moet doen)

1. **Device-code-flow is dood voor persoonlijke MSA-accounts.** Elke afronding
   op login.live.com faalt met "The code you entered has expired" — via
   Authenticator-number-match, via wachtwoord, op desktop én telefoon, met
   tenant `common` én `consumers` — terwijl DavMail's polling bewijst dat de
   code server-side nog geldig is ("Authorization pending" blijft komen).
   Vermoedelijk een anti-phishing-blokkade op de MSA-remoteconnect-laag.
   Uren verloren; niet nog eens proberen.
2. **Wat wél werkt: handmatige authorization-code-bootstrap.** Authorize-URL
   met DavMails eigen client_id → normaal inloggen → code van de
   `nativeclient`-redirect → `curl` naar het token-endpoint → refresh token
   in `davmail.properties` schrijven. Volledig recept in
   `docs/email-outlook.md`.
3. **Drie config-lagen die elk stuk waren:**
   - `davmail.enableOidc=true` (v2.0-endpoints) — anders AADSTS500201.
   - `DAVMAIL_TENANT=consumers` — `common` weigert de EWS-scope voor
     persoonlijke accounts (`invalid_scope`).
   - `davmail.enableGraph=true` — de token draagt Graph-scopes; de
     EWS-sessie weigert hem ("Found Graph stored token, incompatible with
     EWS"). Mode-waarde `O365Graph` bestaat niet als geldige `davmail.mode`;
     het juiste pad is mode `O365EWS` + `enableGraph=true` +
     authentication `O365Modern` (die laadt de stored token).
4. **Image 6.8.1 → trunk.** 6.8.1's Graph-CalDAV crasht met een NPE in
   `DateUtil.getExchangeTimeZone` (event zonder tijdzone); de null-guard zit
   alleen op master. `trunk`-tag gepind met comment; terug naar release-tag
   zodra er iets nieuwers dan 6.8.1 uitkomt.
5. **Valkuil:** een mislukte loginpoging laat DavMail de stored
   `refreshToken=`-waarde leegmaken (key blijft staan → `grep -c` zegt "1"
   maar er is geen token). Check de waarde, niet alleen de key.

## Ithaka-kant

- E-mailaccount "Outlook (DavMail)": imap `davmail:1143` starttls off,
  smtp `davmail:1025` security none, user ed.de.feber@outlook.com,
  wachtwoord = lokaal DavMail-koppelwachtwoord (zelfde in alle velden).
- CalDAV-account: `http://davmail:1080/users/ed.de.feber@outlook.com/calendar/`
  (werkt dankzij `ITHAKA_ALLOW_PRIVATE_CALDAV=1` in compose).
- Beide via de API aangemaakt vanuit een ingelogde browsersessie.

## Overig deze sessie

- Docker-stack herbouwd met NVIDIA-GPU-overlay (`COMPOSE_FILE` stond al in
  `.env`); `nvidia-smi` in de container ziet de RTX 5060 Ti (16 GB).
  CUDA-userspace/vLLM installeren blijft een Cookbook-stap.

## Open punten

- Google-agenda als tweede CalDAV-account (app-wachtwoord nodig van Ed).
- DavMail-image terugpinnen naar een release-tag zodra > 6.8.1 uit is.
