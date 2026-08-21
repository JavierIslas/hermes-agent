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

F5.2 (2026-08-19), tres mejoras del dogfooding:

- GATE DE INTEGRIDAD FACTUAL: el arnes no confia en modelos y el vestidor
  ES un modelo. Tras vestir, se extraen los tokens factuales del crudo
  (paths, URLs, numeros) y se exige su presencia en el vestido; si falta
  alguno, fail-open al crudo — un vestido que miente es peor que crudo.
- SETTINGS PARAMETRIZABLES via ``get_config`` (la del PluginContext):
  * ``presenter_timeout_s`` (default 60, D13): timeout de la llamada.
  * ``presenter_model`` (default None = modelo host): ref "provider/model"
    para vestir con un modelo mas barato. Requiere que el host habilite
    ``allow_model_override`` en el settings llm del plugin (fail-closed
    del core); sin eso, la fachada ignora el override.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Timeout (segundos) para la llamada de vestir. Dogfood 2026-08-19 (D13): con
# glm-5.3, 20s alcanzaba solo para outputs cortos (~80 chars vistieron en
# ~8s); los turnos reales de coding (~2K chars) timeoutaban y fail-openeaban
# al crudo (2 de 3 disparos). Orden del usuario: 1 minuto ("puedo esperar").
# Override via settings: presenter_timeout_s. Al vencer, fail-open al crudo.
_DEFAULT_DRESS_TIMEOUT_S = 60.0

_RE_URL = re.compile(r"https?://\S+")
_RE_NUM = re.compile(r"\d+(?:\.\d+)?")
# F5.5 (dogfood 2026-08-21): diffstats (+462/-15) y ratios (272/272) NO son
# paths. El extractor viejo exigia su presencia como substring exacta y el
# vestidor casi siempre los reformateaba ("+462 líneas / -15") →
# failopen:integrity perpetuo → entrega siempre cruda. Estos patrones se
# tratan como HECHOS NUMÉRICOS: sus números deben sobrevivir, su formato no.
_RE_DIFFSTAT = re.compile(r"[+-]\d+(?:\.\d+)?(?:/[+-]\d+(?:\.\d+)?)?")
_RE_RATIO = re.compile(r"(?<![\w./])\d+(?:\.\d+)?/\d+(?:\.\d+)?(?![\w./])")


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


# =============================================================================
# F5.2: gate de integridad factual del vestido.
# =============================================================================

def _factual_tokens(text: str) -> set[str]:
    """Tokens factuales del texto: URLs, paths, numeros.

    URLs y paths se exigen como substring exacta. Los diffstats
    (+462/-15) y ratios (272/272) NO se exigen como substring: sus
    NUMEROS ya entran via _RE_NUM y sobreviven aunque el vestidor los
    reformatee ("+462 líneas / -15"). El resto de tokens con '/' son
    paths y se exigen exactos.
    """
    tokens: set[str] = set()
    for url in _RE_URL.findall(text):
        url = url.rstrip(".,;:!?)]}\"'")
        if url:
            tokens.add(url)
    for tok in text.split():
        tok = tok.strip(".,;:!?()[]{}\"'`<>*")
        if not tok or "/" not in tok or tok.startswith("http"):
            continue
        if _RE_DIFFSTAT.fullmatch(tok) or _RE_RATIO.fullmatch(tok):
            continue  # diffstat/ratio: sus numeros ya estan via _RE_NUM
        tokens.add(tok)
    for num in _RE_NUM.findall(text):
        tokens.add(num)
    return tokens


def _factual_integrity_ok(raw: str, dressed: str) -> bool:
    """True si todo token factual del crudo sobrevive en el vestido."""
    for tok in _factual_tokens(raw):
        if tok not in dressed:
            logger.warning(
                "presenter: fail-open por integridad factual (token perdido: %r)",
                tok,
            )
            return False
    return True


def _snapshot_allowed_text(snapshot: Optional[dict]) -> str:
    """Texto plano con los HECHOS autorizados del snapshot.

    Los tokens factuales de este texto quedan autorizados en el vestido
    (paths escritos, conteos de tests). El snapshot lo arma el plugin con
    datos del gate_state — nunca texto del modelo.
    """
    if not snapshot:
        return ""
    parts: list[str] = []
    files = snapshot.get("files") or []
    if files:
        parts.append(" ".join(str(f) for f in files))
    for key in ("tests", "lint", "typecheck"):
        val = snapshot.get(key)
        if val:
            parts.append(str(val))
    return " ".join(parts)


