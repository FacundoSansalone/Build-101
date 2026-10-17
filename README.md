# Pre-Sign Guard

> AP2 frena al agente cuando se pasa de los límites. Nosotros lo frenamos cuando
> lo manipulan sin pasarse.

Un guard del lado del comprador para agentes que pagan con
[AP2](https://github.com/google-agentic-commerce/AP2). Se ubica entre la
respuesta del comercio y la firma del usuario, y garantiza que la decisión de
compra se tome con datos estructurados y verificados, no con el texto del
vendedor.

## Estado

| Parte | Dueño | Estado |
|---|---|---|
| `world/` — flujo AP2 real, comercio atacante, catálogo público, benchmark | Dev 1 | Listo; benchmark con LLM medido (`results/`) |
| `contracts/` — modelos compartidos | Los tres | Acordados (ver `CLAUDE.md` → Decisiones) |
| `guard/` — núcleo del Guard | Dev 2 | Listo: `evaluar()`, verificador, chequeos, recibo firmado |
| `proxy/` — proxy A2A | Dev 3 | Listo en `:8010`, con interruptor y eventos |
| `dashboard/` — vista de la demo | Dev 3 | Listo en `:8020`, con modo offline |

Benchmark (10 corridas, modelo barato, a través del proxy): el éxito del
ataque con AP2 como única defensa pasa **de 100% a 0%** con el Guard en B, B2
y C, sin falsos positivos en `baseline`. Detalle en
[`docs/DEMO.md`](docs/DEMO.md#resultados-medidos-2026-10-17).

## Empezar

```sh
cp .env.example .env           # poné tu GOOGLE_API_KEY
# Terminal 1: el flujo AP2 en el escenario B, pasando por el proxy del Guard
MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent bash world/run_world.sh B
# Terminal 2: proxy del Guard (:8010) — desde la raíz del repo
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m proxy
# Terminal 3: dashboard (:8020)
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m dashboard
```

En Linux/macOS el Python es `world/ap2/.venv/bin/python`. Tests de
`contracts/`, `guard/`, `proxy/` y `dashboard/`: `python -m pytest` desde la
raíz con ese mismo Python.

Más detalle en [`world/README.md`](world/README.md). Contexto para Claude Code
en [`CLAUDE.md`](CLAUDE.md).

## Para el equipo

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
[`docs/PREGUNTAS_DEL_JURADO.md`](docs/PREGUNTAS_DEL_JURADO.md).

## Referencias

- *Whispers of Wealth: A Systematic Red-Teaming Study of AP2* — arXiv 2601.22569
- *Beyond the Mandate: A Systematic Security Analysis of AP2* — arXiv 2608.23858
- *Protocol-Level Attacks on Agentic Commerce Platforms (PCAT, AIP-Bench)* — arXiv 2607.21824
- AP2, Security and Privacy Considerations — ap2-protocol.org
