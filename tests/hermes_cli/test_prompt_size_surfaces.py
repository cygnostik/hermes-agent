"""Offline inspection must use the Desktop/TUI session's tool selection."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("platform", ["desktop", "tui", "cli", "telegram"])
@pytest.mark.parametrize("mode", ["configured", "focus", "empty", "pin"])
def test_inspection_uses_interactive_selection(tmp_path, platform, mode):
    """Exercise real config, selection and agent construction without a gateway boot."""
    home = tmp_path / "home"
    home.mkdir()
    config = {
        "model": {"default": "offline-inspection"},
        "agent": {"coding_context": "off", "disabled_toolsets": ["project", "terminal"]},
        "platform_toolsets": {"cli": ["skills", "file", "terminal"],
                              "telegram": ["skills", "file", "terminal"]},
    }
    if mode == "focus":
        config["agent"]["coding_context"] = "focus"
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    elif mode == "empty":
        config["platform_toolsets"]["telegram" if platform == "telegram" else "cli"] = []
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    memories = home / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("Offline memory fixture", encoding="utf-8")
    (memories / "USER.md").write_text("Offline user fixture", encoding="utf-8")
    skill = home / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: fixture-skill\ndescription: Offline fixture\n---\nBody\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "PATHEXT"}}
    env.update(HOME=str(home), HERMES_HOME=str(home), TMPDIR=str(tmp_path),
               TMP=str(tmp_path), TEMP=str(tmp_path), PYTHONUTF8="1")
    if mode == "pin":
        env["HERMES_TUI_TOOLSETS"] = "skills"
    source = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    probe = '''
import json, sys
sys.path.insert(0, sys.argv[1])
network = []
def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        import traceback
        network.append((event, str(args), traceback.format_stack()))
        raise RuntimeError("network forbidden during offline inspection")
sys.addaudithook(audit)
from hermes_cli.prompt_size import _build_inspection_agent, compute_prompt_breakdown
from agent.system_prompt import build_system_prompt
agent = _build_inspection_agent(sys.argv[2])
try:
    names = {tool["function"]["name"] for tool in agent.tools}
    prompt = build_system_prompt(agent)
    assert "tui_gateway.server" not in sys.modules, "inspection booted the gateway"
    mode, platform = sys.argv[3], sys.argv[2]
    assert "terminal" not in names
    assert "desktop_project" not in names
    if mode == "empty":
        assert "skills_list" not in names and "read_file" not in names
        assert "desktop_preview" not in names
    else:
        assert "skills_list" in names, sorted(names)
        assert "fixture-skill" in prompt
        if mode == "pin" and platform in {"desktop", "tui"}:
            assert "read_file" not in names and "desktop_preview" not in names
        else:
            assert "read_file" in names
            assert ("desktop_ui" in agent.enabled_toolsets) == (platform == "desktop")
            # Deferred Desktop tools are listed behind tool_search, not top-level schemas.
            assert ("desktop_preview" in json.dumps(agent.tools)) == (platform == "desktop")
    data = compute_prompt_breakdown(platform)
    assert data["system_prompt"]["bytes"] > 0
    assert data["memory"]["bytes"] > 0 and data["user_profile"]["bytes"] > 0
    assert "Offline memory fixture" in prompt and "Offline user fixture" in prompt
    assert data["tools"]["count"] == len(agent.tools)
    assert not network, network
    print("OFFLINE_SURFACE_OK", platform, mode, sorted(names))
finally:
    agent.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", probe, source, platform, mode], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OFFLINE_SURFACE_OK" in result.stdout


def test_inspection_preserves_context_window_for_local_files(tmp_path):
    """Large local identity/project inputs keep the configured window's dynamic cap."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({
        "model": {"default": "offline-inspection", "context_length": 200_000},
        "agent": {"coding_context": "off"},
        "platform_toolsets": {"cli": []},
    }), encoding="utf-8")
    project = "Project context fixture.\n" * 1_300
    soul = "Local identity fixture.\n" * 1_400
    (tmp_path / "AGENTS.md").write_text(project, encoding="utf-8")
    (home / "SOUL.md").write_text(soul, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "PATHEXT"}}
    env.update(HOME=str(home), HERMES_HOME=str(home), TMPDIR=str(tmp_path),
               TMP=str(tmp_path), TEMP=str(tmp_path), PYTHONUTF8="1")
    source = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    probe = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
network = []
def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        network.append((event, str(args)))
        raise RuntimeError("network forbidden during offline context sizing")
sys.addaudithook(audit)
from hermes_cli.prompt_size import _build_inspection_agent, compute_prompt_breakdown
from agent.system_prompt import build_system_prompt
agent = _build_inspection_agent("cli")
try:
    prompt = build_system_prompt(agent)
    project = Path("AGENTS.md").read_text(encoding="utf-8").strip()
    soul = (Path.home() / "SOUL.md").read_text(encoding="utf-8").strip()
    assert len(project) > 20_000 and len(soul) > 20_000
    assert project in prompt, "configured window lost: AGENTS.md was truncated at the flat cap"
    assert soul in prompt, "configured window lost: SOUL.md was truncated at the flat cap"
    data = compute_prompt_breakdown("cli")
    assert data["system_prompt"]["chars"] >= len(project) + len(soul)
    assert agent.context_compressor is None, "inspection started a compression engine"
    assert not network, network
    print("OFFLINE_CONTEXT_WINDOW_OK", len(project), len(soul), data["system_prompt"]["chars"])
finally:
    agent.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", probe, source], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OFFLINE_CONTEXT_WINDOW_OK" in result.stdout
