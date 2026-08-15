"""Tests de la cirugía F4-2/F4-2b: worker mode via WORKER.md.

Contrato (PLAN.md Task 4.2 REDISEÑADA + Corrección 2):

1. worker_mode=False (default) → slot #1 = SOUL.md como hoy; sin WORKER.md
   en el prompt.
2. worker_mode=True + WORKER.md presente → slot #1 = contenido de WORKER.md;
   SOUL.md NO aparece (ni como identidad ni via context files).
3. worker_mode=True + WORKER.md ausente → fallback DEFAULT_AGENT_IDENTITY
   (core sano sin el plugin).
4. load_worker_md espeja load_soul_md: HERMES_HOME, vacío → None.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from run_agent import AIAgent, DEFAULT_AGENT_IDENTITY


def _make_agent(**kwargs):
    """AIAgent real con los patches mínimos del dialecto test_run_agent."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("terminal")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            api_key="test-k...7890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            **kwargs,
        )


def _make_tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"tool {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


# =============================================================================
# load_worker_md: espejo de load_soul_md.
# =============================================================================
class TestLoadWorkerMd:
    def _load(self, monkeypatch, tmp_path, worker_text):
        import agent.prompt_builder as pb

        monkeypatch.setattr(pb, "get_hermes_home", lambda: tmp_path)
        if worker_text is not None:
            (tmp_path / "WORKER.md").write_text(worker_text, encoding="utf-8")
        return pb.load_worker_md(context_length=None)

    def test_reads_worker_md_from_hermes_home(self, monkeypatch, tmp_path):
        result = self._load(monkeypatch, tmp_path, "sos el worker. crudo.\n")
        assert result is not None
        assert "worker" in result.lower()

    def test_absent_returns_none(self, monkeypatch, tmp_path):
        assert self._load(monkeypatch, tmp_path, None) is None

    def test_empty_returns_none(self, monkeypatch, tmp_path):
        assert self._load(monkeypatch, tmp_path, "   \n") is None


# =============================================================================
# system_prompt: rama worker en el slot de identidad.
# =============================================================================
class TestWorkerIdentitySlot:
    def test_worker_mode_uses_worker_md_not_soul(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("terminal")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("run_agent.load_soul_md", return_value="SOS MARIA BRINK BLOOD LEGION"),
            patch("run_agent.load_worker_md", return_value="Sos un agente de coding. Crudo."),
        ):
            agent = AIAgent(
                api_key="test-k...7890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                worker_mode=True,
            )
            prompt = agent._build_system_prompt()

        assert "agente de coding" in prompt
        assert "MARIA" not in prompt
        assert "BLOOD LEGION" not in prompt

    def test_default_mode_still_uses_soul(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("terminal")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("run_agent.load_soul_md", return_value="SOS MARIA BRINK BLOOD LEGION"),
            patch("run_agent.load_worker_md", return_value="Sos un agente de coding. Crudo."),
        ):
            agent = AIAgent(
                api_key="test-k...7890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                load_soul_identity=True,
                skip_memory=True,
            )
            prompt = agent._build_system_prompt()

        assert "MARIA" in prompt
        assert "agente de coding" not in prompt

    def test_worker_mode_without_worker_md_falls_back(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("terminal")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("run_agent.load_soul_md", return_value="SOS MARIA BRINK BLOOD LEGION"),
            patch("run_agent.load_worker_md", return_value=None),
        ):
            agent = AIAgent(
                api_key="test-d...7890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                worker_mode=True,
            )
            prompt = agent._build_system_prompt()

        # Fallback: DEFAULT_AGENT_IDENTITY (ni SOUL ni WORKER).
        assert DEFAULT_AGENT_IDENTITY in prompt
        assert "MARIA" not in prompt
        assert "agente de coding" not in prompt


# =============================================================================
# Invariante de caching: worker_mode se lee ANTES del primer build.
# =============================================================================
def test_worker_mode_default_false_in_config_defaults():
    """config_defaults: agent.worker_mode existe y defaultea False."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    agent_section = DEFAULT_CONFIG.get("agent", {})
    assert "worker_mode" in agent_section
    assert agent_section["worker_mode"] is False


def test_agent_attr_defaults_false_without_config():
    """agent_init sin agent.worker_mode en config → atributo False."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("terminal")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-k...7890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    assert agent.worker_mode is False
