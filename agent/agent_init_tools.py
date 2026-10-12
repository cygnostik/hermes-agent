"""Tool-snapshot construction shared by runtime and prompt inspection."""

import logging

logger = logging.getLogger(__name__)


def load_tools(agent, enabled_toolsets, disabled_toolsets):
    # A feature that left core for a catalog plugin (Home Assistant) is installed for a home that
    # used it, once per process, before discovery so its tools are in this agent's snapshot.
    from hermes_cli.left_core_migration import recover_at_startup
    if not getattr(agent, "inspection_mode", False):
        recover_at_startup(say=getattr(agent, "_emit_startup_warning", None))
    # A multiplexed gateway may have switched HERMES_HOME since model_tools was imported;
    # make sure this profile's plugins are discovered before the tool snapshot.
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
    except Exception:
        logger.warning("Plugin discovery failed during agent setup", exc_info=True)

    # Capture the registry generation FIRST so a concurrent refresh can detect staleness.
    try:
        from tools.registry import registry as _snapshot_registry
        agent._tool_snapshot_generation = _snapshot_registry._generation
    except Exception:
        logger.debug("Tool registry generation unavailable", exc_info=True)
        agent._tool_snapshot_generation = 0
    import model_tools
    from toolsets import agent_tool_drops, session_disabled_toolsets
    disabled_toolsets = session_disabled_toolsets(disabled_toolsets, getattr(agent, "platform", None))
    agent.disabled_toolsets = disabled_toolsets  # so the tool_search bridge and delegate children drop them too
    agent.tools = model_tools.get_tool_definitions(
        enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets, quiet_mode=agent.quiet_mode,
        **({"allow_network": False} if getattr(agent, "inspection_mode", False) else {}))
    # A finite -q run has no later session to learn for: no skill authoring tool (agent/oneshot_footprint.py).
    from agent.oneshot_footprint import prune_oneshot_tools
    agent.tools = prune_oneshot_tools(agent.tools or [])
    if drops := agent_tool_drops(agent):
        agent.tools = [t for t in agent.tools if t["function"]["name"] not in drops]

    agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools} if agent.tools else set()
    # Kanban guidance is session-static for the dispatcher-owned worker only. Profiles may
    # expose kanban_show interactively, and children/cron runs inherit the env var, without
    # owning a task.
    from agent.delegation_context import owned_kanban_task
    from agent.prompt_builder import KANBAN_GUIDANCE
    agent._kanban_worker_guidance = (
        KANBAN_GUIDANCE if owned_kanban_task() and "kanban_show" in agent.valid_tool_names else ""
    )
    if agent.quiet_mode:
        return
    if agent.tools:
        print(f"🛠️  Loaded {len(agent.tools)} tools: {', '.join(sorted(agent.valid_tool_names))}")
        if enabled_toolsets:
            print(f"   ✅ Enabled toolsets: {', '.join(enabled_toolsets)}")
        if disabled_toolsets:
            print(f"   ❌ Disabled toolsets: {', '.join(disabled_toolsets)}")
        import model_tools
        requirements = model_tools.check_toolset_requirements()
        missing_reqs = [name for name, available in requirements.items() if not available]
        if missing_reqs:
            agent._safe_print(f"⚠️  Some tools may not work due to missing requirements: {missing_reqs}", diagnostic=True)
    else:
        print("🛠️  No tools loaded (all tools filtered out or unavailable)")
    if agent.save_trajectories:
        print("📝 Trajectory saving enabled")
    if agent.ephemeral_system_prompt:
        prompt_preview = agent.ephemeral_system_prompt[:60] + "..." if len(agent.ephemeral_system_prompt) > 60 else agent.ephemeral_system_prompt
        print(f"🔒 Ephemeral system prompt: '{prompt_preview}' (not saved to trajectories)")
    if agent._use_prompt_caching:
        if agent._use_native_cache_layout and agent.provider == "anthropic":
            source = "native Anthropic"
        elif agent._use_native_cache_layout:
            source = "Anthropic-compatible endpoint"
        else:
            source = "Claude via OpenRouter"
        print(f"💾 Prompt caching: ENABLED ({source}, {agent._cache_ttl} TTL)")
