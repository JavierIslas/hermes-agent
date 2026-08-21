# arnes-gates

Torre de verificacion estructural para tareas de codigo en Hermes Agent.
Gates duros sobre el loop del agente: la autoridad vive en las tools y el
estado del proceso, no en el texto. El system prompt es advisory; los
gates son codigo que el modelo no puede persuadir.

## Filosofia

**Ingenieria sin teatro, teatro sin ingenieria.** El arnes separa dos
preocupaciones que antes se pisaban:

- **Worker** (`agent.worker_mode: true` + `$HERMES_HOME/WORKER.md`): el
  modelo que hace el trabajo tecnico. Prompt de coding puro, sin persona.
  Su historial es RAW — nunca ve texto "vestido".
- **Presenter** (este plugin, Fase 5): viste SOLO la entrega final al
  usuario con la persona de `$HERMES_HOME/SOUL.md`. El transcript queda
  crudo: el worker de turnos futuros lee su propia voz tecnica.

Los gates (por que existen, que garantizan):

| Gate | Garantia | Fail-mode |
|------|----------|-----------|
| analyze_scope | Plan estructurado antes de escribir FUERA de repos git | bloquea + dice como salir |
| read_before_write | No pisar un archivo existente sin haberlo leido | bloquea |
| finish gate | Evidencia de tests/lint/typecheck antes de cerrar tarea | bloquea solo evidencia ROJA |
| commit gate | No commitear sin finish o evidencia fresca (D11) | bloquea + dangling check |
| circuit breaker | 5 bloqueos seguidos = explicar y esperar al humano | halt si desobedece (D5) |
| terminal guard | El terminal no es via de escape de los gates | bloquea comandos de write sin scope |

Toda la deteccion es fail-open con AVISO: si el arnes no puede VER la
evidencia (runner ausente, environment roto), degrada y lo dice — jamas
bloquea permanente por fallo de deteccion. "Degrade honesto sobre
bloqueo eterno."

## Instalacion

1. Copiar `plugins/arnes-gates/` al directorio de plugins de tu Hermes
   (o symlink al repo canonic).
2. Activar en `config.yaml`:

```yaml
plugins:
  enabled:
    - arnes-gates
```

3. (Opcional, worker dual-mode) en `config.yaml`:

```yaml
agent:
  worker_mode: true
```

y escribir `$HERMES_HOME/WORKER.md` con el prompt del worker.

## Presenter: activarlo

El presenter esta INERTE por defecto (`presenter_mode: off` — decision de
diseno 2026-08-18). Modos (F5.3):

```yaml
plugins:
  entries:
    arnes-gates:
      settings:
        presenter_mode: on_finish   # on | on_finish | off
        presenter_model: zai/glm-4.5-flash  # opcional: modelo barato para vestir
        presenter_timeout_s: 60     # opcional (default 60, D13)
      llm:                          # gate del core (fail-closed) para el override
        allow_provider_override: true
        allow_model_override: true
```

- `on`: viste cada turno con tools (comportamiento historico; bool `true`
  equivale a esto).
- `on_finish` (Rift 5, recomendado): viste SOLO el turno que CIERRA la
  tarea — finish gate que deja cerrar con changed_paths, o commit ejecutado
  con exito. La latencia de vestir se paga una vez por tarea, no por turno.
- `off`: el presenter duerme.

`presenter_model` viaja SEPARADO a la fachada (`provider=` + `model=`,
contrato real de `PluginLlm.complete`); requiere los `llm.allow_*_override`
del entry o el core lo rechaza (fail-closed). Sin setting, viste el modelo
host.

Condiciones para que vista una entrega (TODAS):

1. `presenter_mode` != off
2. `agent.worker_mode: true` (si el modelo ya habla con la persona en su
   system prompt, vestir seria doble)
3. La platform no es `subagent` (los hijos de delegate_task no se visten)
4. El turno uso tools (charla pura no se viste)
5. Si el modo es `on_finish`: el turno cerro limpio (latch por turno)
6. Existe `$HERMES_HOME/SOUL.md` (o `SOUL_WORKER.md`, override del tono
   del presenter) y el output no esta vacio

