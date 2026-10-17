# proxy/ (Dev 3)

Proxy A2A en el puerto **8010**, entre el Shopping Agent y el Merchant Agent
(8001). Reenvía todo al comercio, llama al Guard en `create_checkout` y aplica
el veredicto. **Nunca modifica el `checkout_jwt`, el `checkout_hash` ni los
mandatos**: los bytes del pedido se reenvían tal cual y el `ap2.checkout` de
la respuesta no se toca (hay tests que lo verifican byte a byte y que la firma
ES256 sigue valiendo). **No decide**: decide `guard/`; el proxy transporta,
registra, aplica el veredicto y bloquea lo que no pudo evaluar (fail-closed).

## Levantarlo

Desde la raíz del repo, con el entorno de `world/ap2`:

```sh
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m proxy   # Linux/macOS: .venv/bin/python
```

Al arrancar loguea a qué comercio apunta, si el Guard está activo y qué
evaluador usa, y crea (o carga) la clave del recibo. Si `guard/` no expone
`evaluar()`, usa `proxy/guard_stub.py` (WARNING `usando stub`). Si el
`evaluar()` cargado no acepta `checkout=`, lo avisa con otro WARNING y lo
llama sin ese argumento.

### Variables de entorno

| Variable | Default | Qué es |
|---|---|---|
| `PROXY_PORT` | `8010` | Puerto del proxy |
| `PROXY_HOST` | `127.0.0.1` | Interfaz. **Fuera de loopback no arranca** salvo `PROXY_ALLOW_REMOTE=1` |
| `PROXY_ALLOW_REMOTE` | `0` | `1` para escuchar en la red (los eventos llevan mandatos y checkouts completos) |
| `MERCHANT_UPSTREAM_URL` | `http://localhost:8001/a2a/merchant_agent` | El comercio real |
| `PROXY_PUBLIC_URL` | `http://localhost:8010/a2a/merchant_agent` | URL que va en la Agent Card que sirve el proxy (y ruta del JSON-RPC) |
| `CATALOGO_URL` | `http://localhost:8011` | Catálogo público (`GET /products`) |
| `GUARD_ACTIVO` | `true` | Estado inicial del interruptor |
| `GUARD_EVENTS_JSONL` | `.logs/guard_events.jsonl` | Copia de los eventos (relativo a la raíz del repo; se crea con modo 0600) |
| `GUARD_KEY_PATH` | `.temp-db/guard_signing_key.pem` | Clave del recibo (la maneja `guard.recibo`) |
| `PROXY_MAX_RESPUESTA_BYTES` | `5242880` | Tope por respuesta del comercio (5 MB) |

## Apuntar el flujo al proxy

- **Shopping Agent:** `MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent bash world/run_world.sh B`
- **Benchmark:** `--merchant-url http://localhost:8010/a2a/merchant_agent`

```sh
cd world/ap2
PYTHONUTF8=1 .venv/Scripts/python -m demo_world.benchmark --source a2a \
  --merchant-url http://localhost:8010/a2a/merchant_agent --decider target --trials 1
```

- **Prueba de punta a punta** (find_products + create_checkout reales, con el
  cliente A2A del sample; `create_checkout` usa el LLM del comercio). Verifica
  que el checkout que llega sea byte a byte el que recibió el proxy, que el
  `checkout_hash` sea el SHA-256 del JWT y, con `--merchant-key` (el PEM de
  `MERCHANT_SIGNING_KEY_PATH` del comercio), que la firma ES256 valga:

```sh
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m proxy.scripts.smoke_checkout \
  --escenario B --cart cart_4 --esperado failed       # bloqueado
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m proxy.scripts.smoke_checkout \
  --escenario C --cart cart_1 --aprobar --esperado completed --merchant-key <pem>
  # escalar → retenido → POST /guard/aprobaciones → reintento → pasa intacto
```

## Qué hace

