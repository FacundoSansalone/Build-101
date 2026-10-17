# proxy/ (Dev 3)

Proxy A2A en el puerto **8010**, entre el Shopping Agent y el Merchant Agent
(8001). Reenvía todo al comercio, llama al Guard en `create_checkout` y aplica
el veredicto. **Nunca modifica el `checkout_jwt`, el `checkout_hash` ni los
mandatos**: los bytes del pedido se reenvían tal cual y el `ap2.checkout` de
la respuesta no se toca (hay tests que lo verifican byte a byte y que la firma
ES256 sigue valiendo).

## Levantarlo

Desde la raíz del repo, con el entorno de `world/ap2`:

```sh
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m proxy   # Linux/macOS: .venv/bin/python
```

Al arrancar loguea a qué comercio apunta, si el Guard está activo y qué
evaluador usa. Si `guard/` todavía no expone `evaluar()`, usa
`proxy/guard_stub.py` y lo avisa con un WARNING (`usando stub`).

### Variables de entorno

| Variable | Default | Qué es |
|---|---|---|
| `PROXY_PORT` | `8010` | Puerto del proxy |
| `PROXY_HOST` | `127.0.0.1` | Interfaz |
| `MERCHANT_UPSTREAM_URL` | `http://localhost:8001/a2a/merchant_agent` | El comercio real |
| `PROXY_PUBLIC_URL` | `http://localhost:8010/a2a/merchant_agent` | URL que va en la Agent Card que sirve el proxy (y ruta del JSON-RPC) |
| `CATALOGO_URL` | `http://localhost:8011` | Catálogo público (`GET /products`) |
| `GUARD_ACTIVO` | `true` | Estado inicial del interruptor |
| `GUARD_EVENTS_JSONL` | `.logs/guard_events.jsonl` | Copia de los eventos (relativo a la raíz del repo) |
| `GUARD_KEY_PATH` | `.temp-db/guard_signing_key.pem` | Clave del recibo (la maneja `guard.recibo`) |

## Apuntar el flujo al proxy

- **Shopping Agent:** `MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent bash world/run_world.sh B`
- **Benchmark:** `--merchant-url http://localhost:8010/a2a/merchant_agent`

```sh
cd world/ap2
PYTHONUTF8=1 .venv/Scripts/python -m demo_world.benchmark --source a2a \
  --merchant-url http://localhost:8010/a2a/merchant_agent --decider target --trials 1
```

- **Prueba de punta a punta** (find_products + create_checkout reales, con el
  cliente A2A del sample; `create_checkout` usa el LLM del comercio):

```sh
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m proxy.scripts.smoke_checkout \
  --escenario B --cart cart_4 --esperado failed      # con Guard: bloqueado
```

## Qué hace

| Llamada | Qué hace el proxy |
|---|---|
| `GET /a2a/merchant_agent/.well-known/agent-card.json` | Pide la card al comercio y reescribe `url` y `additionalInterfaces[].url` con `PROXY_PUBLIC_URL`. Sin esto, el cliente del sample (`A2ACardResolver` + `ClientFactory`) manda el JSON-RPC directo a `:8001` y se saltea el proxy. |
| `POST /a2a/merchant_agent` | Reenvía el JSON-RPC (bytes y headers, incluido `X-A2A-Extensions`). Soporta respuesta JSON (`message/send`, lo que usa el sample porque la card no declara streaming) y SSE (`message/stream`). |
| `find_products` (pedido con `catalog_search`) | Pasa sin cambios (incluido `demo_scenario`). Guarda `carts`, `offer_details` y escenario por `context_id`. |
| `create_checkout` (respuesta con `ap2.checkout`) | Con Guard: `evaluar(Ficha.desde_intent(intent), OfertasVistas(...), cart_id, catalogo, politica=None, checkout_hash=...)`. `aprobado`/`con_avisos`/`escalar`: respuesta intacta + un artefacto aparte con `{"guard.veredicto": <Veredicto>}`. `bloqueado`: la misma tarea (mismos ids) en `failed`, con el motivo en el mensaje y **sin el `ap2.checkout`**: no se presenta para firma. Sin Guard: passthrough puro. |
| Pedidos con mandatos (`ap2.mandates.*`, `checkout_mandate`) | Pasan sin tocar; el evento `reenviado` lleva `datos.mandatos` (los strings tal cual) para el dashboard. |