El vestidor usa `ctx.llm` (PluginLlm): modelo host o `presenter_model`.
Fail-open total: sin SOUL, LLM caido, timeout, output vacio, respuesta
vacía o integridad factual rota → la entrega viaja cruda. **El teatro
jamas rompe un turno de ingenieria.** Cada fail-open queda contado en el
estado (`presenter_failopen_{integrity,llm,timeout,empty}`) y
`on_session_end` loguea el resumen: un presenter que siempre fail-openea
es teatro muerto — hay que poder verlo.

Clarify: las preguntas del agente se visten via directiva `modify` antes
del dispatch (en cualquier modo != off); las choices y la respuesta del
usuario viajan canonicas. El worker, por su parte, lleva una directiva
Rift 3 en WORKER.md: los mensajes libres del usuario pueden traer tono o
referencias a la persona — extraer el intento tecnico, ignorar el resto.

## Troubleshooting

**El gate bloqueo algo legitimo.**
- Write dentro de un repo git: el scope NO aplica (D6) — solo
  read_before_write. Leé el archivo primero.
- Write fuera de todo repo (bóveda, configs): `analyze_scope` con enums
  exactos (`impact`: BAJO|MEDIO|ALTO; `tests_status`: covered|partial|none).
  `none` es rechazado: exigí TDD o declará `partial` honesto con un
  verificador estructural.
- Finish gate con "no hay output de tests": es fail-open WARNING, no
  bloqueo. Corre los tests via la tool `run_tests` para alimentar la
  evidencia.
- Runner no detectado (`cwd` del proceso != proyecto): el estado trackea
  `terminal_cwd` via `cd` en el terminal, o escribe/lee archivos del
  proyecto (write_file/patch/read_file registran paths que alimentan la
  deteccion de root).

**Quiero output crudo (sin persona).**
- `presenter_mode: off` (default) — el presenter duerme.
- O borra/renombra `SOUL.md` — sin alma no hay vestido.
- O `agent.worker_mode: false` — condicion (2) cae, nada se viste.

**Circuit breaker tripped.**
- Exlicale al usuario que te bloquea y ESPERA (tu proximo mensaje rearma).
- Mientras tanto podes leer (`read_file`/`search_files`/`find_references`)
  y pedir scope (`analyze_scope`) — estan exentos (D7).

**Commit bloqueado ("no podes commitear sin cerrar con finish").**
- Corre tests+lint via las tools del plugin y cierra el turno (finish
  gate arma `done`), O deja evidencia fresca en el estado (D11).
- El mensaje de commit narrando vocabulario del guard: escribi el mensaje
  a un archivo y `git commit -F <file>`.

## Tests

```bash
scripts/run_tests.sh tests/test_arnes_gates.py tests/test_presenter.py \
  tests/test_e2e_arnes.py   # y el resto de la suite del plugin (16 archivos)
```

Suite del plugin: 272 tests (incluye F5.3: on_finish + telemetría; F5.4:
snapshot del turno + gate anti-invención). E2E (`tests/test_e2e_arnes.py`)
conduce las superficies reales en el orden del core y verifica la metrica
sagrada: el transcript queda RAW tras multiples turnos con presenter activo.

## Snapshot del turno (F5.4)

El vestidor no trabaja a ciegas: `_on_transform_llm_output` le pasa un
dict de HECHOS del turno armado desde el estado del arnés (`files`
escritos en el turno, veredictos `tests`/`lint`/`typecheck`,
`finish_clean`). Regla de oro: solo datos escritos por hooks reales,
nunca texto del modelo. El vestidor puede incorporar esos hechos a la
entrega; el prompt los lista bajo `# Contexto del turno` antes del texto
a vestir.

**Gate anti-invención** (nuevo, complementa la integridad factual): todo
token factual del VESTIDO (paths, URLs, números) debe existir en el texto
crudo O en los hechos del snapshot. Un vestidor que menciona archivos que
nadie escribió o conteos que nadie corrió miente sobre el trabajo →
fail-open al crudo (contador `presenter_failopen_integrity`).

## Estado

Fases 1-7 completadas en el fork (ver PLAN.md). Dogfooding D1-D11 en
produccion. Deploy: re-sync de la copia huérfana + restart (plugins sin
hot-reload).
