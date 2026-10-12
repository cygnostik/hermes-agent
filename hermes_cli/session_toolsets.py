"""Desktop/TUI tool selection, without starting the gateway."""

import contextlib
import logging
import os
import sys


logger = logging.getLogger(__name__)


def _gui_surface_toolsets(platform: str) -> set[str]:
    """Toolsets that exist because of the CLIENT (off ``_HERMES_CORE_TOOLS``; this is the one gate).
    ``platform`` is the SESSION's source, never a process env var: the desktop may drive a URL/cloud
    backend where ``HERMES_DESKTOP`` is unset (AGENTS.md surface rule)."""
    from toolsets import CLIENT_SURFACE_TOOLSETS
    return set(CLIENT_SURFACE_TOOLSETS) if platform == "desktop" else {"project"}


def _with_session_toolsets(selection, platform: str, disabled_toolsets=None) -> list[str]:
    """*selection* plus the client surface's toolsets the session carries whatever its config says.

    The fold-in happens after ``_get_platform_tools`` already subtracted ``agent.disabled_toolsets``,
    so the same subtraction is applied to the fold-in itself — otherwise ``disabled_toolsets:
    [project]`` is a no-op on desktop/TUI, the only surfaces where the client toolsets exist
    (#54433). ``desktop_ui`` is kept regardless: it is the client's own control surface, not a
    model toolset."""
    fold_in = _gui_surface_toolsets(platform) - set(selection)
    disabled = set(disabled_toolsets or [])
    if disabled:
        fold_in -= disabled - {"desktop_ui"}
    return [*selection, *sorted(fold_in)]


def _tui_notice(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _resolve_explicit_toolsets(explicit: list[str], validate_toolset) -> list[str] | None | bool:
    """Resolve a HERMES_TUI_TOOLSETS pin: list, None for "all", False when nothing was valid."""
    built_in = [name for name in explicit if validate_toolset(name)]
    unresolved = [name for name in explicit if name not in built_in]
    if unresolved:
        try:
            from hermes_cli.plugins import discover_plugins
            discover_plugins()
            plugin_valid = [name for name in unresolved if validate_toolset(name)]
        except Exception:
            plugin_valid = []
        built_in.extend(plugin_valid)
        unresolved = [name for name in unresolved if name not in plugin_valid]
    if any(name in {"all", "*"} for name in built_in):
        if ignored := [name for name in explicit if name not in {"all", "*"}]:
            _tui_notice(f"[tui] HERMES_TUI_TOOLSETS=all enables every toolset; ignoring additional entries: {', '.join(ignored)}")
        return None
    if not unresolved:
        return built_in
    try:  # (enabled, disabled) MCP server names from raw config; both empty on any failure
        from hermes_cli.config import read_raw_config
        from tools.mcp_tool_common import mcp_server_enabled
        raw_cfg = read_raw_config()
        mcp_servers = raw_cfg.get("mcp_servers") if isinstance(raw_cfg.get("mcp_servers"), dict) else {}
        mcp_names, mcp_disabled = set(), set()
        for name, server_cfg in mcp_servers.items():
            if isinstance(server_cfg, dict):
                on = mcp_server_enabled(server_cfg)
                (mcp_names if on else mcp_disabled).add(str(name))
    except Exception:
        mcp_names, mcp_disabled = set(), set()
    mcp_valid = [name for name in unresolved if name in mcp_names]
    disabled = [name for name in unresolved if name in mcp_disabled]
    unknown = [name for name in unresolved if name not in mcp_names and name not in mcp_disabled]
    if unknown:
        _tui_notice(f"[tui] ignoring unknown HERMES_TUI_TOOLSETS entries: {', '.join(unknown)}")
    if disabled:
        _tui_notice("[tui] ignoring disabled MCP servers in HERMES_TUI_TOOLSETS "
                    f"(set enabled: true in config.yaml to use): {', '.join(disabled)}")
    return (built_in + mcp_valid) or False


def resolve_interactive_toolsets(platform: str, disabled_toolsets=None) -> list[str] | None:
    """The agent's toolsets for this session (None = all): an explicit HERMES_TUI_TOOLSETS pin; else the
    coding posture (coding_context collapses to coding toolset + enabled MCP servers in a code workspace);
    else the configured CLI toolsets. Client-surface toolsets fold in here — only this surface can answer them.
    An explicitly saved EMPTY list (``platform_toolsets.cli: []`` that resolves to nothing) is a
    zero-tool state and returns [] — never None, which would mean unrestricted (#82010)."""
    session_platform = platform
    explicit = [item.strip() for item in os.environ.get("HERMES_TUI_TOOLSETS", "").split(",") if item.strip()]
    fallback_notice = None
    if not explicit:
        with contextlib.suppress(Exception):
            from agent.coding_context import coding_selection
            selection = coding_selection(platform=session_platform)
            if selection is not None:
                from hermes_cli.config import load_config
                from hermes_cli.tools_config import _get_platform_tools
                from toolsets import TOOLSET_SESSION_PLATFORMS
                selection += sorted(_get_platform_tools(load_config(), "cli") & TOOLSET_SESSION_PLATFORMS.keys())
                return sorted(_with_session_toolsets(selection, session_platform, disabled_toolsets))
    try:
        from toolsets import validate_toolset
    except Exception:
        logger.debug("Toolset validation unavailable", exc_info=True)
        validate_toolset = None
    if explicit and validate_toolset is not None:
        resolved = _resolve_explicit_toolsets(explicit, validate_toolset)
        if resolved is not False:
            # An operator pin replaces the surface fold-in.
            return resolved
        fallback_notice = "[tui] no valid HERMES_TUI_TOOLSETS entries; using configured CLI toolsets"
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools, _platform_toolsets_explicitly_saved
        cfg = load_config()
        # include_default_mcp_servers=True is the runtime variant (the agent must be able to call
        # default MCP servers); the config-editing variant would silently drop MCP tools from the TUI.
        # Passing ``False`` here is the config-editing variant — used when we need to persist a toolset list
        # without baking in implicit MCP defaults. Using the wrong variant at agent creation time makes MCP
        # tools silently missing from the TUI. See PR #3252 for the original design split.
        enabled = _get_platform_tools(cfg, "cli", include_default_mcp_servers=True)
        if fallback_notice is not None:
            _tui_notice(fallback_notice)
        if enabled:
            return sorted(_with_session_toolsets(enabled, session_platform, disabled_toolsets))
        # An explicitly saved list that resolves to nothing (``platform_toolsets.cli: []`` with no
        # enabled MCP servers) is an explicit zero-tool state, not "no filter": fail closed with []
        # instead of None (#82010). The client-surface fold-in must not resurrect tools the user
        # explicitly switched off, so [] is returned bare. Only an ABSENT/unset key keeps the
        # None = "no restriction" default below.
        if _platform_toolsets_explicitly_saved(cfg, "cli"):
            return []
        return None
    except Exception:
        logger.debug("Configured CLI toolsets unavailable", exc_info=True)
        if fallback_notice is not None:
            _tui_notice("[tui] no valid HERMES_TUI_TOOLSETS entries and configured CLI toolsets could not be loaded; enabling all toolsets")
        return None
