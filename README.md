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
| `world/` — flujo AP2 real, comercio atacante, catálogo público, benchmark | Dev 1 | Listo para correr con API key |
| `contracts/` — modelos compartidos | Los tres | Por acordar |
| `guard/` — núcleo del Guard | Dev 2 | Por empezar |
| `proxy/` — proxy A2A | Dev 3 | Por empezar |
| `dashboard/` — vista de la demo | Dev 3 | Por empezar |

## Empezar

```sh
cp .env.example .env           # poné tu GOOGLE_API_KEY
bash world/run_world.sh B      # levanta el flujo AP2 en el escenario B
```

Más detalle en [`world/README.md`](world/README.md). Contexto para Claude Code
en [`CLAUDE.md`](CLAUDE.md).

## Referencias

- *Whispers of Wealth: A Systematic Red-Teaming Study of AP2* — arXiv 2601.22569
- *Beyond the Mandate: A Systematic Security Analysis of AP2* — arXiv 2608.23858
- *Protocol-Level Attacks on Agentic Commerce Platforms (PCAT, AIP-Bench)* — arXiv 2607.21824
- AP2, Security and Privacy Considerations — ap2-protocol.org