De dónde salen las entradas de `evaluar()`:

- **Ficha:** `demo_world.scenario.intent()` (si no se puede importar,
  `fixtures/scenario_baseline.json` → `intent`).
- **Catálogo:** `GET CATALOGO_URL/products` → `sku -> producto` y
  `comercio_id = merchant.id` (si no responde, `fixtures/public_catalog.json`).

**Si no se puede evaluar** (no pasó el `find_products` por el proxy, o
`evaluar()` lanza una excepción) el checkout **no** se presenta para firma:
la tarea vuelve `failed` y se emiten `error` y `bloqueado`. El Guard nunca
aprueba en silencio lo que no pudo verificar.

## API de control (para el dashboard en `:8020`)

CORS habilitado para `http://localhost:8020` y `http://127.0.0.1:8020`.

| Endpoint | Respuesta |
|---|---|
| `GET /guard/state` | `{"guard_activo": bool, "merchant_url": str, "evaluador": "guard"\|"stub"}` |
| `POST /guard/state` con `{"guard_activo": bool}` | Mismo formato. Emite `guard_cambiado` con `datos={"guard_activo": bool}`. 400 si el cuerpo no es eso. |
| `GET /guard/events` | SSE. Cada mensaje: `event: evento` + `data: <Evento JSON>`. Al conectar reenvía los últimos eventos en memoria (hasta 200). Manda `: ping` cada 15 s. |
| `GET /guard/events/history?limit=200` | Lista JSON de `Evento` (los más viejos primero, máximo 200). |
| `GET /guard/public-key` | `{"kid": "...", "jwk": {...}}` si hay Guard real con `guard.recibo`; 404 si no. |

### Eventos (`contracts.Evento`)

Además van a `GUARD_EVENTS_JSONL`, una línea por evento. `escenario` es el
`demo_scenario` de la compra; `contexto_id`, el `context_id` de A2A.

| `tipo` | `datos` |
|---|---|
| `busqueda_enviada` | `{"catalog_search": str}` |
| `ofertas_recibidas` | `{"carts": [...], "offer_details": [...]}` |
| `checkout_pedido` | `{"cart_id": str}` |
| `checkout_recibido` | `{"cart_id", "checkout_jwt", "checkout_hash", "jwt_header", "jwt_payload", "ap2_violaciones"}` (header y payload decodificados solo para mostrar; `ap2_violaciones` = `demo_world.ap2_check.ap2_violations(cart)`, `[]` es AP2 OK, `null` si no se pudo correr) |
| `veredicto` | `{"veredicto": <Veredicto JSON>}` |
| `reenviado` | `{"metodo": "find_products"\|"create_checkout"\|"otro", "mandatos": {...}?}` |
| `bloqueado` | `{"cart_id", "motivo"}` |
| `guard_cambiado` | `{"guard_activo": bool}` |
| `error` | `{"mensaje"}` |

## Código

| Archivo | Qué es |
|---|---|
| `app.py` | `create_app(config, transport=None, evaluador=None)`: rutas, reenvío, aplicación del veredicto, API de control |
| `a2a.py` | Helpers puros sobre el JSON de A2A (clasificar pedidos, acumular respuestas, artefacto del veredicto, tarea bloqueada, card, SSE) |
| `evaluador.py` | `cargar_evaluador()` (Guard real o stub), firma del recibo y clave pública con `guard.recibo` |
| `guard_stub.py` | Stub con la firma de `contracts.Evaluador`: bloquea si el elegido es el último cart |
| `eventos.py` | Bus de eventos: memoria (200), suscriptores SSE y JSONL |
| `fuentes.py` | Intent, catálogo público y `ap2_check` con respaldo en `fixtures/` |
| `config.py` | Variables de entorno |
| `scripts/smoke_checkout.py` | Prueba real de punta a punta con el cliente A2A del sample |

## Tests

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest proxy contracts
```

Usan un comercio falso (`tests/fake_world.py`) que responde con los carts de
`fixtures/scenario_*.json` y un `ap2.checkout` con un JWT ES256 real; el
upstream se inyecta con `httpx.ASGITransport`, sin levantar nada.
