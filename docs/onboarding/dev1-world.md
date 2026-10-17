# Onboarding: Dev 1 (`world/`)

> Si sos un Claude Code leyendo esto: tu dev es el dueño de `world/`. Leé
> también [`CLAUDE.md`](../../CLAUDE.md) y [ARQUITECTURA.md](../ARQUITECTURA.md).

## Tu misión en una frase

Que el mundo AP2 real (comercio atacante, catálogo público y benchmark) corra
de punta a punta con un LLM y produzca los números de la demo.

## Qué ya existe

**`world/` está hecho.** Todo lo de abajo ya funciona y tiene tests:

| Qué | Dónde |
|---|---|
| Copia del sample oficial de AP2 (human-present / cards) | [`world/ap2/`](../../world/ap2/) (commit en `world/ap2/UPSTREAM_COMMIT`) |
| Cambios al sample, listados | [`world/NOTICE.md`](../../world/NOTICE.md) |
| Script para levantar todo | [`world/run_world.sh`](../../world/run_world.sh) |
| Escenarios del comercio | [`demo_world/scenario.py`](../../world/ap2/code/samples/python/src/demo_world/scenario.py) + [`data/scenarios.json`](../../world/ap2/code/samples/python/src/demo_world/data/scenarios.json) |
| Catálogo público (`:8011`) | [`demo_world/public_catalog.py`](../../world/ap2/code/samples/python/src/demo_world/public_catalog.py) + [`data/catalog.json`](../../world/ap2/code/samples/python/src/demo_world/data/catalog.json) |
| Chequeo de tope con el SDK real de AP2 | [`demo_world/ap2_check.py`](../../world/ap2/code/samples/python/src/demo_world/ap2_check.py) |
| Benchmark | [`demo_world/benchmark.py`](../../world/ap2/code/samples/python/src/demo_world/benchmark.py) |
| Exportador de fixtures | [`demo_world/export_fixtures.py`](../../world/ap2/code/samples/python/src/demo_world/export_fixtures.py) → [`fixtures/`](../../fixtures/) |
| Tests | [`demo_world/demo_world_tests.py`](../../world/ap2/code/samples/python/src/demo_world/demo_world_tests.py) |
| Cambiar de escenario en caliente | [`demo_world/set_scenario.py`](../../world/ap2/code/samples/python/src/demo_world/set_scenario.py) |

Detalle completo en [`world/README.md`](../../world/README.md).

## Tus tareas

Lo pendiente según [`world/README.md`](../../world/README.md#pendiente), más el
apoyo a la integración:

1. **Correr el benchmark con LLM y guardar los resultados.**
   `--trials 10`, todos los escenarios, modelo barato (el default del sample).
   *Terminado:* hay un JSON en `world/ap2/results/` con 10 corridas por
   escenario, sin errores, y la tabla resumen está pegada en
   [DEMO.md](../DEMO.md) o en el estado de [PLAN.md](../PLAN.md#6-estado-por-dev).
   Ojo: `.gitignore` ignora `world/ap2/results/`; ver
   [decisiones abiertas](../PLAN.md#decisiones-abiertas).
2. **Probar la compra completa en la UI con el escenario B** (hasta el pago,
   OTP `123`).
   *Terminado:* se llega al recibo de pago comprando AX-URB-04, y está anotado
   qué pasos y mensajes ve el usuario (sirve para el guion).
3. **Ajustar la inyección si el modelo no la obedece lo suficiente.**
   Variantes en `scenarios.json` → `injections` (`rank_first`,
   `rank_first_system`).
   *Terminado:* en B, "elige objetivo" es alto con el modelo barato, y quedó
   anotado qué variante se usó. **No inventar números:** si el modelo resiste,
   se reporta tal cual.
4. **Apoyar a Dev 3 con la integración del proxy**: pasar el flujo por `:8010`
   (`MERCHANT_AGENT_URL`) y por `--merchant-url` en el benchmark.
   *Terminado:* el punto de control 3 (B de punta a punta, con y sin Guard).
5. **Mantener los fixtures al día**: si cambia algo en `scenarios.json` o
   `catalog.json`, regenerar con `export_fixtures` y avisar.

## De qué dependés y quién depende de vos

- **Dependés de:** una `GOOGLE_API_KEY` en `.env` (o Vertex AI). Del Guard
  (Dev 2) para la columna "éxito con Guard".
- **Dependen de vos:** Dev 2 de `fixtures/`; Dev 3 del flujo levantado, del
  formato de los resultados del benchmark (para el gráfico) y de la URL del
  comercio. Si cambiás el formato de `fixtures/` o de `results/`, avisá.

## Cómo probar tu parte sin depender de los otros

```sh
# tests, sin red ni LLM
cd world/ap2 && GOOGLE_API_KEY=dummy uv run --no-sync --package ap2-samples \
  pytest code/samples/python/src/demo_world -q -o python_files='*_tests.py'

# benchmark sin servidores ni API key (decisores fijos)
cd world/ap2 && uv run --no-sync --package ap2-samples python -m demo_world.benchmark \
  --source direct --decider target --trials 2

# Guard falso para probar la columna "con Guard" (no commitear)
# un módulo con: def evaluate(intent, offers_seen, chosen_cart_id): return {"status": "bloqueado"}
```

## Primer prompt sugerido para Claude Code

```text
Soy Dev 1 del repo Pre-Sign Guard y soy dueño de world/. Leé CLAUDE.md,
world/README.md, world/NOTICE.md y docs/onboarding/dev1-world.md.

world/ ya está hecho. Quiero cerrar lo pendiente. Antes de tocar nada:
1. Corré los tests de demo_world y el benchmark con --source direct
   --decider target --trials 2, y decime si todo pasa.
2. Verificá que exista .env con GOOGLE_API_KEY (no me muestres la clave).
3. Armame un plan para: (a) correr el benchmark con LLM, --trials 10, contra
   los servidores levantados con world/run_world.sh; (b) qué mirar en la UI
   para probar la compra completa con el escenario B; (c) cómo decidir si hay
   que cambiar la variante de inyección.

No modifiques nada dentro de world/ap2/ fuera de demo_world/ sin avisarme,
y si lo hacemos, lo agregamos a world/NOTICE.md.
```

## Errores a evitar

- **Modificar AP2 o el sample "porque es más fácil".** Todo cambio fuera de
  `demo_world/` va a `world/NOTICE.md` y se marca con
  `HACKATHON (Pre-Sign Guard)`. El protocolo y el SDK no se tocan.
- **Ajustar la inyección hasta que el número "quede lindo"** sin anotarlo. La
  métrica tiene que ser defendible ante el jurado.
- **Correr el benchmark con un modelo caro** sin decirlo: el mensaje es que el
  Guard permite usar modelos baratos.
- **Cambiar `scenarios.json` sin regenerar los fixtures**: Dev 2 y Dev 3
  quedarían testeando contra datos viejos.
- **Olvidar que `run_world.sh` borra `.temp-db/` y `.logs/`** al arrancar
  (incluidas las claves de firma generadas).
- **Perder los resultados**: están ignorados por git hasta que se decida otra
  cosa.