| Llamada | Qué hace el proxy |
|---|---|
| `GET /a2a/merchant_agent/.well-known/agent-card.json` (o `agent.json`) | Pide la card al comercio y reescribe `url` y `additionalInterfaces[].url` con `PROXY_PUBLIC_URL`. Sin esto, el cliente del sample manda el JSON-RPC directo a `:8001`. Devuelve solo el JSON (`Content-Type: application/json`), **sin ningún header del comercio** (nunca `Set-Cookie`). Cualquier otra ruta bajo `/a2a/merchant_agent/` da 404 y no se pide al comercio. |
| `POST /a2a/merchant_agent` | Reenvía el JSON-RPC (bytes del pedido tal cual, con `X-A2A-Extensions`). Respuesta JSON (`message/send`, lo que usa el sample) o SSE (`message/stream`, `tasks/resubscribe`), detectada con `"text/event-stream" in content-type.lower()`. Del comercio al cliente solo pasa `X-A2A-Extensions`; el `Content-Type` se normaliza. |
| `find_products` (pedido con `catalog_search`) | Pasa sin cambios. **Solo de estas respuestas** salen las ofertas: carts, offer_details y escenario, guardados (copiados) por el `context_id` de la respuesta. Una búsqueda nueva del cliente en el mismo contexto las reemplaza; ninguna otra respuesta las toca. |
| `create_checkout` (pedido con `cart_id`) | Exige **un solo** `ap2.checkout`, en los artefactos de una tarea, con `cart_id` = el del pedido y `contextId` = el del pedido. Evalúa con las ofertas del contexto del pedido: `evaluar(ficha, OfertasVistas(...), cart_id_del_pedido, catalogo, politica=None, checkout_hash=..., checkout=CheckoutVisto.model_validate(ap2.checkout))`. |
| `tasks/get`, `tasks/resubscribe` | Con checkout: usa la decisión guardada por `(task_id, sha256(checkout_jwt), cart_id)`. Si la tarea fue bloqueada o retenida, **nunca** devuelve el checkout (aunque se apague el Guard). Si el checkout de la tarea cambió, se reevalúa. Si el proxy no evaluó esa tarea, bloquea. |
| Pedidos con mandatos (`ap2.mandates.*`, `checkout_mandate`) | Pasan sin tocar; el evento `reenviado` lleva `datos.mandatos` (los strings tal cual) para el dashboard. |

### Qué pasa según el veredicto

| Veredicto | Al agente | Eventos |
|---|---|---|
| `aprobado`, `con_avisos` | Tarea intacta + artefacto aparte `{"guard.veredicto": <Veredicto>}` | `veredicto`, `reenviado` |
| `escalar` | **Retenido**: la misma tarea (mismos ids) en `failed`, sin el `ap2.checkout`, con `guard.veredicto` y el texto "Pre-Sign Guard: escalar. <motivo> El checkout requiere aprobación humana…" | `veredicto`, `bloqueado` con `requiere_aprobacion: true` |
| `escalar` + aprobación humana vigente (mismo contexto, cart y monto) | Tarea intacta (checkout byte a byte el del comercio) + `guard.veredicto` | `veredicto`, `reenviado` con `aprobacion_humana: true` |
| `bloqueado` | Tarea `failed`, sin el checkout, con `guard.veredicto` y "Pre-Sign Guard: bloqueado. <motivo> No se presenta para firma." | `veredicto`, `bloqueado` |
| No se pudo evaluar | Tarea `failed` (la del comercio o una armada por el proxy) con un `guard.veredicto` `bloqueado` y un motivo fijo del proxy | `error`, `bloqueado` |

El texto para el agente es siempre la plantilla fija
`"Pre-Sign Guard: <status>. <motivo>"` (el motivo lo arma `guard/` sin texto
del comercio). Cualquier parte `guard.veredicto` que mande el comercio se
**elimina** antes (en toda respuesta, también con el Guard apagado): ese data
part solo lo pone el proxy, y el Shopping Agent lo lee para mostrar el motivo.

