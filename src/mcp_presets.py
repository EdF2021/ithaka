"""
mcp_presets.py

Server-side catalog of MCP connector presets shown in the "Add MCP Server"
UI (static/js/settings.js). Each preset fills in transport/command/args/env/
url for a known MCP server package so the admin doesn't have to look up the
npm package name and required env vars by hand.

Env values are placeholders only (e.g. "<TOKEN>" or "") — never real secrets.
The admin still fills in real values before saving; nothing here is a
credential.
"""

from typing import Any, Dict, List, Optional


def _preset(
    id: str,
    name: str,
    transport: str,
    *,
    command: Optional[str] = None,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    url: Optional[str] = None,
    oauth: Optional[Dict[str, Any]] = None,
    help: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "id": id,
        "name": name,
        "transport": transport,
        "command": command,
        "args": args or [],
        "env": env or {},
        "url": url,
        "oauth": oauth,
        "help": help,
        "tags": tags or [],
    }


_PRESETS: List[Dict[str, Any]] = [
    _preset(
        "gmail", "Gmail", "stdio",
        command="npx",
        args=["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
        env={"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""},
        oauth={
            "provider": "google",
            "keys_file": "gmail/gcp-oauth.keys.json",
            "token_file": "gmail/credentials.json",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.settings.basic",
            ],
        },
        help="""Setup:
1. Go to console.cloud.google.com > create or select a project
2. APIs & Services > Library > search "Gmail API" > Enable
3. APIs & Services > OAuth consent screen > set up (External is fine)
4. Under Audience, add your Gmail address as a test user
5. APIs & Services > Credentials > + Create Credentials > OAuth Client ID
6. Application type: Desktop App > Create
7. Copy the Client ID and Client Secret into the fields above
8. Click Add Server, then click the Authorize button
9. Sign in with Google, copy the URL from the error page, paste it back""",
        tags=["email", "google", "oauth"],
    ),
    _preset(
        "email-imap-smtp", "Email (IMAP/SMTP)", "stdio",
        command="npx",
        args=["-y", "@codefuturist/email-mcp", "stdio"],
        env={
            "MCP_EMAIL_ADDRESS": "",
            "MCP_EMAIL_PASSWORD": "",
            "MCP_EMAIL_IMAP_HOST": "",
            "MCP_EMAIL_SMTP_HOST": "",
        },
        help="Works with any IMAP/SMTP email provider.\n"
             "1. Enter your email address and password (or app password)\n"
             "2. Enter your provider's IMAP and SMTP hostnames\n"
             "3. Click Add Server",
        tags=["email"],
    ),
    _preset(
        "caldav", "CalDAV (Radicale/Nextcloud)", "stdio",
        command="npx",
        args=["-y", "caldav-mcp"],
        env={
            "CALDAV_BASE_URL": "<http://localhost:5232>",
            "CALDAV_USERNAME": "",
            "CALDAV_PASSWORD": "",
        },
        help="Works with any CalDAV server (Radicale, Nextcloud, etc.).\n"
             "1. Enter your CalDAV server URL (e.g. http://localhost:5232)\n"
             "2. Enter your username and password\n"
             "3. Click Add Server",
        tags=["calendar"],
    ),
    _preset(
        "google-calendar", "Google Calendar", "stdio",
        command="npx",
        args=["-y", "@cocal/google-calendar-mcp"],
        env={"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""},
        oauth={
            "provider": "google",
            "keys_file": "google-calendar/gcp-oauth.keys.json",
            "token_file": "google-calendar/tokens.json",
            # @cocal/google-calendar-mcp keys its token file by account mode
            # ("normal" by default) — see its tokenManager.
            "token_format": "multi_account",
            "env_map": {
                "keys_file": "GOOGLE_OAUTH_CREDENTIALS",
                "token_file": "GOOGLE_CALENDAR_MCP_TOKEN_PATH",
            },
            "scopes": [
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/calendar.events",
            ],
        },
        help="""Setup:
1. Go to console.cloud.google.com > create or select a project
2. APIs & Services > Library > search "Google Calendar API" > Enable
3. APIs & Services > OAuth consent screen > set up (External is fine)
4. Under Audience, add your Google address as a test user
5. APIs & Services > Credentials > + Create Credentials > OAuth Client ID
6. Application type: Desktop App > Create
7. Copy the Client ID and Client Secret into the fields above
8. Click Add Server, then click the Authorize button
9. Sign in with Google, copy the URL from the error page, paste it back""",
        tags=["calendar", "google", "oauth"],
    ),
    _preset(
        "google-drive", "Google Drive", "stdio",
        command="npx",
        # Maintained replacement for the archived
        # @modelcontextprotocol/server-gdrive, whose plain start constructs an
        # OAuth2 client without client credentials and therefore can never
        # refresh an expired access token. This package's external-token mode
        # takes tokens straight from env — no browser needed in the container.
        args=["-y", "@piotr-agier/google-drive-mcp"],
        env={"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""},
        oauth={
            "provider": "google",
            "keys_file": "google-drive/gcp-oauth.keys.json",
            "token_env": {
                "access_token": "GOOGLE_DRIVE_MCP_ACCESS_TOKEN",
                "refresh_token": "GOOGLE_DRIVE_MCP_REFRESH_TOKEN",
                "client_id": "GOOGLE_DRIVE_MCP_CLIENT_ID",
                "client_secret": "GOOGLE_DRIVE_MCP_CLIENT_SECRET",
            },
            "scopes": [
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/presentations",
            ],
        },
        help="""Setup:
1. Go to console.cloud.google.com > create or select a project
2. APIs & Services > Library > enable the Google Drive API (plus Docs/Sheets/
   Slides APIs if you want those tools)
3. APIs & Services > OAuth consent screen > set up (External is fine)
4. Under Audience, add your Google address as a test user
5. APIs & Services > Credentials > + Create Credentials > OAuth Client ID
6. Application type: Desktop App > Create
7. Copy the Client ID and Client Secret into the fields above
8. Click Add Server, then click the Authorize button
9. Sign in with Google, copy the URL from the error page, paste it back""",
        tags=["storage", "google", "oauth"],
    ),
    _preset(
        "github", "GitHub", "stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
        help="1. Go to github.com > Settings > Developer Settings > Personal Access Tokens > Fine-grained tokens\n"
             "2. Generate a new token with the repo permissions you need\n"
             "3. Paste it as Github Personal Access Token",
        tags=["dev", "github"],
    ),
    _preset(
        "slack", "Slack", "stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-slack"],
        env={"SLACK_BOT_TOKEN": "", "SLACK_TEAM_ID": ""},
        help="1. Go to api.slack.com/apps > Create New App > From Scratch\n"
             "2. Add Bot Token Scopes (channels:read, chat:write, etc.)\n"
             "3. Install to workspace, copy the Bot User OAuth Token (xoxb-...)\n"
             "4. Team ID is in your workspace URL or Slack admin settings",
        tags=["chat"],
    ),
    _preset(
        "notion", "Notion", "stdio",
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        env={"OPENAPI_MCP_HEADERS": ""},
        help="1. Go to notion.so/my-integrations\n"
             "2. Create a new integration\n"
             "3. Copy the Internal Integration Secret\n"
             "4. Share the Notion pages/databases you want accessible with the integration\n"
             "5. For Openapi Mcp Headers enter:\n"
             '   {"Authorization": "Bearer YOUR_SECRET", "Notion-Version": "2022-06-28"}',
        tags=["docs"],
    ),
    _preset(
        "linear", "Linear", "stdio",
        command="npx",
        args=["-y", "mcp-linear"],
        env={"LINEAR_API_KEY": ""},
        help="1. Go to linear.app > Settings > API\n"
             "2. Create a Personal API Key\n"
             "3. Paste it as Linear Api Key",
        tags=["dev", "tasks"],
    ),
    _preset(
        "brave-search", "Brave Search", "stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-brave-search"],
        env={"BRAVE_API_KEY": ""},
        help="1. Go to brave.com/search/api\n"
             "2. Sign up for a free plan (2000 queries/month)\n"
             "3. Copy your API key",
        tags=["search"],
    ),
    _preset(
        "browser-playwright", "Browser (Playwright)", "stdio",
        command="npx",
        args=["-y", "@playwright/mcp@latest", "--headless"],
        env={},
        help="Browser automation via Playwright. The AI can navigate pages, click, fill forms, and read content.\n"
             "Runs headless by default. Remove --headless from Args to see the browser window.\n"
             "First run installs Chromium automatically.",
        tags=["automation"],
    ),
    _preset(
        "filesystem", "Filesystem", "stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/home"],
        env={},
        help="Edit the Args field to change which directory the server has access to.",
        tags=["files"],
    ),
    _preset(
        "memory", "Memory", "stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
        env={},
        tags=["memory"],
    ),
    _preset(
        "postgres", "Postgres", "stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres", "postgresql://user:pass@localhost/db"],
        env={},
        help="Replace the connection string in the Args field with your actual Postgres connection URL.",
        tags=["database"],
    ),
    _preset(
        "todoist", "Todoist", "stdio",
        command="npx",
        args=["-y", "todoist-mcp-server"],
        env={"TODOIST_API_TOKEN": ""},
        help="1. Go to todoist.com > Settings > Integrations > Developer\n"
             "2. Copy your API token",
        tags=["tasks"],
    ),
    _preset(
        "github-remote", "GitHub (remote, hosted)", "http",
        url="https://api.githubcopilot.com/mcp/",
        help="GitHub's hosted remote MCP server — no local install needed.\n"
             "1. Create a GitHub Personal Access Token (Settings > Developer Settings > "
             "Personal Access Tokens) with the scopes you need, or use OAuth if your "
             "GitHub plan supports it\n"
             "2. Ithaka will prompt you to authorize when you connect\n"
             "3. If using a PAT instead of OAuth, some MCP clients expect it as an "
             "Authorization header — check GitHub's remote MCP docs for the current flow",
        tags=["dev", "github", "hosted"],
    ),
    _preset(
        "custom-http", "Custom HTTP server", "http",
        url="<https://mcp.example.com/mcp>",
        help="Any MCP server reachable over Streamable HTTP or SSE.\n"
             "1. Enter the server's URL\n"
             "2. Click Add Server — Ithaka will attempt to connect and, if the "
             "server requires OAuth, walk you through authorizing",
        tags=["custom", "hosted"],
    ),
]


def get_presets() -> List[Dict[str, Any]]:
    """Return the MCP connector preset catalog.

    Returns a fresh list of preset dicts (not a reference to the module-level
    list) so callers can't mutate the shared catalog.
    """
    return [dict(p) for p in _PRESETS]
