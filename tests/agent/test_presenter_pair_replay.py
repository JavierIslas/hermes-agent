"""Worker/presenter feedback loop: the MODEL must replay the RAW text, the USER
sees the dressed text (#44239 kept for display; new sidecar keeps the worker sane).

Regression: with worker/presenter pairing, `transform_llm_output` dresses the final
response. Upstream #44239 made the DRESSED text the stored+replayed content, so the
worker read its own dressed output as few-shot persona and adopted the presenter's
voice within a session (verified in presenter_pairs.jsonl: raw outputs carrying the
persona before the presenter ran). The fix: keep #44239's user-facing invariant
(`content` = transformed text) but stamp the RAW text on the `api_content` sidecar
(same mechanism as #111761's promoted reasoning), so `build_api_messages` replays
the exact bytes the model produced and the prompt-cache prefix stays byte-stable.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _rewriting_hook(calls):
    def invoke_hook(hook_name, **kwargs):
        calls.append(hook_name)
        if hook_name == "transform_llm_output":
            return ["REWRITTEN:" + kwargs["response_text"]]
        return []
    return invoke_hook


def _fake_completion(text):
    def create(**kwargs):
        msg = SimpleNamespace(content=text, tool_calls=None, reasoning=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15), model="fake/model",
        )
    return create


@pytest.fixture
def db_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    db = SessionDB(db_path=tmp_path / ".hermes" / "state.db")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", model="fake/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True, platform="cli",
            session_id="sess-pair", session_db=db,
        )
    agent.client = MagicMock()
    return agent, db


def test_model_replays_raw_while_user_sees_dressed(db_agent, monkeypatch):
    agent, db = db_agent
    calls = []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _rewriting_hook(calls))
    agent.client.chat.completions.create = _fake_completion("RAW MODEL TEXT")

    result = agent.run_conversation("hello")

    last_assistant = next(m for m in reversed(result["messages"]) if m.get("role") == "assistant")
    # User-facing invariant (#44239) is intact: delivery + stored content = dressed.
    assert result["final_response"] == "REWRITTEN:RAW MODEL TEXT"
    assert last_assistant["content"] == result["final_response"]
    stored = [r["content"] for r in db.get_messages("sess-pair") if r["role"] == "assistant"]
    assert stored == [result["final_response"]]
    # NEW: the worker replays its own raw text, not the dressed one.
    assert last_assistant.get("api_content") == "RAW MODEL TEXT"
    from agent.turn_context import build_api_messages
    api_messages, _ = build_api_messages(
        agent, result["messages"], current_turn_user_idx=None,
        ext_prefetch_cache=None, plugin_user_context=None, moa_config=None,
        active_system_prompt="sys",
    )
    on_wire = [m for m in api_messages if m.get("role") == "assistant"][-1]
    assert on_wire["content"] == "RAW MODEL TEXT"
    assert "api_content" not in on_wire  # sidecar never reaches the provider
    assert result["response_transformed"] is True
    assert result["pre_transform_response"] == "RAW MODEL TEXT"
    assert calls.count("transform_llm_output") == 1


def test_recovery_path_tail_row_carries_raw_sidecar(db_agent, monkeypatch):
    """Recovery `break` appends the closing assistant row in _close_transcript_tail;
    that row must carry the dressed content (display) AND the raw api_content sidecar."""
    from agent.turn_finalizer import finalize_turn

    agent, _db = db_agent
    calls = []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _rewriting_hook(calls))
    agent._persist_session = lambda *a, **k: None
    agent._current_turn_id = "turn-r"
    messages = [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"ok": True})},
    ]

    result = finalize_turn(
        agent, final_response="RECOVERED TEXT", api_call_count=1, interrupted=False, failed=False,
        messages=messages, conversation_history=None, effective_task_id="task-1", turn_id="turn-r",
        user_message="do a thing", original_user_message="do a thing", _should_review_memory=False,
        _turn_exit_reason="partial_stream_recovery",
    )

    assert result["final_response"].startswith("REWRITTEN:RECOVERED TEXT")
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "REWRITTEN:RECOVERED TEXT"
    # NEW: recovery tail row also carries the raw sidecar for the next replay.
    assert messages[-1].get("api_content") == "RECOVERED TEXT"
    assert calls.count("transform_llm_output") == 1
