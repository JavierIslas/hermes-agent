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
diseno 2026-08-18). Para despertarlo:

```yaml
plugins:
  entries:
    arnes-gates:
      settings:
        presenter_mode: on
```

Condiciones para que vista una entrega (TODAS):

1. `presenter_mode: on`
2. `agent.worker_mode: true` (si el modelo ya habla con la persona en su
   system prompt, vestir seria doble)
3. La platform no es `subagent` (los hijos de delegate_task no se visten)
4. El turno uso tools (charla pura no se viste)
5. Existe `$HERMES_HOME/SOUL.md` (o `SOUL_WORKER.md`, override del tono
   del presenter) y el output no esta vacio

El vestidor usa `ctx.llm` (PluginLlm): el modelo host del usuario, sin
API key propia. Fail-open total: sin SOUL, LLM caido, output vacio o
respuesta vacia → la entrega viaja cruda. **El teatro jamas rompe un
turno de ingenieria.**

Clarify: las preguntas del agente se visten via directiva `modify` antes
del dispatch; las choices y la respuesta del usuario viajan canonicas.

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

Suite del plugin: 227 tests (221 + 6 E2E). E2E (`tests/test_e2e_arnes.py`)
conduce las superficies reales en el orden del core y verifica la metrica
sagrada: el transcript queda RAW tras multiples turnos con presenter activo.

## Estado

Fases 1-7 completadas en el fork (ver PLAN.md). Dogfooding D1-D11 en
produccion. Deploy: re-sync de la copia huérfana + restart (plugins sin
hot-reload).
