"""Every native tool must be explicitly classified for public/non-admin users.

NON_ADMIN_BLOCKED_TOOLS is a denylist: a new native tool (one with a
FUNCTION_TOOL_SCHEMAS entry) defaults to ALLOWED for public/non-admin users
unless someone remembers to add it there. PUBLIC_ALLOWED_TOOLS
(src/tool_security.py) makes the current, unchanged "allowed by omission"
set explicit, so this test can assert every schema tool is one or the other
-- CI then fails loudly if a new tool ships unclassified, instead of it
quietly reaching public users unreviewed.

Same style as tests/test_email_registry_sync.py: import src.agent_tools
first to resolve the agent_tools/tool_parsing/tool_schemas circular-import
cluster, then compare the registries directly.
"""
import src.agent_tools  # noqa: F401 — resolve the circular-import cluster first
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PUBLIC_ALLOWED_TOOLS


def _schema_tool_names() -> set:
    names = {(t.get("function") or {}).get("name") for t in FUNCTION_TOOL_SCHEMAS}
    names.discard(None)
    return names


def test_no_schema_tool_is_mcp_namespaced():
    # mcp__* tools are namespaced dynamically at MCP-attach time and are
    # blocked wholesale by is_public_blocked_tool()'s `startswith("mcp__")`
    # check; they never appear in FUNCTION_TOOL_SCHEMAS. The partition below
    # assumes that, so pin it explicitly rather than let it drift silently.
    assert not any(name.startswith("mcp__") for name in _schema_tool_names())


def test_public_allowed_and_blocked_are_disjoint():
    # A tool cannot be both explicitly public-allowed and non-admin-blocked.
    overlap = PUBLIC_ALLOWED_TOOLS & NON_ADMIN_BLOCKED_TOOLS
    assert overlap == set(), f"classified as both allowed and blocked: {sorted(overlap)}"


def test_every_schema_tool_is_classified():
    """The security-relevant direction: every FUNCTION_TOOL_SCHEMAS tool must
    fall into PUBLIC_ALLOWED_TOOLS or NON_ADMIN_BLOCKED_TOOLS. NON_ADMIN_BLOCKED_TOOLS
    also covers several XML-only tools with no native schema (email drafts,
    vault, attachment download) -- those don't round-trip through
    FUNCTION_TOOL_SCHEMAS, so this checks containment, not full set equality."""
    schema_names = _schema_tool_names()
    classified = PUBLIC_ALLOWED_TOOLS | NON_ADMIN_BLOCKED_TOOLS
    unclassified = schema_names - classified
    assert unclassified == set(), (
        f"unclassified native tool(s) — add to PUBLIC_ALLOWED_TOOLS or "
        f"NON_ADMIN_BLOCKED_TOOLS in src/tool_security.py: {sorted(unclassified)}"
    )


def test_every_xml_only_mutating_tool_is_blocked_for_non_admins():
    """XML/fence-only tools never appear in FUNCTION_TOOL_SCHEMAS, so the
    schema partition above cannot see them — and NON_ADMIN_BLOCKED_TOOLS is a
    denylist, so an unclassified XML-only mutating tool is allowed-by-omission
    for public/non-admin users. That is exactly how manage_research escaped
    (is_public_blocked_tool("manage_research") was False while it could read
    and delete any user's saved research). Use the hand-maintained plan-mode
    mutator backstop as the inventory of known mutating tool names: every one
    without a native schema must be explicitly in NON_ADMIN_BLOCKED_TOOLS."""
    from src.tool_security import _PLAN_MODE_KNOWN_MUTATORS

    xml_only_mutators = set(_PLAN_MODE_KNOWN_MUTATORS) - _schema_tool_names()
    unblocked = xml_only_mutators - NON_ADMIN_BLOCKED_TOOLS
    assert unblocked == set(), (
        f"XML-only mutating tool(s) reachable by non-admin users — add to "
        f"NON_ADMIN_BLOCKED_TOOLS in src/tool_security.py: {sorted(unblocked)}"
    )


def test_public_allowed_tools_are_real_schema_names():
    # Catches the opposite drift: a name lingering in PUBLIC_ALLOWED_TOOLS
    # after its schema was removed/renamed (dead weight, not a vulnerability,
    # but worth keeping honest).
    schema_names = _schema_tool_names()
    stale = PUBLIC_ALLOWED_TOOLS - schema_names
    assert stale == set(), (
        f"PUBLIC_ALLOWED_TOOLS entries with no matching schema (rename/removal?): "
        f"{sorted(stale)}"
    )
