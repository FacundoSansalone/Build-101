# KaiAI

> AP2 frena al agente cuando se pasa de los límites. Nosotros lo frenamos cuando
> lo manipulan sin pasarse.

Un guard del lado del comprador para agentes que pagan con
[AP2](https://github.com/google-agentic-commerce/AP2). Se ubica entre la
respuesta del comercio y la firma del usuario, y garantiza que la decisión de
compra se tome con datos estructurados y verificados, no con el texto del
vendedor.

## Qué hace

Un solo flujo con dos capas, del lado del comprador
([docs/FLUJO.md](docs/FLUJO.md)):

1. **Seguridad (`filtro/`).** Revisa cada respuesta del proveedor antes que
   nada. Si hay malicia, corta la conversación y no llega a ningún agente.
2. **Intención (`intencion/`).** Nuestro agente pregunta al usuario para qué
   quiere la compra y observa la negociación. Si una oferta no cumple el pedido
   literal pero sí la necesidad, consulta al usuario. Si acepta, los parámetros
   nuevos van al agente del cliente.
3. **Guard (`guard/`).** Antes de la firma, verifica la compra contra los
   parámetros que autorizó el usuario y contra fuentes que el vendedor no
   controla.

**La garantía es el Guard antes de la firma; el filtro es defensa en
profundidad.** Los modelos entienden y proponen, el usuario autoriza, el código
verifica.

El proxy (`proxy/`) pone las capas en el camino entre el agente del cliente y
el comercio. Para integradores externos hay un gateway en el mismo proceso: la
API `/v1`, con claves, varios comercios y webhooks. Guía para integrar:
[docs/INTEGRACION.md](docs/INTEGRACION.md).

## Estado

| Parte | Dueño | Estado |
|---|---|---|
| `world/`: flujo AP2 real, comercio atacante, catálogo público, benchmark | Dev 1 | Listo; benchmark con LLM medido (`results/`); escenarios de computadoras |
| `contracts/`: modelos compartidos | Los tres | Acordados (ver `CLAUDE.md` → Decisiones) |
| `filtro/`: seguridad del agente | Dev 1 | Listo: heurísticas y modelo, falla cerrado |
| `intencion/`: contexto del pedido y oportunidades | Dev 2 | Listo, con tests |
| `guard/`: núcleo del Guard | Dev 2 | Listo: `evaluar()`, verificador, chequeos, recibo firmado |
| `proxy/`: proxy A2A y gateway `/v1` | Dev 3 | Listo en `:8010`; gateway en `:8030` (opcional, con claves) |
| `dashboard/`: panel y `/demo` | Dev 3 | Listo en `:8020`, con modo offline |
| `decisiones/`, `taxonomia/`, `fabricante/` | Compartido | Listo: juicios tipados, taxonomía de Google y ficha del fabricante |

Benchmark (10 corridas, modelo barato, a través del proxy): el éxito del
ataque con AP2 como única defensa pasa **de 100% a 0% en esta medición** con el
Guard en B, B2 y C, sin falsos positivos en `baseline`. Detalle en
[`docs/DEMO.md`](docs/DEMO.md#resultados-medidos-2026-10-17).

## Empezar

### La demo completa, en una sola orden

```sh
cp .env.example .env                       # poné tu GOOGLE_API_KEY
bash deploy/start_all.sh                   # comercio general → http://localhost:8020/demo
```

Levanta los cinco procesos de `world/`, el proxy (`:8010`) y el dashboard
(`:8020`). Funciona en Linux, macOS y Windows con Git Bash. Sin argumento, el
comercio general responde a cualquier pedido, con el comportamiento que elige el
selector de `/demo`. Los escenarios fijos (zapatillas, notebooks, auriculares,
computadoras) quedan para el benchmark y se ven con `DEMO_ESCENARIOS_FIJOS=1`. Variables,
despliegue en Tenki y gateway: [deploy/README.md](deploy/README.md). Guion de la
demo: [docs/DEMO.md](docs/DEMO.md).

### A mano

En Linux/macOS el Python es `world/ap2/.venv/bin/python`.

```sh
# Terminal 1: el mundo AP2, pasando por el proxy del Guard
MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent bash world/run_world.sh B
# Terminal 2: proxy del Guard (:8010), desde la raíz (no lee .env solo)
GUARD_FILTRO=filtro:filtrar FILTRO_SOLO_HEURISTICAS=1 PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m proxy
# Terminal 3: dashboard (:8020)
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m dashboard
```

Más detalle en [`world/README.md`](world/README.md). Contexto para Claude Code
en [`CLAUDE.md`](CLAUDE.md).

## Tests

Desde la raíz del repo:

```sh
# Python: contracts, decisiones, filtro, guard, intencion, proxy, dashboard, taxonomia y fabricante
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest      # Linux/macOS: world/ap2/.venv/bin/python

# JS del dashboard (lógica de la vista, sin DOM)
node --test "dashboard/tests/js/*.test.mjs"

# world/ (escenarios y exportación de fixtures)
cd world/ap2 && GOOGLE_API_KEY=dummy uv run --no-sync --package ap2-samples pytest code/samples/python/src/demo_world -q -o python_files='*_tests.py'
```

Medido el 2026-10-18 en Windows: Python con 2585 pasan y 5 se saltean, en unos
45 s (antes la suite se colgaba en Windows); JS con 127; `world/` con 44.

## Para el equipo

**Etapa 2 (desde el 2026-10-18):** leé primero
[`docs/FLUJO.md`](docs/FLUJO.md), que define el flujo único con seguridad e
intención, y tu tarea en
[`docs/PLAN.md`](docs/PLAN.md#7-etapa-2-flujo-único).

1. Leé [`docs/CONTEXTO.md`](docs/CONTEXTO.md) y
   [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md).
2. Mirá cómo trabajamos en [`docs/PLAN.md`](docs/PLAN.md) (fases, puntos de
   control, estado por dev).
3. Abrí tu guía de onboarding y pasale el primer prompt a tu Claude Code:
   - Dev 1, `world/`: [`docs/onboarding/dev1-world.md`](docs/onboarding/dev1-world.md)
   - Dev 2, `guard/`: [`docs/onboarding/dev2-guard.md`](docs/onboarding/dev2-guard.md)
   - Dev 3, `proxy/` y `dashboard/`: [`docs/onboarding/dev3-proxy-dashboard.md`](docs/onboarding/dev3-proxy-dashboard.md)
4. Paso 0, los tres juntos: acordar los contratos a partir de
   [`docs/CONTRATOS_PROPUESTOS.md`](docs/CONTRATOS_PROPUESTOS.md).

Demo y pitch: [`docs/DEMO.md`](docs/DEMO.md). Objeciones del jurado:
[`docs/PREGUNTAS_DEL_JURADO.md`](docs/PREGUNTAS_DEL_JURADO.md). Integradores
externos (gateway `/v1`, claves, webhooks): [`docs/INTEGRACION.md`](docs/INTEGRACION.md).

## Referencias

- *Whispers of Wealth: A Systematic Red-Teaming Study of AP2* — arXiv 2601.22569
- *Beyond the Mandate: A Systematic Security Analysis of AP2* — arXiv 2608.23858
- *Protocol-Level Attacks on Agentic Commerce Platforms (PCAT, AIP-Bench)* — arXiv 2607.21824
- AP2, Security and Privacy Considerations — ap2-protocol.org
