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
evaluador usa, crea (o carga) la clave del recibo y escribe el token de la
API de control en `.temp-db/guard_api_token`.

**El evaluador falla cerrado.** Si no puede importar el `guard.evaluar()`
real, o el que importa no acepta `checkout=` (evaluaría sin atar el checkout
firmado al cart), **el proxy no arranca** y sale con el motivo. Para probar
sin el Guard real hay que pedirlo explícitamente con `PROXY_ALLOW_STUB=1`: usa
`proxy/guard_stub.py`, lo avisa en el log y lo marca en `GET /guard/state` y en
cada evento `veredicto` (`"evaluador": "stub"`; el dashboard muestra una
franja). Un `evaluar()` sin `checkout=` no se usa nunca, ni con el permiso.

### Variables de entorno

| Variable | Default | Qué es |
|---|---|---|
| `PROXY_PORT` | `8010` | Puerto del proxy |
| `PROXY_HOST` | `127.0.0.1` | Interfaz. **Fuera de loopback no arranca** salvo `PROXY_ALLOW_REMOTE=1` |
| `PROXY_ALLOW_REMOTE` | `0` | `1` para escuchar en la red (los eventos llevan mandatos y checkouts completos) |
| `MERCHANT_UPSTREAM_URL` | `http://127.0.0.1:8001/a2a/merchant_agent` | El comercio real (escucha en 127.0.0.1) |
| `PROXY_PUBLIC_URL` | `http://localhost:8010/a2a/merchant_agent` | URL que va en la Agent Card que sirve el proxy (y ruta del JSON-RPC). Es la que ve el cliente A2A: queda en `localhost`, igual que `MERCHANT_AGENT_URL` |
| `CATALOGO_URL` | `http://127.0.0.1:8011` | Catálogo público (`GET /products`) |
| `GUARD_ACTIVO` | `true` | Estado inicial del interruptor |
| `GUARD_EVENTS_JSONL` | `.logs/guard_events.jsonl` | Copia de los eventos (relativo a la raíz del repo; se crea con modo 0600) |
| `GUARD_KEY_PATH` | `.temp-db/guard_signing_key.pem` | Clave del recibo (la maneja `guard.recibo`) |
| `GUARD_API_TOKEN_PATH` | `.temp-db/guard_api_token` | Token de la API de control, nuevo en cada arranque (modo 0600 donde aplica). Lo lee el servidor del dashboard |
| `PROXY_ALLOW_STUB` | `0` | `1` para arrancar con el stub si no hay `guard.evaluar()` real (solo pruebas) |
| `PROXY_MAX_RESPUESTA_BYTES` | `5242880` | Tope por respuesta del comercio (5 MB) |
| `SUPABASE_URL` | — | Proyecto de Supabase (`https://<ref>.supabase.co`). Con la clave, activa el registro en Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | — | Clave de servicio de Supabase (`sb_secret_…` o la `service_role` legacy). También se acepta como `SUPABASE_SECRET_KEY`, el nombre que usa hoy el dashboard. **Secreta**: va en el entorno o en un `.env` ignorado por git, nunca en el repo. El proxy no lee `.env` solo: `set -a; source .env; set +a` antes de `python -m proxy` |

Los servicios locales van por `127.0.0.1` y no por `localhost`: en Windows
`localhost` prueba primero `::1`, donde el comercio y el catálogo no
escuchan, y cada intento fallido suma ~2 s.

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
  # escalar → retenido → POST /guard/aprobaciones (con el token de
  # .temp-db/guard_api_token; otro archivo con --token-file) → reintento → pasa intacto
