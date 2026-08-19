"""Presenter: viste la entrega del worker con la persona de SOUL.md.

Fase 5 del arnes. Diseno verificado 2026-08-18 contra el core post-merge:

- El hook NATIVO ``transform_llm_output`` dispara en turn_finalizer
  DESPUES de persistir el mensaje assistant: el transcript queda RAW y solo
  la entrega al usuario se viste (Grieta 1 resuelta por la estructura del
  core; no existe ``display_text`` que inventar).
- El LLM es la fachada ``ctx.llm`` (PluginLlm): modelo host, sin API key
  propia. Presenter recibe la fachada directamente, no el ctx completo.
- Fail-open TOTAL: cualquier fallo (sin SOUL, LLM caido, output vacio,
  respuesta vacia) devuelve el texto original. El teatro jamas rompe un
  turno de ingenieria.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Timeout (segundos) para la llamada de vestir. Dogfood 2026-08-19: con
# glm-5.3, 20s alcanzaba solo para outputs cortos (~80 chars vistieron en
# ~8s); los turnos reales de coding (~2K chars) timeoutaban y fail-openeaban
# al crudo (2 de 3 disparos). Orden del usuario: 1 minuto ("puedo esperar").
# Al vencer, fail-open al texto original.
_DRESS_TIMEOUT_S = 60.0


def _load_soul() -> Optional[str]:
    """Lee el alma del presenter de HERMES_HOME.

    SOUL_WORKER.md (si existe) es el override del vestidor; si no, SOUL.md.
    Dos archivos, dos propositos: el tono del presenter se personaliza sin
    tocar la identidad que el core inyecta en el system prompt.
    """
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except Exception as exc:  # pragma: no cover - defensivo
        logger.debug("presenter: no se resolvio HERMES_HOME: %s", exc)
        return None

    for name in ("SOUL_WORKER.md", "SOUL.md"):
        path = home / name
        try:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        except Exception as exc:
            logger.debug("presenter: fallo leyendo %s: %s", path, exc)
    return None


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


class Presenter:
    """Viste output/preguntas del worker con la persona de SOUL."""

    def __init__(self, llm: Any):
        self._llm = llm

    # -- API publica --------------------------------------------------------

    def present(self, worker_output: str) -> str:
        """Viste el output final del worker. Fail-open total."""
        if not worker_output or not worker_output.strip():
            return worker_output
        soul = _load_soul()
        if not soul:
            return worker_output
        try:
            result = self._llm.complete(
                messages=[
                    {"role": "system", "content": soul},
                    {"role": "user", "content": _DRESS_OUTPUT_PROMPT.format(
                        worker_output=worker_output)},
                ],
                timeout=_DRESS_TIMEOUT_S,
                purpose="arnes-gates presenter: vestir output",
            )
            dressed = getattr(result, "text", None)
            if not dressed or not dressed.strip():
                return worker_output
            if dressed.strip() == worker_output.strip():
                return worker_output
            return dressed
        except Exception as exc:
            logger.warning("presenter: fail-open al vestir output (%s)", exc)
            return worker_output

    def dress_question(self, question: str) -> str:
        """Viste la pregunta de clarify. Fail-open total."""
        if not question or not question.strip():
            return question
        soul = _load_soul()
        if not soul:
            return question
        try:
            result = self._llm.complete(
                messages=[
                    {"role": "system", "content": soul},
                    {"role": "user", "content": _DRESS_QUESTION_PROMPT.format(
                        question=question)},
                ],
                timeout=_DRESS_TIMEOUT_S,
                purpose="arnes-gates presenter: vestir pregunta",
            )
            dressed = getattr(result, "text", None)
            if not dressed or not dressed.strip():
                return question
            return dressed
        except Exception as exc:
            logger.warning("presenter: fail-open al vestir pregunta (%s)", exc)
            return question


_DRESS_OUTPUT_PROMPT = """\
Vas a presentar al usuario el resultado de un turno de trabajo tecnico.
El texto fue producido por un worker de ingenieria: seco, factual.

REGLAS:
- Mantene INTACTO todo el contenido factual: paths, numeros de linea,
  comandos, resultados de tests, mensajes de error, URLs.
- Cambia SOLO el tono y la presentacion: tu voz, tu personalidad.
- No agregues informacion tecnica nueva ni omitas ninguna.
- No menciones al worker, ni que hubo una transformacion: eres tu hablando.
- Responde con el texto vestido y nada mas (sin preambulos ni cierre).

# Texto a vestir
{worker_output}
"""

_DRESS_QUESTION_PROMPT = """\
Vas a preguntarle algo al usuario, con tu voz.
El worker interno formulo esta pregunta tecnica; vos la presents.

REGLAS:
- Es UNA pregunta: mantene que siga siendo una pregunta.
- No agregues opciones nuevas ni enumeres las que el worker no listó.
- No alteres terminos tecnicos (branch names, paths, comandos).
- Responde con la pregunta vestida y nada mas (sin preambulos).

# Pregunta original
{question}
"""


__all__ = ["Presenter", "_load_soul"]
