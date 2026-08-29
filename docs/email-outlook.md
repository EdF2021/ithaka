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
`docker.io/kran0/davmail-docker:6.8.1`, reachable only from other containers
as hostname `davmail`:

| Protocol | Port | Notes |
| --- | --- | --- |
| IMAP | 1143 | plain, no TLS |
| SMTP | 1025 | plain, no TLS |
| CalDAV | 1080 | plain HTTP |

It's not published on a host port — only containers on the compose network
(i.e. `ithaka`) can reach it. Key env vars, all overridable via `.env`:

- `DAVMAIL_MODE` (default `O365EWS`) — the Exchange backend DavMail talks to.
- `DAVMAIL_AUTHENTICATION` (default `O365Modern`) — the OAuth method. Set to
  `O365DeviceCode` only for the one-time initial login (see below).
- `DAVMAIL_TENANT` (default `common`) — set to `consumers` if you hit a
  unified-consent error on a personal Microsoft account.

The OAuth refresh token is written to the `davmail-config` volume
(`davmail.oauth.persistToken=true`) and survives container restarts. Removing
that volume means doing the device-code login again.

The `ithaka` service also gets `ITHAKA_ALLOW_PRIVATE_CALDAV=${ITHAKA_ALLOW_PRIVATE_CALDAV:-1}`,
which allows CalDAV URLs pointing at the internal `davmail` hostname (Ithaka's
CalDAV client otherwise rejects private-network URLs as SSRF risk — see
`src/caldav_sync.py`). Leave this at `1` when you run the bundled `davmail`
service; set it to `0` if you don't run it and want the SSRF guard back to
strict.

## One-time login

DavMail needs an interactive OAuth login the first time it connects to a
mailbox. Do this once per Microsoft account:

1. In `.env`, set:

   ```
   DAVMAIL_AUTHENTICATION=O365DeviceCode
   ```

2. Start (or restart) just the gateway:

   ```bash
   docker compose up -d davmail
   ```

3. Trigger the flow by making any IMAP login attempt against it — easiest is
   to add the Ithaka email account in Settings (see below) with your real
   Microsoft address and your chosen local password, and let it try to
   connect. You can also trigger it directly, e.g. `openssl s_client` or any
   IMAP client pointed at `davmail:1143` from inside the compose network.

4. Watch the logs for the device code and URL:

   ```bash
   docker compose logs -f davmail
   ```

   You'll see something like "To sign in, use a web browser to open the page
   https://login.microsoftonline.com/... and enter the code XXXXXXXX".

5. Open that URL on any device, enter the code, and sign in with the
   Microsoft account you're connecting.

6. Once DavMail logs a successful token acquisition, switch back to normal
   auth in `.env` — either set it explicitly or just remove the line (it
   defaults to `O365Modern`):

   ```
   DAVMAIL_AUTHENTICATION=O365Modern
   ```

7. Apply the change:

   ```bash
   docker compose up -d davmail
   ```

From now on DavMail uses the persisted refresh token silently — no more
interactive prompts, until the refresh token itself expires or is revoked
(e.g. by Conditional Access, a password reset, or deleting the
`davmail-config` volume).

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

- **`O365Interactive not supported in headless mode`** — `DAVMAIL_AUTHENTICATION`
  is still on an interactive method (or unset while a stale interactive value
  lingers) with no way to open a browser inside the container. Set it to
  `O365DeviceCode` for the initial login, then back to `O365Modern`.
- **Prompted to log in again after everything worked before** — the
  `davmail-config` volume was removed or recreated, so the refresh token is
  gone. Redo the one-time device-code login above.
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