```

## Qué hace

| Llamada | Qué hace el proxy |
|---|---|
| `GET /a2a/merchant_agent/.well-known/agent-card.json` (o `agent.json`) | Pide la card al comercio y reescribe `url` y `additionalInterfaces[].url` con `PROXY_PUBLIC_URL`. Sin esto, el cliente del sample manda el JSON-RPC directo a `:8001`. Devuelve solo el JSON (`Content-Type: application/json`), **sin ningún header del comercio** (nunca `Set-Cookie`). Cualquier otra ruta bajo `/a2a/merchant_agent/` da 404 y no se pide al comercio. |
| `POST /a2a/merchant_agent` | Reenvía el JSON-RPC (bytes del pedido tal cual, con `X-A2A-Extensions`). Respuesta JSON (`message/send`, lo que usa el sample) o SSE (`message/stream`, `tasks/resubscribe`), detectada con `"text/event-stream" in content-type.lower()`. Del comercio al cliente solo pasa `X-A2A-Extensions`; el `Content-Type` se normaliza. |
| `find_products` (pedido con `catalog_search`) | Pasa sin cambios. **Solo de estas respuestas** salen las ofertas: carts, offer_details y escenario, guardados (copiados) por el `context_id` de la respuesta. Una búsqueda nueva del cliente en el mismo contexto las reemplaza; ninguna otra respuesta las toca. |
| `create_checkout` (pedido con `cart_id`) | Exige **un solo** `ap2.checkout`, en los artefactos de una tarea, con `cart_id` = el del pedido y `contextId` = el del pedido. Evalúa con las ofertas del contexto del pedido: `evaluar(ficha, OfertasVistas(...), cart_id_del_pedido, catalogo, politica=None, checkout_hash=..., checkout=CheckoutVisto.model_validate(ap2.checkout))` (siempre con `checkout=`). |
| `tasks/get`, `tasks/resubscribe` | Con checkout: usa la decisión guardada por `(task_id, sha256 del data part ap2.checkout completo en JSON canónico, cart_id)`. Si cambia cualquier campo del data part (el JWT, `checkout_hash`, `amount`, `amount_cents`, `currency`, `item_label`…), se reevalúa; el orden de las claves no cuenta. Si la tarea fue bloqueada o retenida, **nunca** devuelve el checkout (aunque se apague el Guard). Si el proxy no evaluó esa tarea, bloquea. |
| Pedidos con mandatos (`ap2.mandates.*`, `checkout_mandate`) | Pasan sin tocar; el evento `reenviado` lleva `datos.mandatos` (los strings tal cual) para el dashboard. |

### Qué pasa según el veredicto

| Veredicto | Al agente | Eventos |
|---|---|---|
| `aprobado`, `con_avisos` | Tarea intacta + artefacto aparte `{"guard.veredicto": <versión para el agente>}` | `veredicto`, `reenviado` |
| `escalar` | **Retenido**: la misma tarea (mismos ids) en `failed`, sin el `ap2.checkout`, con `guard.veredicto` y el texto "Pre-Sign Guard: escalar. <motivo> El checkout requiere aprobación humana…" | `veredicto`, `bloqueado` con `requiere_aprobacion: true` |
| `escalar` + aprobación humana vigente (mismo contexto, cart y monto) | Tarea intacta (checkout byte a byte el del comercio) + `guard.veredicto` | `veredicto`, `reenviado` con `aprobacion_humana: true` |
| `bloqueado` | Tarea `failed`, sin el checkout, con `guard.veredicto` y "Pre-Sign Guard: bloqueado. <motivo> No se presenta para firma." | `veredicto`, `bloqueado` |
| No se pudo evaluar | Tarea `failed` (la del comercio o una armada por el proxy) con un `guard.veredicto` `bloqueado` y un motivo fijo del proxy | `error`, `bloqueado` |

El texto para el agente es siempre la plantilla fija
`"Pre-Sign Guard: <status>. <motivo>"` (el motivo lo arma `guard/` sin texto
del comercio). Cualquier parte `guard.veredicto` que mande el comercio se
**elimina** antes (en toda respuesta, también con el Guard apagado): ese data
part solo lo pone el proxy, y el Shopping Agent lo lee para mostrar el motivo.

**Al agente va una versión reducida del veredicto**, sin texto del comercio:
`{"status", "motivo", "cart_id_elegido", "chequeos": [{"tipo", "resultado"}]}`.
`cart_id_elegido` va solo si es un identificador seguro
(`[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}`; si no, `null`). No lleva `observado`,
`esperado`, `detalle`, ranking ni recibo (con `verificaciones[].declarado`).
El veredicto completo va al dashboard en el evento `veredicto`. El Shopping
Agent solo lee `status` y `motivo`.

**No se puede evaluar → no llega a la firma** (fail-closed): más de un
`ap2.checkout`, checkout de otro cart o de otro contexto, checkout fuera de
los artefactos o en una respuesta que no es a `create_checkout`, respuesta
`message` con checkout, JSON inválido o con `NaN`/`Infinity` o números que
desbordan (`1e999`, aunque esté en una parte ajena), tipo inesperado,
`artifact-update` dentro de una respuesta JSON, respuesta de más de 5 MB,
error de red, timeout o stream cortado, sin ofertas del contexto, o
`evaluar()` que lanza. Si no era un checkout: un error de red o una respuesta
demasiado grande dan 502 con error JSON-RPC, y una respuesta ilegible pasa
tal cual con un evento `error`, salvo que traiga un `guard.veredicto` (no se
puede sacar de algo ilegible): ahí 502 (en SSE, ese evento no se reenvía).

**SSE:** cada evento con `ap2.checkout` se evalúa. Si pasa, se retiene hasta
el evento final del stream (así un segundo checkout nunca llega al cliente);
un segundo checkout, aunque tenga el mismo `artifactId`, bloquea y corta el
stream. Un stream que termina sin evento final después del checkout también
se bloquea. Al reescribir eventos, `id:` y `event:` del comercio se sanean
(sin CR, LF, `\x0b`, `\x0c`, `\x1c`-`\x1e`, U+0085, U+2028 ni U+2029: el
cliente A2A corta líneas con `str.splitlines()`) y esos caracteres van
escapados (`\uXXXX`) en las líneas `data:`. Así el cliente ve exactamente los
eventos que evaluó el proxy. El SSE de `/guard/events` hace lo mismo.

De dónde salen las entradas de `evaluar()`:

- **Ficha:** `demo_world.scenario.intent()` (si no se puede importar,
  `fixtures/scenario_baseline.json` → `intent`).
- **Catálogo:** `GET CATALOGO_URL/products` → `sku -> producto` y
  `comercio_id = merchant.id` (si no responde, `fixtures/public_catalog.json`;
  el evento `veredicto` dice cuál se usó en `fuente`).

## API de control (para el dashboard en `:8020`)

Solo local: todas las rutas `/guard/*` exigen `Host` =
`localhost:<puerto>` o `127.0.0.1:<puerto>` (403 si no: protege del DNS
rebinding). Los POST exigen `Content-Type: application/json` (415), si
traen `Origin` que sea `http://localhost:8020` o `http://127.0.0.1:8020` (403)
y **el header `X-Guard-Token`** con el token de este arranque (403
`token_invalido`). El token es aleatorio, cambia en cada arranque y se
escribe en `.temp-db/guard_api_token` (modo 0600 donde aplica): un proceso
local o el agente con una herramienta HTTP no pueden apagar el Guard ni
aprobar un checkout. El servidor del dashboard lo lee y lo agrega en su
reenvío same-origin (sin `Origin`, con `Host: 127.0.0.1:8010`); el navegador
nunca lo ve. Los GET (estado, aprobaciones, eventos, clave pública) no piden
token. CORS habilitado para esos dos orígenes.

| Endpoint | Respuesta |
|---|---|
| `GET /guard/state` | `{"guard_activo": bool, "merchant_url": str, "evaluador": "guard"\|"stub"}` |
| `POST /guard/state` con `{"guard_activo": bool}` y `X-Guard-Token` | Mismo formato. Emite `guard_cambiado`. 400 si el cuerpo no es eso. |
| `POST /guard/aprobaciones` con `{"contexto_id": str, "cart_id": str}` y `X-Guard-Token` | Registra una **aprobación humana de un solo uso** para ese contexto y cart, atada al **total firmado exacto** del último veredicto `escalar` de ese contexto/cart: el `totals[type=total].amount` del payload del `checkout_jwt` (un entero, en centavos) y su `currency`, sin fallbacks a `line_items` ni al data part. 201 con la aprobación; 200 si ya había una pendiente igual; 409 si el último veredicto no es `escalar` o si el checkout escalado no tiene un total firmado entero exacto (falta, es float, hay dos, claves duplicadas…): queda retenido; 400 si el cuerpo no es exactamente eso. |
| `GET /guard/aprobaciones` | `{"pendientes": [...], "usadas": [...]}`; cada una con `id`, `contexto_id`, `cart_id`, `centavos`, `monto`, `moneda`, `creada`, `estado`, `usada`, `task_id`. |
| `GET /guard/events` | SSE. Cada mensaje: `event: evento` + `data: <Evento JSON>`. Al conectar reenvía los últimos eventos en memoria (hasta 200). `: ping` cada 15 s. Hasta 20 suscriptores (503 después). |
| `GET /guard/events/history?limit=200` | Lista JSON de `Evento` (los más viejos primero, máximo 200). |
| `GET /guard/public-key` | `{"kid": "...", "jwk": {...}}` si hay Guard real con `guard.recibo`; 404 si no. |

**Cómo se usa la aprobación:** el agente reintenta `create_checkout` (el
Shopping Agent se lo pide al usuario cuando ve `escalar`); el Guard vuelve a
evaluar. Solo si da otra vez `escalar` (nunca un `bloqueado`) y el total
firmado del nuevo checkout (entero exacto y moneda) es el aprobado, el
checkout pasa intacto y la aprobación queda usada.

### Eventos (`contracts.Evento`)

Además van a `GUARD_EVENTS_JSONL`, una línea por evento. `escenario` es el
`demo_scenario` de la compra; `contexto_id`, el `context_id` de A2A.

| `tipo` | `datos` |
|---|---|
| `busqueda_enviada` | `{"catalog_search": str}`. Sale con el `contextId` del pedido o, si no lo traía, recién con el de la respuesta del comercio. |
| `ofertas_recibidas` | `{"carts": [...], "offer_details": [...]}` |
| `checkout_pedido` | `{"cart_id": str}` |
| `checkout_recibido` | `{"cart_id", "checkout_jwt", "checkout_hash", "jwt_header", "jwt_payload", "ap2_violaciones"}` (+ `cart_id_checkout` si el del checkout no es el del pedido). Header y payload decodificados solo para mostrar; `ap2_violaciones` = `demo_world.ap2_check.ap2_violations(cart)` |
| `veredicto` | `{"veredicto": <Veredicto JSON completo>, "fuente": "http"\|"fixture", "recibo_firmado": bool, "evaluador": "guard"\|"stub"}` |
| `reenviado` | `{"metodo": "find_products"\|"create_checkout"\|"otro", "mandatos": {...}?, "aprobacion_humana": true?, "aprobacion_id": str?}` |
| `bloqueado` | `{"cart_id", "motivo", "status", "requiere_aprobacion": bool}` (`requiere_aprobacion: true` = retenido por `escalar`) |
| `guard_cambiado` | `{"guard_activo": bool}` |
| `error` | `{"mensaje"}` |
| `pedido_detectado` | Etapa 2. `{"compra_id", "pedido", "pregunta", "requisitos": [Requisito], "vence"}` |
| `contexto_registrado` | Etapa 2. `{"compra_id", "omitido": bool, "vencido": bool, "contexto": Contexto\|null}` |
| `oportunidad` | Etapa 2. `{"compra_id", "oportunidad": Oportunidad, "vence"}` |
| `oportunidad_decidida` | Etapa 2. `{"compra_id", "oportunidad_id", "decision": "aceptada"\|"rechazada"\|"vencida", "requisitos_autorizados": [Requisito]\|null}` |

`bloqueado` de la etapa 2 lleva además `"etapa": "seguridad"` (corte del
filtro, con `senales`) o `"etapa": "intencion"` (la consulta venció).

## Etapa 2: contexto, filtro y oportunidad

El flujo único de [docs/FLUJO.md](../docs/FLUJO.md). Solo con el Guard
prendido; con el interruptor apagado el proxy no pregunta ni retiene nada.

1. **Contexto.** Con un agente de intención cargado (`intencion/`), el
   primer `find_products` de una compra se **retiene**: el agente arma la
   pregunta del porqué y los parámetros explícitos del pedido, se emite
   `pedido_detectado` y el proxy espera la respuesta del usuario
   (`POST /guard/compras/contexto`), que la omita o que venza
   `PROXY_ESPERA_CONTEXTO_S`. Recién ahí reenvía el pedido al proveedor.
   Otra búsqueda del mismo contexto no vuelve a preguntar.
2. **Filtro.** Con un filtro cargado (`GUARD_FILTRO`, de Dev 1), cada
   respuesta del proveedor pasa por él antes que nada. Si bloquea o falla,
   el agente del cliente recibe una tarea `failed` con un texto fijo y
   **nada** del proveedor; no se llama al agente de intención.
3. **Oportunidad.** Si ninguna oferta cumple los parámetros autorizados, el
   agente busca una que cubra la necesidad. Si la encuentra, se emite
   `oportunidad` y el proxy **retiene la respuesta** hasta la decisión
   (`POST /guard/compras/decision`) o hasta que venza
   `PROXY_ESPERA_DECISION_S`:
   - **acepta:** la respuesta pasa intacta, más el artefacto
     `guard.parametros_autorizados` (`cart_id`, `requisitos`, `texto`
     fijo), y la compra queda con esos parámetros;
   - **rechaza:** pasa intacta y siguen los parámetros originales;
   - **vence:** tarea `failed` sin nada del proveedor ("en pausa"): el
     silencio no es autorización.
4. **Verificación.** En `create_checkout`, `evaluar()` usa los parámetros
   autorizados **de esa compra** (si no hay compra, la ficha de la demo).
   Una oferta que no los cumple se bloquea aunque el agente la elija.

Reglas que no dependen de la demo:

- **Ficha.** Si el escenario de la demo tiene una ficha confirmada
  (`scenarios.json` o `fixtures/`), manda esa. La que arma el agente con el
  pedido solo se usa si no hay ninguna (un comercio real). El LLM puede
  extraer de más o de menos, y no puede achicar ni agrandar la ficha
  confirmada.
- **Tiempo.** Las dos esperas caen en la misma llamada del agente del
  cliente, que espera hasta 600 s. El proxy usa un presupuesto de 570 s por
  llamada (los plazos se recortan a lo que queda), y cada llamada al agente
  de intención tiene un tope de 45 s.
- **Filtro.** Corre en un hilo, con un tope de 30 s. Si vence o falla,
  bloquea. Revisa también los errores del comercio, los `result` que no son
  objeto y cada evento de un stream. Lo que no se puede leer, con filtro, no
  se reenvía.
- **Streams (SSE).** El filtro corta evento por evento. La respuesta no se
  retiene (ya salió), así que no hay oportunidad: sigue el pedido original y
  el Guard igual verifica el checkout.
- **Búsquedas repetidas.** Otra búsqueda de la misma compra espera el mismo
  porqué y la misma decisión. Después de vencida o bloqueada, tampoco recibe
  nada; después de aceptada, vuelve a llevar los parámetros.
- **Contexto.** Una respuesta del comercio que usa el `contextId` de otra
  compra se corta.
- **Fallas del agente.** Si el agente de intención falla, la compra sigue con
  el pedido literal (y un evento `error`): la seguridad no depende de esa
  capa.
- **Data parts del proxy.** Un `guard.parametros_autorizados` que mande el
  comercio se borra, igual que un `guard.veredicto`.

| Variable | Default | Qué es |
|---|---|---|
| `GOOGLE_API_KEY` o `GEMINI_API_KEY` | — | Sin ninguna de las dos no hay agente de intención (la etapa 2 queda apagada). `world/run_world.sh` igual exige `GOOGLE_API_KEY` |
| `INTENCION_ACTIVA` | `1` | `0` para apagar la capa de intención aunque haya clave. **Usalo para el benchmark y `smoke_checkout.py`**: no hay un humano que conteste y cada búsqueda esperaría `PROXY_ESPERA_CONTEXTO_S` |
| `INTENCION_MODELO` | `gemini-3.1-flash-lite-preview` | Modelo del agente de intención |
| `GUARD_FILTRO` | — | Filtro de seguridad, `paquete.modulo:funcion` (`contracts.Filtro`). Si se pide y no carga, el proxy no arranca |
| `PROXY_ESPERA_CONTEXTO_S` | `120` | Cuánto se espera el porqué |
| `PROXY_ESPERA_DECISION_S` | `420` | Cuánto se espera la decisión sobre la oportunidad |

| Endpoint | Respuesta |
|---|---|
| `GET /guard/compras` | `{"compras": [Compra]}`, la más nueva primero. `Compra`: `compra_id`, `estado` (`esperando_contexto`, `negociando`, `esperando_decision`, `sin_oportunidad`, `bloqueada`, `aceptada`, `rechazada`, `vencida`), `creada`, `contexto_id`, `pedido`, `pregunta`, `requisitos`, `contexto`, `oportunidad`, `vence`, `decision`, `requisitos_autorizados` |
| `POST /guard/compras/contexto` con `{"compra_id", "respuesta"}` o `{"compra_id", "omitir": true}` y `X-Guard-Token` | 200 con la compra; 404 `compra_desconocida`; 409 `{"error": "estado", "estado"}` si no está esperando el porqué; 400 si el cuerpo no es exactamente eso (respuesta de 1 a 1000 caracteres) |
| `POST /guard/compras/decision` con `{"compra_id", "decision": "aceptar"\|"rechazar"}` y `X-Guard-Token` | Igual: 200, 404, 409 (si no hay una consulta pendiente) o 400 |

`GET /guard/state` suma `"intencion"` (nombre del agente o `null`) y
`"filtro"` (bool).

## Seguridad

- Los mandatos y el `checkout_jwt` completos se muestran en el dashboard (la
  demo lo exige). La mitigación: el proxy solo escucha en loopback, la API
  valida `Host`/`Origin`/`Content-Type`, los POST exigen el token por
  arranque, y el JSONL y el token se crean con modo 0600.
- El evaluador falla cerrado: sin `guard.evaluar()` real con `checkout=` el
  proxy no arranca (salvo `PROXY_ALLOW_STUB=1`, marcado en todo).
- Por el veredicto no le llega al agente texto del comercio (versión
  reducida); `id`/`event` del SSE se sanean.
- Ningún header del comercio llega al cliente salvo `X-A2A-Extensions`.
- JSON estricto (sin `NaN`/`Infinity`), lectura con tope de bytes y SSE leído
  línea a línea con el mismo tope.
- Los textos que el proxy manda al agente son plantillas fijas; los detalles
  de errores (que pueden traer texto del comercio) solo van al evento `error`.

## Registro en Supabase (opcional)

Con `SUPABASE_URL` y `SUPABASE_SERVICE_ROLE_KEY` en el entorno, `python -m
proxy` registra en Supabase (`persistencia.py`):

- **`veredictos`**: una fila por evento `veredicto`, con el `Veredicto`
  completo y el recibo firmado (`recibo_jws`). Las `verificaciones` del
  recibo traen, campo por campo, lo declarado contra el catálogo público: es
  la evidencia de por qué se rechazó. No se suben mandatos ni el
  `checkout_jwt` (solo su `checkout_hash`).
- **`aprobaciones`**: cada aprobación humana al crearse y al usarse. Es
  auditoría: la regla de un solo uso sigue en memoria y no se recarga al
  arrancar (las ofertas de ese contexto ya no están).

Es solo registro: escribe en segundo plano (timeout 5 s) y, si Supabase no
responde, el proxy sigue igual, avisa con un evento `error` una vez por caída
y el JSONL queda como copia completa. Sin las variables no se escribe nada.
Los tests nunca usan el entorno: la persistencia se inyecta en `create_app`.

Tablas: `sql/001_persistencia.sql` (RLS activado y sin políticas: la clave
anon no lee ni escribe). Para subir corridas del benchmark:

```sh
world/ap2/.venv/bin/python -m proxy.subir_benchmark results/*.json
```

Hace upsert en `benchmark_corridas` y `benchmark_escenarios` por nombre de
archivo: subir la misma corrida dos veces no la duplica.

## Código

| Archivo | Qué es |
|---|---|
| `app.py` | `create_app(config, transport=None, evaluador=None, persistencia=None, agente=None, filtro=None)`: rutas, reenvío, etapas de la etapa 2 (contexto, filtro, oportunidad), decisión sobre el checkout (JSON y SSE), caché de decisiones, API de control |
| `a2a.py` | Helpers puros sobre el JSON de A2A (pedido, ofertas, checkouts, `guard.veredicto` ajeno y versión para el agente, tareas bloqueadas, total firmado exacto, huella del checkout, lectura acotada y SSE saneado) |
| `aprobaciones.py` | Registro de aprobaciones humanas (un solo uso, atadas a contexto, cart y monto) |
| `seguridad.py` | Middleware de la API de control (`Host`, `Origin`, `Content-Type`, `X-Guard-Token`) y el token por arranque |
| `evaluador.py` | `cargar_evaluador()` (Guard real con `checkout=` o `EvaluadorNoDisponible`; el stub solo con `PROXY_ALLOW_STUB=1`), clave y firma del recibo con `guard.recibo` |
| `guard_stub.py` | Stub con la firma de `contracts.Evaluador`: bloquea si el elegido es el último cart |
| `eventos.py` | Bus de eventos: memoria (200), suscriptores SSE (hasta 20) y JSONL |
| `fuentes.py` | Intent, catálogo público (con su fuente) y `ap2_check` con respaldo en `fixtures/` |
| `config.py` | Variables de entorno y chequeo de loopback |
| `persistencia.py` | Registro opcional en Supabase (veredictos y aprobaciones), en segundo plano y sin cortar el flujo si falla |
| `compras.py` | Etapa 2: registro de compras en memoria (pedido, parámetros autorizados, contexto, oportunidad, decisión) y las dos esperas |
| `subir_benchmark.py` | `python -m proxy.subir_benchmark results/*.json`: sube corridas del benchmark a Supabase |
| `sql/001_persistencia.sql` | Tablas de Supabase |
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
`guard.evaluar()` real. Los tests que usan el stub lo habilitan
explícitamente (`permitir_stub=True` en el `config` de `conftest.py`) y sus
clientes mandan el token, como el dashboard.
