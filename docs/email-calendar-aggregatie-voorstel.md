# Voorstel: Gmail + Outlook mail & agenda centraal in Ithaka

*Onderzoek 2026-08-29 — vijf parallelle research-agents (Nango, Stalwart,
alternatievenlandschap, open-source unified-API's incl. Kurrier, en een
Ithaka-codebase-inventaris). Bronnen per onderdeel in de sectie "Onderzochte
kandidaten".*

## TL;DR

Er is geen goede open-source "unified communications API" die Gmail + Outlook
mail én agenda achter één self-hosted laag verenigt — die categorie is dood
(Panora/Supaglue/Revert gearchiveerd), cloud-only (Nylas/Unipile/Aurinko/
Composio), betaald (EmailEngine €995/jr) of te zwaar zonder winst (Nango).
Maar dat is geen probleem, want **het gat in Ithaka is veel kleiner dan
gedacht**: Gmail-mail (IMAP/SMTP + XOAUTH2) en Google Calendar (CalDAV met
Google-endpoint-mapping) werken al native. Alleen Microsoft ontbreekt volledig.

**Aanbeveling: DavMail als Outlook-gateway** — één extra container in de
bestaande compose-stack die Outlook.com vertaalt naar exact de drie protocollen
die Ithaka al spreekt (IMAP/SMTP/CalDAV). Geen nieuwe code in Ithaka nodig,
geen eigen Microsoft Entra app-registratie, geen Google Cloud-project.

## Doelarchitectuur

```
Gmail ───────── IMAP/SMTP (XOAUTH2 of app-wachtwoord) ──► Ithaka e-mail   [bestaat al]
Google Calendar ─ CalDAV (app-wachtwoord) ───────────────► Ithaka agenda  [bestaat al]
Outlook.com ──── OAuth (DavMail regelt zelf) ──► DavMail-container
                        DavMail ── IMAP/SMTP/CalDAV (lokale basic auth) ──► Ithaka
```

Eén centrale laag voor het déél dat een laag nodig heeft; de rest direct.

## Waarom DavMail

- **Exact passend**: gateway die Exchange/M365/Outlook.com ontsluit als
  IMAP/SMTP/CalDAV/CardDAV/LDAP — de protocollen die Ithaka's bestaande
  multi-account e-mail- (`routes/email_helpers.py`) en CalDAV-clients
  (`src/caldav_sync.py`) al spreken.
- **Auth opgelost**: `O365Modern`-modus ondersteunt persoonlijke
  live.com/outlook.com-accounts (device-code + browser-consent, token-cache
  met refresh). Geen eigen Entra-app-registratie. Dit is precies het gat:
  Outlook.com basic auth is dood (IMAP sinds 2022, SMTP sinds maart 2026) en
  Ithaka heeft nul Microsoft-code.
- **Actief onderhouden**: v6.6.0 (apr 2026) / v6.7.0 (mei 2026) volgden
  Microsofts OAuth-wijzigingen; GPL-2.0; Docker-compose-voorbeelden bestaan.
- **Licht**: één JVM-container naast de bestaande stack; poorten alleen
  intern (geen expose buiten het compose-netwerk).

## Implementatieplan (gefaseerd, klein)

1. **DavMail-service in `docker-compose.yml`** — interne poorten (IMAP 1143,
   SMTP 1025, CalDAV 1080), `O365Modern`, token-cache op een volume.
   Eenmalige interactieve device-code-login per Outlook-account.
2. **Outlook-mail in Ithaka** — gewoon een `EmailAccount` toevoegen:
   IMAP-host `davmail`, poort 1143, lokale credentials. Geen codewijziging.
3. **Outlook-agenda in Ithaka** — CalDAV-account toevoegen met URL
   `http://davmail:1080/users/<mail>/calendar`. Geen codewijziging
   (bestaande two-way sync, 90d terug / 365d vooruit).
4. **Google Calendar activeren** (als nog niet gedaan) — CalDAV-account met
   app-wachtwoord; de Google-endpoint-mapping zit al in `src/caldav_sync.py`.
5. **Optioneel later**: kleine UI-preset "Outlook via DavMail" in de
   accountformulieren + een gotcha-sectie in CLAUDE.md/docs; evt. een
   ICS-feed-poller als read-only fallback voor extra agenda's (bestaat nu
   niet — alleen file-import).

Moeite-inschatting: **laag-middel** (stap 1-4 is configuratie + één
compose-wijziging; risico zit in de eenmalige OAuth-dans en DavMail-tuning).

## Onderzochte kandidaten (waarom niet)

| Kandidaat | Oordeel |
|---|---|
| **Nango** | Self-hosted gratis = alleen Auth+Proxy; syncs ("Functions") achter paywall; 5 services + Postgres + Redis + S3; eigen Google/MS OAuth-apps alsnog verplicht; gebouwd voor SaaS-multi-tenant. Meer werk dan direct bouwen, zonder winst. |
| **Stalwart** | Mooie moderne mailserver (nu ook CalDAV/CardDAV, AGPL, ~100MB), maar **geen aggregator**: kan niet bij Gmail/Outlook pollen; vereist losse imapsync/mbsync-crons + agenda-synctool + SPF/DKIM-gedoe bij verzenden. Overkill als tussenlaag. |
| **Kurrier** | Actiefste self-hosted workspace (~1k stars) maar **vervangt** Gmail/Outlook (eigen IMAP/CalDAV-wereld) i.p.v. ze te syncen; mail-koppeling alleen via IMAP-credentials, agenda is een eigen CalDAV-server zonder Google/Outlook-sync. |
| **EmailEngine** | Doet e-mail-unificatie goed, maar proprietary ~€995/jaar en geen agenda. |
| **Nylas / Unipile / Aurinko / Composio** | Cloud-only (of enterprise-only self-host); tokens/data bij een derde partij. Tegen het self-hosted-uitgangspunt. |
| **Panora / Supaglue / Revert** | Open-source unified-API-categorie: gearchiveerd/gestopt, en dekten sowieso CRM/ticketing, geen mail/agenda. |
| **vdirsyncer + Baikal** | Valide alternatief voor een centrale agenda-laag, maar meer bewegende delen dan nodig nu de CalDAV-client Google al direct aankan; achterhand-optie als het aantal bronnen groeit. |

## Randvoorwaarden-feiten (2026)

- Gmail: app-wachtwoorden (met 2FA) werken nog voor IMAP/SMTP/CalDAV;
  Ithaka's XOAUTH2-flow bestaat ook al (`GOOGLE_OAUTH_CLIENT_ID/SECRET`).
- Google CalDAV-endpoint: `apidata.googleusercontent.com/caldav/v2/…`
  (mapping zit in `src/caldav_sync.py:185-216`).
- Outlook.com: basic auth volledig dood → OAuth verplicht → DavMail is de
  brug zonder eigen app-registratie.
- Outlook published-ICS-URL bestaat als read-only fallback (sync-latency
  kan uren zijn).

## Besluit gevraagd

Akkoord met de DavMail-route (stap 1-4)? Dan is de eerste concrete stap de
compose-wijziging + eenmalige device-code-login voor het Outlook-account.

Ed de Feber, in nauwe samenwerking met Claude
