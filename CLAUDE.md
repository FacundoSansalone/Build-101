# Pre-Sign Guard — contexto para Claude Code

Proyecto de hackatón. Equipo de 3 desarrolladores, cada uno con Claude Code.

## Qué construimos

Un guard del lado del comprador que impide que un agente de compras sea
manipulado **dentro de los límites de su mandato AP2**. Actúa entre la respuesta
del comercio (A2A) y la firma del usuario. Principio: el agente decide solo con
datos estructurados y verificados, nunca con texto libre del vendedor.

AP2 ya frena lo que supera el tope firmado. Nuestro valor está en lo que AP2 no
ve: un producto que cumple el tope pero no es lo pedido (ataque de inyección),
datos falsos del comercio y precios que no coinciden con su catálogo público.

## Estructura y dueños

| Carpeta | Dueño | Qué es |
|---|---|---|
| `world/` | Dev 1 | Flujo AP2 real (copia del sample oficial), comercio atacante, catálogo público, benchmark. Ver `world/README.md`. |
| `guard/` | Dev 2 | Núcleo del Guard: ficha del pedido, extractor estructurado, verificador, chequeos, veredicto, recibo. |
| `proxy/` | Dev 3 | Proxy A2A entre el Shopping Agent y el Merchant Agent que llama al Guard. |
| `dashboard/` | Dev 3 | Vista de la demo: flujo, veredicto, recibo, gráfico del benchmark. |
| `contracts/` | Los tres | Modelos de datos compartidos. **No se cambian sin acordarlo entre los tres.** |
| `fixtures/` | Generado | Escenarios exportados por `world/` para trabajar sin levantar agentes. |

Cada uno edita solo su carpeta. Merges a `main` chicos y frecuentes.

## Reglas

- **No modificar AP2.** El Guard funciona con AP2 tal cual. Los únicos cambios al
  sample están listados en `world/NOTICE.md`.
- **El Guard nunca altera objetos firmados** (checkout JWT, mandatos). Puede
  leerlos, bloquear, avisar o escalar, pero no reescribirlos.
- **Las decisiones críticas del Guard son código determinístico**, no un LLM.
  El LLM solo se usa para convertir el pedido en lenguaje natural en la ficha.
- **No prometemos "detectar inyecciones".** Prometemos que la decisión se toma
  con datos verificados. Las métricas son tasas de éxito del ataque antes y
  después, no "0%".
- Antes de una tarea grande, pedí un plan y revisalo.
- Si una tarea requiere cambiar un contrato, pará y avisá al equipo.

## Datos que produce `world/` (fuente de los contratos)

El Merchant Agent responde por A2A, en cada búsqueda:

- `ap2.cart` → `{cart_id, item_label, amount, currency}` (modelo `Cart` de AP2).
  Es lo único que ve el Shopping Agent; la inyección viaja en `item_label`.
- `demo.offer_details` → lista de `{cart_id, sku, title, brand, category, use,
  price, currency, stock, sizes, description}` tal como **declara** el comercio
  (puede mentir).
- `demo_scenario` → nombre del escenario activo.

Catálogo público (verdad): `GET http://localhost:8011/products` y
`/products/<sku>`. El pedido del usuario y sus requisitos están en
`fixtures/scenario_*.json` → `intent`.

Contrato **provisorio** del Guard que usa el benchmark (a confirmar en `contracts/`):

```python
def evaluate(intent: dict, offers_seen: dict, chosen_cart_id: str) -> dict:
    # offers_seen = {"carts": [...], "offer_details": [...]}
    # -> {"status": "aprobado" | "con_avisos" | "escalar" | "bloqueado", ...}
```

## Puertos

8000 Shopping Agent (ADK) · 8001 Merchant Agent · 8002 Credentials Provider ·
8003 Payment Processor · 8010 proxy del Guard (reservado) · 8011 catálogo público.

El Shopping Agent usa `MERCHANT_AGENT_URL` para hablar con el comercio. Para
pasar por el proxy: `MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent`.

## Comandos

```sh
bash world/run_world.sh B                       # levanta todo en el escenario B
cd world/ap2 && uv run --no-sync --package ap2-samples python -m demo_world.benchmark --trials 10
cd world/ap2 && uv run --no-sync --package ap2-samples python -m demo_world.export_fixtures
```