def _no_invented_tokens(dressed: str, raw: str, snapshot: Optional[dict]) -> bool:
    """True si el vestido no inventa tokens factuales.

    Gate anti-invencion (F5.4): todo token factual del VESTIDO (paths,
    URLs, numeros) debe existir en el RAW o en los hechos del snapshot.
    Un vestidor que menciona archivos que nadie escribio o conteos que
    nadie corrio esta mintiendo sobre el trabajo → fail-open al crudo.
    """
    allowed = raw + " " + _snapshot_allowed_text(snapshot)
    for tok in _factual_tokens(dressed):
        if tok not in allowed:
            logger.warning(
                "presenter: fail-open por invencion (token no autorizado: %r)",
                tok,
            )
            return False
    return True


def _coerce_timeout(value: Any) -> float:
    """Timeout honesto: numero positivo o default. Nunca trona."""
    try:
        t = float(value)
        return t if t > 0 else _DEFAULT_DRESS_TIMEOUT_S
    except (TypeError, ValueError):
        return _DEFAULT_DRESS_TIMEOUT_S


class Presenter:
    """Viste output/preguntas del worker con la persona de SOUL.

    ``on_event`` (F5.3): callback opcional ``fn(event: str)`` que recibe
    ``"dressed"`` o ``"failopen:integrity|llm|timeout|empty|nosoul"`` tras
    cada intento de vestir. Telemetría: el dogfood D13 mostró fail-opens
    invisibles (2/3 vestidos morían en timeout sin registro observable).
    Nunca rompe el turno: un callback que truca se traga.
    """

    def __init__(self, llm: Any, get_config: Optional[Callable[..., Any]] = None,
                 on_event: Optional[Callable[[str], None]] = None):
        self._llm = llm
        self._get_config = get_config or (lambda key, default=None: default)
        self._on_event = on_event

    def _emit(self, event: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # pragma: no cover - telemetría jamás rompe
            logger.debug("presenter: on_event falló (ignorado)", exc_info=True)

    # -- settings (F5.2) -----------------------------------------------------

    def _timeout_s(self) -> float:
        return _coerce_timeout(
            self._get_config("presenter_timeout_s", _DEFAULT_DRESS_TIMEOUT_S)
        )

    def _model_kw(self) -> dict[str, str]:
        """presenter_model como kwargs provider= + model= SEPARADOS.

        F5.3 (2026-08-21) — fix de contrato: la firma real de
        ``PluginLlm.complete()`` (/opt/hermes/agent/plugin_llm.py) es
        ``provider=`` y ``model=`` independientes, gateada por
        ``plugins.entries.<id>.llm.allow_model_override``. El código viejo
        pasaba la ref entera "provider/model" como ``model`` →
        PluginLlmTrustError → fail-open silencioso SIEMPRE.

        Formatos aceptados: "provider/model" (primera barra divide; el resto
        del string es el model, p.ej. "openrouter/qwen/qwen-2.5"), "model"
        solo (sin barra; provider queda al host), o nada. Un bool True de
        YAML natural NO es una ref de modelo y se ignora.
        """
        m = self._get_config("presenter_model", None)
        if not (isinstance(m, str) and m.strip()):
            return {}
        m = m.strip()
        if "/" in m:
            provider, _, model = m.partition("/")
            if provider.strip() and model.strip():
                return {"provider": provider.strip(), "model": model.strip()}
            return {}
        return {"model": m}

    # -- API publica --------------------------------------------------------

    def present(self, worker_output: str, ctx_snapshot: Optional[dict] = None) -> str:
        """Viste el output final del worker. Fail-open total.

        ``ctx_snapshot`` (F5.4): hechos estructurados del turno (files,
        tests, lint, finish_clean) que el vestidor puede incorporar. Los
        arma el plugin desde gate_state — nunca texto del modelo.
        """
        if not worker_output or not worker_output.strip():
            return worker_output
        soul = _load_soul()
        if not soul:
            self._emit("failopen:nosoul")
            return worker_output
        try:
            kw: dict[str, Any] = {
                "timeout": self._timeout_s(),
                "purpose": "arnes-gates presenter: vestir output",
            }
            kw.update(self._model_kw())
            result = self._llm.complete(
                messages=[
                    {"role": "system", "content": soul},
                    {"role": "user", "content": _build_dress_output_prompt(
                        worker_output, ctx_snapshot)},
                ],
                **kw,
            )
            dressed = getattr(result, "text", None)
            if not dressed or not dressed.strip():
                self._emit("failopen:empty")
                return worker_output
            if dressed.strip() == worker_output.strip():
                logger.info(
                    "presenter: vestidor devolvio el texto sin cambios "
                    "(idempotente) — entrega cruda")
                return worker_output
            if not _factual_integrity_ok(worker_output, dressed):
                self._emit("failopen:integrity")
                return worker_output
            if not _no_invented_tokens(dressed, worker_output, ctx_snapshot):
                self._emit("failopen:integrity")
                return worker_output
            self._emit("dressed")
            return dressed
        except TimeoutError as exc:
            self._emit("failopen:timeout")
            logger.warning("presenter: fail-open al vestir output por timeout (%s)", exc)
            return worker_output
        except Exception as exc:
            self._emit("failopen:llm")
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
            kw: dict[str, Any] = {
                "timeout": self._timeout_s(),
                "purpose": "arnes-gates presenter: vestir pregunta",
            }
            kw.update(self._model_kw())
            result = self._llm.complete(
                messages=[
                    {"role": "system", "content": soul},
                    {"role": "user", "content": _DRESS_QUESTION_PROMPT.format(
                        question=question)},
                ],
                **kw,
            )
            dressed = getattr(result, "text", None)
            if not dressed or not dressed.strip():
                return question
            if not _factual_integrity_ok(question, dressed):
                return question
            return dressed
        except Exception as exc:
            logger.warning("presenter: fail-open al vestir pregunta (%s)", exc)
            return question


def _build_dress_output_prompt(worker_output: str, ctx_snapshot: Optional[dict] = None) -> str:
    """Arma el prompt de vestir: contexto del turno (hechos) + texto.

    El bloque '# Contexto del turno' va ANTES del '# Texto a vestir':
    el stub echo del E2E extrae tras ese marcador final, y los vestidores
    reales leen los hechos primero y la entrega despues.
    """
    snapshot_block = ""
    if ctx_snapshot:
        lines = []
        files = ctx_snapshot.get("files") or []
        if files:
            lines.append("- Archivos del turno: " + ", ".join(str(f) for f in files))
        for key, label in (("tests", "Tests"), ("lint", "Lint"),
                           ("typecheck", "Typecheck")):
            val = ctx_snapshot.get(key)
            if val:
                lines.append(f"- {label}: {val}")
        if ctx_snapshot.get("finish_clean"):
            lines.append("- Cierre de tarea verificado (finish gate)")
        if lines:
            snapshot_block = (
                "# Contexto del turno (hechos verificados por el arnés; "
                "podés mencionarlos, no inventar otros)\n" + "\n".join(lines) + "\n\n"
            )
    return _DRESS_OUTPUT_PROMPT.format(
        worker_output=worker_output, snapshot_block=snapshot_block)


_DRESS_OUTPUT_PROMPT = """\
Vas a presentar al usuario el resultado de un turno de trabajo tecnico.
El texto fue producido por un worker de ingenieria: seco, factual.

REGLAS:
- Mantene INTACTO todo el contenido factual del texto: paths, numeros de
  linea, comandos, resultados de tests, mensajes de error, URLs.
- Cambia SOLO el tono y la presentacion: tu voz, tu personalidad.
- No agregues informacion tecnica nueva ni omitas ninguna. Podes incorporar
  los hechos del contexto del turno si suman a la entrega; NUNCA inventes
  paths, numeros ni resultados que no esten en el texto o el contexto.
- No menciones al worker, ni que hubo una transformacion: eres tu hablando.
- Responde con el texto vestido y nada mas (sin preambulos ni cierre).

{snapshot_block}# Texto a vestir
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


__all__ = [
    "Presenter",
    "_load_soul",
    "_factual_tokens",
    "_factual_integrity_ok",
    "_snapshot_allowed_text",
    "_no_invented_tokens",
    "_build_dress_output_prompt",
]
