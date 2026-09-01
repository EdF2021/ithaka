# Outlook.com / Microsoft 365 via DavMail

Ithaka's email and calendar accounts speak plain IMAP, SMTP, and CalDAV with
username/password authentication. Microsoft no longer accepts that for
Outlook.com or Microsoft 365 mailboxes:

- Basic authentication for IMAP was turned off tenant-wide in 2022.
- Basic authentication for SMTP AUTH follows in March 2026.

A normal password in the IMAP/SMTP form now fails with errors such as:

- `IMAP: AUTHENTICATE failed`
- `SMTP: 535 5.7.139 Authentication unsuccessful, basic authentication is disabled`

Ithaka does not talk to Microsoft Graph or OAuth directly, and registering an
Entra app just to add one mailbox is overkill. Instead the stack bundles
[DavMail](http://davmail.sourceforge.net/), a local gateway that does the
Microsoft OAuth dance for you and re-exposes the mailbox as plain
IMAP/SMTP/CalDAV on the compose network — no Entra app registration needed.

## What's running

The `davmail` service in `docker-compose.yml` (mirrored in
`docker-compose.gpu-nvidia.yml` / `docker-compose.gpu-amd.yml`) runs
a digest-pinned `trunk` build of `docker.io/kran0/davmail-docker`, reachable
only from other containers as hostname `davmail`. (`trunk` instead of the
6.8.1 release: personal
accounts run over DavMail's Graph backend, and 6.8.1 still crashes with a
NullPointerException in `DateUtil.getExchangeTimeZone` on calendar events
without a timezone; the null guard is only on master. Pin back to a release
tag once something newer than 6.8.1 ships.)

| Protocol | Port | Notes |
| --- | --- | --- |
| IMAP | 1143 | plain, no TLS |
| SMTP | 1025 | plain, no TLS |
| CalDAV | 1080 | plain HTTP |

It's not published on a host port — only containers on the compose network
(i.e. `ithaka`) can reach it. Key env vars, all overridable via `.env`:

- `DAVMAIL_MODE` (default `O365EWS`) — the Exchange backend DavMail talks to.
- `DAVMAIL_AUTHENTICATION` (default `O365Modern`) — the OAuth method.
  `O365Modern` uses the refresh token stored in the config volume.
- `DAVMAIL_TENANT` (default `common`) — **set to `consumers` for a personal
  outlook.com / hotmail account.** The `common` tenant rejects the EWS scope
  for personal accounts (`invalid_scope`), and its device-code flow is broken
  outright (see below).
- `DAVMAIL_GRAPH` (default `true`) — sets `davmail.enableGraph=true` so
  DavMail serves the mailbox through the Microsoft Graph API. Required for
  personal accounts (their tokens carry Graph scopes; the EWS session refuses
  them with "Found Graph stored token, incompatible with EWS").
- `DAVMAIL_OIDC` (default `true`) — sets `davmail.enableOidc=true`, i.e. the
  Microsoft identity platform v2.0 endpoints. Required for personal accounts
  (v1 fails with AADSTS500201).

The OAuth refresh token is written to the `davmail-config` volume
(`davmail.oauth.persistToken=true`) and survives container restarts. Removing
that volume means doing the one-time login again.

The `ithaka` service also gets `ITHAKA_ALLOW_PRIVATE_CALDAV=${ITHAKA_ALLOW_PRIVATE_CALDAV:-1}`,
which allows CalDAV URLs pointing at the internal `davmail` hostname (Ithaka's
CalDAV client otherwise rejects private-network URLs as SSRF risk — see
`src/caldav_sync.py`). Leave this at `1` when you run the bundled `davmail`
service; set it to `0` if you don't run it and want the SSRF guard back to
strict.

## One-time login (personal accounts: authorization-code bootstrap)

DavMail needs a refresh token the first time it connects to a mailbox.

**Don't bother with `DAVMAIL_AUTHENTICATION=O365DeviceCode` for a personal
account.** Microsoft's login.live.com layer rejects every completion of the
device-code flow with a misleading "The code you entered has expired" — on
any device, via Authenticator and via password, even while DavMail's polling
shows the code is still valid server-side. (Verified 2026-08-29; likely an
anti-phishing block on the MSA remoteconnect flow.)

What does work is the plain OAuth authorization-code flow, done once by hand:

1. Open this URL in a browser where you're (or can get) signed in to the
   Microsoft account (all on one line; this is DavMail's own client id):

   ```
   https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_id=facd6cff-a294-4415-b59f-c5b01937d7bd&response_type=code&redirect_uri=https%3A%2F%2Flogin.microsoftonline.com%2Fcommon%2Foauth2%2Fnativeclient&response_mode=query&scope=openid%20profile%20offline_access%20Mail.ReadWrite%20Calendars.ReadWrite%20MailboxSettings.Read%20Mail.ReadWrite.Shared%20Contacts.ReadWrite%20Tasks.ReadWrite%20Mail.Send%20People.Read&login_hint=<your-email>
   ```

2. Sign in normally. You land on a blank `.../oauth2/nativeclient?code=...`
   page — copy the `code=` value from the address bar **quickly** (the page
   may bounce to a `/common/wrongplace` URL after a moment; if it does, just
   reload the authorize URL — the signed-in session hands out a fresh code
   without prompting).

3. Exchange the code for tokens (from any shell; no client secret needed):

   ```bash
   curl -s -X POST https://login.microsoftonline.com/consumers/oauth2/v2.0/token \
     -d client_id=facd6cff-a294-4415-b59f-c5b01937d7bd \
     -d grant_type=authorization_code \
     --data-urlencode "redirect_uri=https://login.microsoftonline.com/common/oauth2/nativeclient" \
     --data-urlencode "code=<the code>"
   ```

4. Put the `refresh_token` from the JSON response into DavMail's config and
   restart it:

   ```bash
   docker exec ithaka-davmail-1 sh -c \
     'printf "davmail.oauth.<your-email>.refreshToken=%s\n" "<refresh token>" >> /davmail-config/davmail.properties'
   docker compose restart davmail
   ```

5. Make sure `.env` has `DAVMAIL_TENANT=consumers` (and no leftover
   `DAVMAIL_AUTHENTICATION=O365DeviceCode` line), then test: an IMAP login
   against `davmail:1143` with your email + local password should log
   "Loaded stored token" followed by a successful refresh.

From then on DavMail refreshes and re-persists the (rotated, encrypted)
token silently — no more interactive prompts, until the refresh token is
revoked (password reset, security event, or deleting the `davmail-config`
volume).

## The DavMail local password

The password you type into Ithaka's account form is **not** your Microsoft
password — it never leaves DavMail's own local encryption. DavMail uses it
to encrypt the OAuth refresh token it stores on disk; it's a password you
invent yourself, used purely to protect the local token cache.

**Use the exact same local password in every Ithaka field that talks to this
mailbox** — IMAP, SMTP, and CalDAV. DavMail matches the password across
protocols to the same cached token; a mismatch between fields for the same
account looks like an authentication failure even though the underlying
Microsoft session is fine.

## Ithaka account configuration

**Email account** (Settings → Email):

| Field | Value |
| --- | --- |
| IMAP host | `davmail` |
| IMAP port | `1143` |
| IMAP STARTTLS | off |
| SMTP host | `davmail` |
| SMTP port | `1025` |
| SMTP security | none |
| Username | your full Microsoft email address |
| Password | your chosen local DavMail password (same value everywhere) |

**Calendar account** (CalDAV):

```
http://davmail:1080/users/<your-email-address>/calendar
```

with the same username and local password.

## Troubleshooting

- **Device-code login keeps saying "The code you entered has expired"** —
  not your fault and not fixable by being faster: Microsoft blocks the MSA
  device-code completion. Use the authorization-code bootstrap above.
- **`refresh token failed Found Graph stored token, incompatible with EWS`** —
  the token has Graph scopes but DavMail built an EWS session. Make sure the
  compose file sets `davmail.enableGraph=true` (env `DAVMAIL_GRAPH`, default
  on) and restart davmail.
- **CalDAV requests die with a 503 / `NullPointerException` in
  `DateUtil.getExchangeTimeZone`** — you're on the 6.8.1 image; its Graph
  calendar path crashes on events without a timezone. Use the `trunk` image
  (see above).
- **`O365Interactive not supported in headless mode`** — `DAVMAIL_AUTHENTICATION`
  is on an interactive method with no way to open a browser inside the
  container. Set it (back) to `O365Modern`.
- **Prompted to log in again after everything worked before** — the
  `davmail-config` volume was removed or recreated, so the refresh token is
  gone. Also possible: a *failed* login attempt makes DavMail blank the
  stored `refreshToken=` value (the key stays, the value empties). Redo the
  one-time bootstrap above.
- **Login works interactively but Ithaka still can't connect** — check that
  every Ithaka field (IMAP, SMTP, CalDAV) uses the *same* local password;
  DavMail can't match a mismatched one to the cached token.
- **Work/school account blocked** — organizational (Microsoft 365 tenant)
  accounts can be blocked by Conditional Access policies (device compliance,
  MFA enforcement DavMail can't satisfy, blocked legacy client detection).
  This is a tenant admin setting, not something DavMail or Ithaka can work
  around; ask the tenant admin for an exemption or use a personal account
  instead.
- **Unified consent / "need admin approval" error on a personal account** —
  try setting `DAVMAIL_TENANT=consumers` in `.env` instead of the default
  `common`, then `docker compose up -d davmail`.
- **CalDAV URL rejected as unsafe** — confirm `ITHAKA_ALLOW_PRIVATE_CALDAV=1`
  is set for the `ithaka` service (it is by default when `davmail` is
  defined in compose).
