"""The `llm` tool: one auxiliary LLM call, text -> text.

RLM-style primitive (Recursive Language Models, Zhang & Khattab,
arXiv:2512.24601): the caller (model or user code inside execute_code) can
consult an LLM on an arbitrary slice of a larger input without loading that
input into its own context window. Semantics follow the spike validation
(2026-09-08, /workspace/jefatura-dev/spikes/001-rlm-llm-primitive): with a
strong routing model this primitive is what lets a root model chunk, filter
and semantically judge inputs ~10x its window at ~1/5 the tokens.

Design notes:
- Rides `call_llm` (agent/auxiliary_client.py): provider/model resolution,
  credential pools, fallback chains, per-task semaphores and config under
  `auxiliary.llm_tool` in config.yaml all come for free. No new client code.
- Output budget is CLAMPED, not trusted: the spike measured a sub-model
  without thinking emitting 1,084 tokens per YES/NO judgment when unclamped.
- Errors return JSON (`success: false`), never raise: inside execute_code a
  raised exception kills the whole script mid-loop (spike run v3 burned 60
  calls then died on exactly this).
"""

import json
import logging
from typing import Any, Dict

from tools.registry import registry

logger = logging.getLogger(__name__)

#: Hard clamp on max_tokens per call. Generous for a judgment call, small
#: enough that a runaway loop of 50 calls (the execute_code tool budget)
#: cannot spend more than ~40k output tokens in one script.
MAX_OUTPUT_TOKENS = 800

#: Sensible default for judgment-style sub-calls (spike: flash with thinking
#: off answered 8-way classification in ~30 completion tokens).
DEFAULT_MAX_TOKENS = 300

LLM_SCHEMA = {
    "name": "llm",
    "description": (
        "Ask an auxiliary LLM one self-contained question and get its text answer. "
        "Use it to make semantic judgments (classify, extract, judge, translate, "
        "summarize) on a SPECIFIC slice of text you already have (a chunk, an entry, "
        "a candidate list) instead of loading a huge input into this conversation. "
        "For bulk work, call it from execute_code: "
        "`from hermes_tools import llm` lets you loop it over slices "
        "(map-reduce over a corpus). One call = one judgment; do not chat with it. "
        "Returns JSON: {success, text, usage}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The complete question plus the text slice to judge. Self-contained.",
            },
            "system": {
                "type": "string",
                "description": "Optional role/instruction for the sub-model (e.g. 'Answer only YES or NO').",
            },
            "max_tokens": {
                "type": "integer",
                "description": f"Output cap for the answer (default {DEFAULT_MAX_TOKENS}, max {MAX_OUTPUT_TOKENS}).",
            },
        },
        "required": ["prompt"],
    },
}


def check_llm_requirements() -> bool:
    """Available whenever the auxiliary client can resolve any provider.

    The resolution chain (config `auxiliary.llm_tool` -> auxiliary auto-route
    -> main agent model) handles credentials; an entirely unconfigured host
    fails at call time with a JSON error, which is the honest signal.
    """
    return True


def _clamp_max_tokens(value: Any) -> int:
    try:
        requested = int(value) if value is not None else DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        requested = DEFAULT_MAX_TOKENS
    return max(1, min(requested, MAX_OUTPUT_TOKENS))


def llm_tool(prompt: str = "", system: str = "", max_tokens: int = None, **_ignored: Any) -> str:
    """Handle one auxiliary LLM call; ALWAYS returns a JSON string."""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    prompt = (prompt or "").strip()
    if not prompt:
        return json.dumps({"success": False, "error": "prompt is required and empty"},
                          ensure_ascii=False)
    effective_max = _clamp_max_tokens(max_tokens)

    messages: list = []
    if system:
        messages.append({"role": "system", "content": str(system)})
    messages.append({"role": "user", "content": prompt})

    try:
        response = call_llm(
            task="llm_tool", messages=messages, max_tokens=effective_max,
        )
    except Exception as exc:  # noqa: BLE001 - errors are tool output, not crashes
        logger.warning("llm tool call failed: %s", exc)
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    usage: Dict[str, int] = {}
    u = getattr(response, "usage", None)
    if u is not None:
        usage = {
            "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        }
    text = extract_content_or_reasoning(response) or ""
    return json.dumps({"success": True, "text": text, "usage": usage}, ensure_ascii=False)


registry.register(
    name="llm", toolset="llm", schema=LLM_SCHEMA,
    handler=lambda args, **kw: llm_tool(
        prompt=args.get("prompt", ""), system=args.get("system", ""),
        max_tokens=args.get("max_tokens"),
    ),
    check_fn=check_llm_requirements, emoji="🧠",
)