**No se puede evaluar → no llega a la firma** (fail-closed): más de un
`ap2.checkout`, checkout de otro cart o de otro contexto, checkout fuera de
los artefactos o en una respuesta que no es a `create_checkout`, respuesta
`message` con checkout, JSON inválido o con `NaN`/`Infinity`, tipo inesperado,
`artifact-update` dentro de una respuesta JSON, respuesta de más de 5 MB,
error de red, timeout o stream cortado, sin ofertas del contexto, o
`evaluar()` que lanza. Si no era un checkout: un error de red o una respuesta
demasiado grande dan 502 con error JSON-RPC.

**SSE:** cada evento con `ap2.checkout` se evalúa. Si pasa, se retiene hasta
el evento final del stream (así un segundo checkout nunca llega al cliente);
un segundo checkout, aunque tenga el mismo `artifactId`, bloquea y corta el
stream. Un stream que termina sin evento final después del checkout también
se bloquea.

De dónde salen las entradas de `evaluar()`:

- **Ficha:** `demo_world.scenario.intent()` (si no se puede importar,
  `fixtures/scenario_baseline.json` → `intent`).
- **Catálogo:** `GET CATALOGO_URL/products` → `sku -> producto` y
  `comercio_id = merchant.id` (si no responde, `fixtures/public_catalog.json`;
  el evento `veredicto` dice cuál se usó en `fuente`).

## API de control (para el dashboard en `:8020`)

Solo local: todas las rutas `/guard/*` exigen `Host` =
`localhost:<puerto>` o `127.0.0.1:<puerto>` (403 si no: protege del DNS
rebinding). Los POST exigen `Content-Type: application/json` (415) y, si
traen `Origin`, que sea `http://localhost:8020` o `http://127.0.0.1:8020`
(403). El reenvío del servidor del dashboard no manda `Origin` y usa
`Host: localhost:8010`, así que pasa. CORS habilitado para esos dos orígenes.

| Endpoint | Respuesta |
|---|---|
| `GET /guard/state` | `{"guard_activo": bool, "merchant_url": str, "evaluador": "guard"\|"stub"}` |
| `POST /guard/state` con `{"guard_activo": bool}` | Mismo formato. Emite `guard_cambiado`. 400 si el cuerpo no es eso. |
| `POST /guard/aprobaciones` con `{"contexto_id": str, "cart_id": str}` | Registra una **aprobación humana de un solo uso** para ese contexto y cart, atada al monto (centavos y moneda del checkout firmado) del último veredicto `escalar` de ese contexto/cart. 201 con la aprobación; 200 si ya había una pendiente igual; 409 si el último veredicto de ese contexto/cart no es `escalar`; 400 si el cuerpo no es exactamente eso. |
| `GET /guard/aprobaciones` | `{"pendientes": [...], "usadas": [...]}`; cada una con `id`, `contexto_id`, `cart_id`, `centavos`, `monto`, `moneda`, `creada`, `estado`, `usada`, `task_id`. |
| `GET /guard/events` | SSE. Cada mensaje: `event: evento` + `data: <Evento JSON>`. Al conectar reenvía los últimos eventos en memoria (hasta 200). `: ping` cada 15 s. Hasta 20 suscriptores (503 después). |
| `GET /guard/events/history?limit=200` | Lista JSON de `Evento` (los más viejos primero, máximo 200). |
| `GET /guard/public-key` | `{"kid": "...", "jwk": {...}}` si hay Guard real con `guard.recibo`; 404 si no. |

**Cómo se usa la aprobación:** el agente reintenta `create_checkout` (el
Shopping Agent se lo pide al usuario cuando ve `escalar`); el Guard vuelve a
evaluar. Solo si da otra vez `escalar` (nunca un `bloqueado`) y el monto del
nuevo checkout es el aprobado, el checkout pasa intacto y la aprobación queda
usada.

### Eventos (`contracts.Evento`)

