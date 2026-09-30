"""E2E smoke of the worker/presenter pairing contract (see test_presenter_pair_replay.py).

Real AIAgent, two turns, a transform hook that dresses the final response:
- the user-facing delivery and the durable content column keep the DRESSED text (#44239);
- the api_content sidecar and the next turn's wire replay keep the RAW model text.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from run_agent import AIAgent


def _dressing_hook():
    import hermes_cli.lifecycle as lifecycle

    real = lifecycle.invoke_hook

    def invoke_hook(name, **kw):
        if name == "transform_llm_output":
            return ["VESTIDA: criatura, " + kw["response_text"]]
        return real(name, **kw)

    return invoke_hook


def test_two_turn_pairing_user_sees_dressed_model_replays_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="k-test-123456", base_url="https://example.com/v1", model="fake/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True, platform="cli",
            session_id="smoke-pair", session_db=db,
        )

    wire_calls = []

    def fake_create(**kwargs):
        wire_calls.append(
            [dict(role=m.get("role"), content=m.get("content")) for m in kwargs["messages"]]
        )
        msg = SimpleNamespace(content="RESPUESTA CRUDA DEL WORKER", tool_calls=None, reasoning=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="fake/model",
        )

    agent.client = MagicMock()
    agent.client.chat.completions.create = fake_create

    with patch("hermes_cli.lifecycle.invoke_hook", _dressing_hook()):
        r1 = agent.run_conversation("primer mensaje")
        # User-facing: dressed.
        assert r1["final_response"] == "VESTIDA: criatura, RESPUESTA CRUDA DEL WORKER"
        assert r1["response_transformed"] is True
        assert r1["pre_transform_response"] == "RESPUESTA CRUDA DEL WORKER"
        stored = [m for m in db.get_messages("smoke-pair") if m["role"] == "assistant"]
        assert stored, "assistant row not durable"
        # Durable display content: dressed (#44239 intact).
        assert stored[0]["content"] == r1["final_response"]
        # Replay sidecar: raw model bytes.
        assert stored[0].get("api_content") == "RESPUESTA CRUDA DEL WORKER"

        r2 = agent.run_conversation("segundo mensaje", conversation_history=r1["messages"])
        # What the MODEL saw of turn 1 in turn 2's request: the RAW text.
        call2 = wire_calls[-1]
        asst = [m for m in call2 if m["role"] == "assistant"]
        assert asst, "no assistant row on the wire"
        assert asst[-1]["content"] == "RESPUESTA CRUDA DEL WORKER"
        # Turn 2 delivery is dressed too (the loop keeps working both turns).
        assert r2["final_response"] == "VESTIDA: criatura, RESPUESTA CRUDA DEL WORKER"
