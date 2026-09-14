# Smoke test — Fase 7 (2026-08-18)

## Parte automatizada (verificada)

**Suite E2E** (`tests/test_e2e_arnes.py`, 6 tests, parte de la suite 227/227
en 16 archivos — log: suite_f7.log):

- [x] El agente llama analyze_scope y el plan valido prende scope_done.
- [x] Escritura sin lectura previa sobre archivo existente: BLOQUEA,
      archivo intacto (read_before_write; in-repo el scope no aplica — D6).
- [x] Escritura fuera de todo repo sin scope: BLOQUEA con salida indicada.
- [x] run_tests corre el runner REAL del proyecto (pytest contra el juguete)
      y el finish gate arma `done` con evidencia verde.
- [x] Evidencia ROJA ("3 failed"): finish gate bloquea y manda a correr tests.
- [x] Evidencia AUSENTE: fail-open con WARNING (degradar > bloquear eterno).
- [x] Output final con personalidad: presenter viste la entrega con el
      stub LLM cuando las 5 condiciones se cumplen.
- [x] presenter_mode=off: entrega cruda, cero llamadas LLM (deploy inerte).
- [x] **Metrica sagrada F7**: tras 3 turnos con presenter ACTIVO, el
      transcript que lee el worker esta 100% RAW — cera contaminacion.
- [x] Registro completo: los 6 hooks cuelgan de PluginContext.

**Smoke de produccion** (carga como la carga el runtime, via symlink a la
huerfana, 2026-08-18 ~16:30):

- [x] register() registra los 6 hooks (incluido transform_llm_output).
- [x] presenter_mode default off; worker_mode False en config real ->
      routing inerte: produccion estable con el plugin dormido.
- [x] diff huerfana vs canonica: solo README.md (pendiente de sync).

## Parte interactiva (del lado del usuario)

Sesion real de coding con presenter DESPIERTO — el dogfood final:

```bash
cd ~/workspace/desarrollosPersonales/hermes-fork/hermes-agent
source .venv/bin/activate
hermes chat
# pedir algo simple: "agrega validacion de telefono en user.py"
```

Checklist en vivo:
- [ ] analyze_scope antes de escribir (o write in-repo directo tras read).
- [ ] Escrituras sin lectura previa bloqueadas.
- [ ] El cierre de la tarea llega con personalidad (presenter).
- [ ] Los tool calls se ven en streaming durante el turno.
- [ ] En el turno SIGUIENTE, el agente no "recuerda" su voz teatral
      (transcript RAW — la prueba de fuego de la Grieta 1).

Requiere: presenter_mode on + agent.worker_mode true en la config de esa
instancia + SOUL.md presente. Ver README.md del plugin ("Presenter:
activarlo").