Además van a `GUARD_EVENTS_JSONL`, una línea por evento. `escenario` es el
`demo_scenario` de la compra; `contexto_id`, el `context_id` de A2A.

| `tipo` | `datos` |
|---|---|
| `busqueda_enviada` | `{"catalog_search": str}`. Sale con el `contextId` del pedido o, si no lo traía, recién con el de la respuesta del comercio. |
| `ofertas_recibidas` | `{"carts": [...], "offer_details": [...]}` |
| `checkout_pedido` | `{"cart_id": str}` |
| `checkout_recibido` | `{"cart_id", "checkout_jwt", "checkout_hash", "jwt_header", "jwt_payload", "ap2_violaciones"}` (+ `cart_id_checkout` si el del checkout no es el del pedido). Header y payload decodificados solo para mostrar; `ap2_violaciones` = `demo_world.ap2_check.ap2_violations(cart)` |
| `veredicto` | `{"veredicto": <Veredicto JSON>, "fuente": "http"\|"fixture", "recibo_firmado": bool}` |
| `reenviado` | `{"metodo": "find_products"\|"create_checkout"\|"otro", "mandatos": {...}?, "aprobacion_humana": true?, "aprobacion_id": str?}` |
| `bloqueado` | `{"cart_id", "motivo", "status", "requiere_aprobacion": bool}` (`requiere_aprobacion: true` = retenido por `escalar`) |
| `guard_cambiado` | `{"guard_activo": bool}` |
| `error` | `{"mensaje"}` |

## Seguridad

- Los mandatos y el `checkout_jwt` completos se muestran en el dashboard (la
  demo lo exige). La mitigación: el proxy solo escucha en loopback, la API
  valida `Host`/`Origin`/`Content-Type` y el JSONL se crea con modo 0600.
- Ningún header del comercio llega al cliente salvo `X-A2A-Extensions`.
- JSON estricto (sin `NaN`/`Infinity`), lectura con tope de bytes y SSE leído
  línea a línea con el mismo tope.
- Los textos que el proxy manda al agente son plantillas fijas; los detalles
  de errores (que pueden traer texto del comercio) solo van al evento `error`.

## Código

| Archivo | Qué es |
|---|---|
| `app.py` | `create_app(config, transport=None, evaluador=None)`: rutas, reenvío, decisión sobre el checkout (JSON y SSE), caché de decisiones, API de control |
| `a2a.py` | Helpers puros sobre el JSON de A2A (pedido, ofertas, checkouts, `guard.veredicto` ajeno, tareas bloqueadas, monto firmado, lectura acotada y SSE) |
| `aprobaciones.py` | Registro de aprobaciones humanas (un solo uso, atadas a contexto, cart y monto) |
| `seguridad.py` | Middleware de la API de control (`Host`, `Origin`, `Content-Type`) |
| `evaluador.py` | `cargar_evaluador()` (Guard real o stub, y si acepta `checkout=`), clave y firma del recibo con `guard.recibo` |
| `guard_stub.py` | Stub con la firma de `contracts.Evaluador`: bloquea si el elegido es el último cart |
| `eventos.py` | Bus de eventos: memoria (200), suscriptores SSE (hasta 20) y JSONL |
| `fuentes.py` | Intent, catálogo público (con su fuente) y `ap2_check` con respaldo en `fixtures/` |
| `config.py` | Variables de entorno y chequeo de loopback |
| `scripts/smoke_checkout.py` | Prueba real de punta a punta con el cliente A2A del sample |

## Tests

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest proxy contracts
```

Usan un comercio falso (`tests/fake_world.py`) que responde con los carts de
`fixtures/scenario_*.json` y un `ap2.checkout` con un JWT ES256 real; el
upstream se inyecta con `httpx.ASGITransport`, sin levantar nada. El comercio
falso se puede hacer portar mal (`manipular`, `manipular_sse`, headers) para
probar cada caso fail-closed. `tests/test_guard_real.py` corre el flujo con el
`guard.evaluar()` real.
