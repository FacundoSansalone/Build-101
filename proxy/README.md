# proxy/ (Dev 3)

Proxy A2A en el puerto **8010**, entre el Shopping Agent y el Merchant Agent
(8001). Reenvía todo al comercio, llama al Guard en `create_checkout` y aplica
el veredicto. **Nunca modifica el `checkout_jwt`, el `checkout_hash` ni los
mandatos**: los bytes del pedido se reenvían tal cual y el `ap2.checkout` de
la respuesta no se toca (hay tests que lo verifican byte a byte y que la firma
ES256 sigue valiendo). **No decide**: decide `guard/`; el proxy transporta,
registra, aplica el veredicto y bloquea lo que no pudo evaluar (fail-closed).

El mismo proceso es además el **gateway de producción** para integradores
externos (API `/v1`, varios comercios, claves de API, webhooks y estado
persistente). Se activa solo con configuración: sin ella, todo sigue como en
la demo. Ver [Gateway de producción](#gateway-de-producción) y, para quien
integra, [`docs/INTEGRACION.md`](../docs/INTEGRACION.md).

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
| `CATALOGO_URL` | `http://127.0.0.1:8011` | Catálogo público (`GET /products`). Vacío = el comercio no publica catálogo: no se consulta y se evalúa sin catálogo |
| `CATALOGO_RESPALDO_FIXTURE` | `0` | `1` solo para la demo offline: si el catálogo no responde, usar `fixtures/public_catalog.json`. Con `0`, se evalúa sin catálogo |
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

Al revés, el Shopping Agent habla con el proxy por `localhost:8010`
(`MERCHANT_AGENT_URL`): con `PROXY_HOST=127.0.0.1` (el default) el proxy
escucha además en `[::1]` al mismo puerto (medido en Windows: ~2,1 s menos
por conexión nueva). Si la máquina no tiene IPv6, sigue solo con IPv4 y lo
avisa en el log; si otro proceso tiene ese puerto en `[::1]`, **no arranca**
(los clientes de `localhost` le hablarían a ese). En Windows los sockets se
abren con `SO_EXCLUSIVEADDRUSE`, así ningún otro proceso se ata al mismo
puerto (con el `SO_REUSEADDR` de uvicorn, Windows lo dejaba).

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
| `escalar` | **Retenido**: la misma tarea (mismos ids) en `failed`, sin el `ap2.checkout`, con `guard.veredicto` y el texto "KaiAI: escalar. <motivo> El checkout requiere aprobación humana…" | `veredicto`, `bloqueado` con `requiere_aprobacion: true` |
| `escalar` + aprobación humana vigente (mismo contexto, cart y monto) | Tarea intacta (checkout byte a byte el del comercio) + `guard.veredicto` | `veredicto`, `reenviado` con `aprobacion_humana: true` |
| `bloqueado` | Tarea `failed`, sin el checkout, con `guard.veredicto` y "KaiAI: bloqueado. <motivo> No se presenta para firma." | `veredicto`, `bloqueado` |
| No se pudo evaluar | Tarea `failed` (la del comercio o una armada por el proxy) con un `guard.veredicto` `bloqueado` y un motivo fijo del proxy | `error`, `bloqueado` |

El texto para el agente es siempre la plantilla fija
`"KaiAI: <status>. <motivo>"` (el motivo lo arma `guard/` sin texto
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

- **Ficha:** los parámetros autorizados de la compra (etapa 2, abajo) o, si
  no hay compra, la ficha confirmada **del escenario de esa compra** y, si
  no se conoce, la global (`demo_world.scenario.intent()`; si no se puede
  importar, `fixtures/scenario_baseline.json` → `intent`). Siempre con
  `guard.ficha.ficha_desde_intent`: los requisitos de `hard`/`preferred`,
  los atributos (`intent.attributes`) y la categoría de la taxonomía
  (`google_product_category`). Con `PROXY_FICHA_DEMO=0` no se usan las fichas
  de la demo (como con un comercio real): sin compra no hay parámetros
  autorizados y el Guard escala.
- **Fabricante (`fabricante/`):** antes de `evaluar()` el proxy consulta la
  ficha del fabricante por cada GTIN de las ofertas (el declarado y el del
  catálogo para ese SKU), con la fuente de `FABRICANTE_FUENTE` (por defecto
  `fixture`: datos de prueba, rotulados), y la suma al `catalogo` como
  `DatoExterno` (`guard.fuentes.con_fuentes`). El I/O queda afuera de
  `evaluar()`. Sin fuente, sin dato, con falla o si tarda más de 15 s, lo
  obligatorio que solo declara el comercio escala: nunca aprueba por eso.
  El evento `veredicto` dice qué fuente se usó en `fabricante`. Lo mismo se
  usa para verificar las ofertas que ve el detector de oportunidades.
- **Catálogo (no siempre existe):** `GET CATALOGO_URL/products` →
  `sku -> producto`. Si no hay `CATALOGO_URL` o no responde, se evalúa **sin
  catálogo** (todo queda "solo declarado" y lo obligatorio escala); el fixture
  solo con `CATALOGO_RESPALDO_FIXTURE=1`. El evento `veredicto` dice cuál se
  usó en `fuente` (`http`, `fixture` o `ninguno`).
- **Comercio:** `comercio_id` es el `merchant.id` **firmado** en el checkout,
  no el del catálogo. Si el catálogo dice ser de otro comercio, no se usa
  (`catalogo_descartado` en el evento `veredicto`).

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

**Compras de integrador en los GET.** Sin `X-Guard-Token`, `GET
/guard/compras`, `/guard/aprobaciones`, `/guard/events` y
`/guard/events/history` no incluyen nada de las compras de integrador (sus
compras, sus aprobaciones ni los eventos de su tráfico o de sus contextos).
El servidor del dashboard reenvía los GET sin el token y en un deploy
(`deploy/tenki.sh`) el panel es público: sin esto, cualquiera veía el pedido,
la ficha y los contextos de todos los integradores, que `/v1` aísla. Con el
token (un proceso de la máquina que lo lee de `.temp-db/guard_api_token`) se
ve todo. La demo (tráfico anónimo) se ve igual que siempre.

| Endpoint | Respuesta |
|---|---|
| `GET /guard/state` | `{"guard_activo": bool, "merchant_url": str, "evaluador": "guard"\|"stub"}` (más `gateway` si está configurado: ver [Gateway de producción](#gateway-de-producción)) |
| `POST /guard/state` con `{"guard_activo": bool}` y `X-Guard-Token` | Mismo formato. Emite `guard_cambiado`. 400 si el cuerpo no es eso. |
| `POST /guard/aprobaciones` con `{"contexto_id": str, "cart_id": str}` y `X-Guard-Token` | 409 `compra_de_integrador` si el contexto es de una compra de integrador (en memoria, en el almacén o marcado al registrar su veredicto); 503 si el almacén no responde. Si no, registra una **aprobación humana de un solo uso** para ese contexto y cart, atada al **total firmado exacto** del último veredicto `escalar` de ese contexto/cart: el `totals[type=total].amount` del payload del `checkout_jwt` (un entero, en centavos) y su `currency`, sin fallbacks a `line_items` ni al data part. 201 con la aprobación; 200 si ya había una pendiente igual; 409 si el último veredicto no es `escalar` o si el checkout escalado no tiene un total firmado entero exacto (falta, es float, hay dos, claves duplicadas…): queda retenido; 400 si el cuerpo no es exactamente eso. |
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
| `checkout_recibido` | `{"cart_id", "checkout_jwt", "checkout_hash", "jwt_header", "jwt_payload", "ap2_violaciones"}` (+ `cart_id_checkout` si el del checkout no es el del pedido). Header y payload decodificados solo para mostrar; `ap2_violaciones` = `demo_world.ap2_check.ap2_violations(cart, intent)` con el **tope y la moneda de la ficha de esa compra** (`intent_ap2`); `null` si la compra no tiene tope o no se pudo correr |
| `veredicto` | `{"veredicto": <Veredicto JSON completo>, "fuente": "http"\|"fixture"\|"ninguno", "fabricante": {"fuente": "fixture"\|"icecat"\|"ninguna"\|…, "fixture": bool, "consultas": [{"gtin", "con_dato", "motivo"}], "error"?: str}, "comercio_id": <merchant.id firmado>, "catalogo_descartado"?: str, "recibo_firmado": bool, "evaluador": "guard"\|"stub"}` |
| `reenviado` | `{"metodo": "find_products"\|"create_checkout"\|"otro", "mandatos": {...}?, "aprobacion_humana": true?, "aprobacion_id": str?}` |
| `bloqueado` | `{"cart_id", "motivo", "status", "requiere_aprobacion": bool}` (`requiere_aprobacion: true` = retenido por `escalar`) |
| `guard_cambiado` | `{"guard_activo": bool}` |
| `error` | `{"mensaje"}` |
| `pedido_detectado` | Etapa 2. `{"compra_id", "pedido", "pregunta", "requisitos": [Requisito], "vence", "turno": int, "confirmar": bool}`. Con intake, además `"faltan"`, `"a_confirmar"`, `"ficha_parcial"`, `"cortado"`, `"mensaje"`, `"fuera_de_catalogo"`. Uno por turno (0 = el porqué) |
| `contexto_registrado` | Etapa 2. `{"compra_id", "turno", "respuesta": str\|null, "omitido": bool, "vencido": bool, "contexto": Contexto\|null}`. Con intake, `"intake"` y, en el último, `"final": true`, `"confirmada"`, `"ficha_autorizada"`, `"sin_verificar_guard"`, `"categoria_taxonomia"` (ID de la taxonomía de Google o `null`), `"sin_tiempo"` |
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

   **Intake (agente `decisiones`):** si después de una respuesta falta algo
   necesario para comprar (producto, cantidad, tope, moneda, plazo), el
   proxy **vuelve a preguntar** en la misma llamada: otro
   `pedido_detectado` con `turno`, `faltan` y `ficha_parcial`, y otra
   espera. Corta tras 3 repreguntas (y lo dice). Al final pide **confirmar
   la ficha** (`confirmar: true`): el usuario la confirma
   (`{"compra_id", "confirmar": true}`), la corrige (una respuesta: se
   vuelve a evaluar) o no la confirma (omitir). Lo inferido (por ejemplo,
   una sola unidad o la moneda de un "$" suelto) solo entra a los
   parámetros autorizados si confirmó. Los atributos (memoria, color,
   cancelación de ruido…) y el operador `minimo` **entran** a la ficha
   autorizada: el Guard los verifica (`guard.chequeos.CAMPOS_SOPORTADOS` y
   `OPERADORES_SOPORTADOS`); `sin_verificar_guard` queda para lo que el Guard
   no supiera chequear (hoy, nada). Al cerrar, si el agente sabe clasificar
   (el de decisiones) y queda tiempo, la ficha suma la categoría de la
   **taxonomía de Google** (`taxonomia.clasificar`, unas 3 llamadas; sin
   cliente queda vacía). Solo se repregunta si quedan al menos 30 s de la
   llamada. Con Gemini (sin intake) es una sola pregunta, como antes.
2. **Filtro.** Con un filtro cargado (`GUARD_FILTRO`, de Dev 1), cada
   respuesta del proveedor pasa por él antes que nada. Si bloquea o falla,
   el agente del cliente recibe una tarea `failed` con un texto fijo y
   **nada** del proveedor; no se llama al agente de intención. Al filtro no
   le llega el **eco exacto de nuestro propio pedido** en el `history` de la
   tarea (mismo `messageId`, rol y partes: el agente del cliente del sample
   manda `role: agent` y el comercio lo repite); un eco alterado por el
   comercio sí se revisa. La respuesta que se reenvía no cambia.
3. **Oportunidad.** Si ninguna oferta cumple los parámetros autorizados, el
   agente busca una que cubra la necesidad. Si la encuentra, se emite
   `oportunidad` y el proxy **retiene la respuesta** hasta la decisión
   (`POST /guard/compras/decision`) o hasta que venza
   `PROXY_ESPERA_DECISION_S`. El `titulo` de la oportunidad es el **título
   público** de la oferta (el de la fuente que lo verificó: catálogo, página
   pública o fabricante) o vacío, nunca el que declara el vendedor: el proxy
   lo fuerza, sea cual sea el agente, y sin título el dashboard muestra el
   SKU. Según lo que decida el usuario:
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
  pedido solo se usa si no hay ninguna (un comercio real, o
  `PROXY_FICHA_DEMO=0`). El LLM puede extraer de más o de menos, y no puede
  achicar ni agrandar la ficha confirmada.
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
| `INTENCION_PROVEEDOR` | según las claves | `decisiones` (intake con juicios tipados: repregunta y confirma la ficha) o `gemini` (una sola pregunta). Sin la variable: `decisiones` si hay cliente de decisiones, si no `gemini`. Otro valor: no arranca |
| `OPENAI_API_KEY` o `TYPESAFE_API_KEY` | — | Cliente de decisiones (OpenAI Decisions o Jev; `DECISIONES_PROVEEDOR` elige, ver `decisiones/`) |
| `GOOGLE_API_KEY` o `GEMINI_API_KEY` | — | Agente `gemini` y, con `decisiones`, el detector de oportunidades (sin Gemini hay intake pero no oportunidades). Sin ninguna clave no hay agente de intención. `world/run_world.sh` igual exige `GOOGLE_API_KEY` |
| `INTENCION_ACTIVA` | `1` | `0` para apagar la capa de intención aunque haya clave. **Usalo para el benchmark y `smoke_checkout.py`**: no hay un humano que conteste y cada búsqueda esperaría `PROXY_ESPERA_CONTEXTO_S` |
| `INTENCION_MODELO` | `gemini-3.1-flash-lite-preview` | Modelo de Gemini (agente o detector) |
| `GUARD_FILTRO` | — | Filtro de seguridad, `paquete.modulo:funcion` (`contracts.Filtro`). Si se pide y no carga, el proxy no arranca |
| `PROXY_ESPERA_CONTEXTO_S` | `120` | Cuánto se espera el porqué (y cada repregunta del intake) |
| `PROXY_ESPERA_DECISION_S` | `420` | Cuánto se espera la decisión sobre la oportunidad |
| `PROXY_PERMITIR_SIN_FILTRO` | `0` | Con la intención activa, sin `GUARD_FILTRO` el proxy no arranca; `1` lo permite (solo tests y demo, se avisa en el log) |
| `PROXY_FICHA_DEMO` | `1` | `0`: no usar las fichas de la demo (`scenarios.json`, `fixtures/`), como con un comercio real. Mandan los parámetros que armó y confirmó el intake; sin compra, el Guard escala |
| `DEMO_HOY` | — | Fecha `YYYY-MM-DD` que el agente de intención toma como hoy. Las ofertas de la demo tienen fechas fijas (hoy = domingo `2026-10-18`, "el lunes" = `2026-10-19`): para correrla otro día, `DEMO_HOY=2026-10-18`. Se avisa en el log al arrancar; otro formato no arranca. No llega al Guard: los vencimientos del checkout firmado se miran con el reloj de verdad |
| `FABRICANTE_FUENTE` | `fixture` | Ficha del fabricante por GTIN: `fixture` (`fixtures/fabricante/`, datos de prueba rotulados), `icecat` (en vivo) o `ninguna`. Un valor inválido (o `icecat` sin `ICECAT_USUARIO`) no arranca. Ver [`fabricante/README.md`](../fabricante/README.md) (`FABRICANTE_FIXTURES_DIR`, `ICECAT_*`) |
| `FILTRO_SOLO_HEURISTICAS`, `DECISIONES_PROVEEDOR`, `DECISIONES_MODELO_*`, `FILTRO_TIMEOUT_S` | — | Del filtro y del cliente de decisiones: ver [`filtro/README.md`](../filtro/README.md) |

| Endpoint | Respuesta |
|---|---|
| `GET /guard/compras` | `{"compras": [Compra]}`, la más nueva primero. `Compra`: `compra_id`, `estado` (`esperando_contexto`, `negociando`, `esperando_decision`, `sin_oportunidad`, `bloqueada`, `aceptada`, `rechazada`, `vencida`), `creada`, `contexto_id`, `pedido`, `pregunta`, `requisitos`, `contexto`, `oportunidad`, `vence`, `decision`, `requisitos_autorizados` e `intake` (`turno`, `confirmar` y, con intake, lo que falta, la ficha parcial y, al final, la autorizada) |
| `POST /guard/compras/contexto` con `{"compra_id", "respuesta"}`, `{"compra_id", "omitir": true}` o `{"compra_id", "confirmar": true}` y `X-Guard-Token` | 200 con la compra; 404 `compra_desconocida`; 409 `{"error": "estado", "estado"}` si no está esperando una respuesta (o, para `confirmar`, si el turno no es el de confirmar la ficha); 400 si el cuerpo no es exactamente eso (respuesta de 1 a 1000 caracteres) |
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

Este registro es auditoría y no cambia con el gateway. El **estado** de las
compras de integrador (lo que hace falta para seguir después de un
reinicio) es otra cosa: va en el almacén del gateway (`GATEWAY_ESTADO`, ver
abajo) y en otras tablas. Ahí sí sobreviven las aprobaciones de esas
compras, con el uso único controlado en la base (`gw_aprobaciones`).

## Gateway de producción

El proxy llevado a producción: cualquier agente de compras externo (un
**integrador**) puede pasar sus compras por el filtro, la intención y el Guard,
contra cualquier comercio de una lista que aprueba el operador. La guía para
quien integra está en [`docs/INTEGRACION.md`](../docs/INTEGRACION.md); acá va
lo del operador. El filtro, la intención y el Guard funcionan igual que en la
demo.

**Sin configuración nueva, la demo queda igual.** Sin `GATEWAY_CLAVES_ARCHIVO`
(o sin ninguna clave activa) no hay oyente público, el Shopping Agent sigue
hablando con `:8010/a2a/merchant_agent` sin credenciales, `/guard/*` y el
dashboard no cambian y no se escribe nada nuevo en Supabase. Lo único que se
suma en el oyente local son rutas: `/a2a/m/merchant_1` (el mismo comercio de
siempre) y `/v1/*` (`/v1/compras` da 401 porque no hay claves; la
especificación y la clave pública responden). Además, el log de cada pedido
nombra el comercio (`→ comercio merchant_1: …`).

### Oyentes

Un solo proceso, una sola app y un solo servidor sobre **dos sockets**, para
que compartan el estado de las compras, las ofertas y las aprobaciones:

| Oyente | Default | Qué sirve | Autenticación |
|---|---|---|---|
| **local** | `PROXY_HOST:PROXY_PORT` = `127.0.0.1:8010` | todo: `/a2a/*`, `/guard/*`, `/v1/*` | `/guard/*` igual que siempre. `/a2a/*` anónimo (la demo) o con clave `psg_` (modo integrador). `/v1/*` con clave |
| **público** | `GATEWAY_HOST:GATEWAY_PORT` = `127.0.0.1:8030` | `/a2a/*` y `/v1/*`; `/guard/*` y el resto dan 404 | Siempre con clave, salvo `/v1/public-key`, `/v1/openapi.json` y `/v1/docs` |

- El público **solo se abre con al menos una clave activa**. Sin claves,
  `python -m proxy` arranca como siempre (`uvicorn.run`, un oyente). Con
  claves arma `uvicorn.Server(...).run(sockets=[local, público])` (uvicorn
  0.54) y Ctrl+C sale con 0.
- El middleware `AutenticacionGateway` (el más externo) reconoce el oyente
  por el **puerto local** de la conexión. Por eso `GATEWAY_PORT` no puede ser
  igual a `PROXY_PORT` (no arranca).
- **TLS** lo termina el túnel o el proxy reverso (Tenki publica `https://`).
  El gateway sirve HTTP plano; `GATEWAY_PUBLIC_URL` es la URL https pública y
  se usa en la Agent Card, en las `a2a_url` y en el OpenAPI.

**Arranque fail-closed.** `python -m proxy` no arranca (`SystemExit` con el
motivo) si: un archivo de configuración es inválido o no se puede leer (si
`GATEWAY_CLAVES_ARCHIVO` está definido y el archivo no existe, tampoco: el
operador pidió el gateway); el archivo de claves nombra comercios que no
están registrados; la URL de un webhook no sirve; los dos puertos son
iguales; hay claves y
`PROXY_ALLOW_STUB=1` (con el gateway el stub está prohibido); hay claves y no
hay `GUARD_FILTRO` (salvo `PROXY_PERMITIR_SIN_FILTRO=1`, solo tests y demo);
`GATEWAY_ESTADO=supabase` sin credenciales; o una variable `GATEWAY_*` tiene un
valor inválido. Sin claves activas (todas revocadas) no hay oyente público:
un `GATEWAY_HOST` fuera de loopback no expone nada, así que solo se avisa y
la demo arranca igual.

### Autenticación

- **Clave:** `psg_<kid>_<secreto>`. `kid` son 12 hex públicos (sirven para
  buscarla); el secreto sale de `secrets.token_urlsafe(32)`. En el archivo
  solo va `sha256(clave completa)`. La comparación es en tiempo constante, y
  un `kid` que no existe se compara contra un hash ficticio.
- **Header:** `Authorization: Bearer psg_…`. En el oyente local, un
  `Authorization` que no es nuestro pasa como anónimo (no cambia la demo);
  una clave `psg_` presente tiene que ser válida (nunca se degrada a
  anónimo). Más de un `Authorization` con una clave nuestra: 401.
- Sin clave: 401 `no_autenticado` (`WWW-Authenticate: Bearer`); clave mala:
  401 `clave_invalida`. Nunca se loguea la clave ni el header.

Generar una clave (imprime la clave una sola vez y el objeto para el archivo)
y un secreto de webhook:

```sh
world/ap2/.venv/bin/python -m proxy.claves nueva --integrador acme
world/ap2/.venv/bin/python -m proxy.claves secreto-webhook
```

### Tráfico A2A de integrador

Con una clave válida (en cualquier oyente), un pedido a `/a2a/m/{comercio}`
es de un integrador:

- **`X-Compra-Id` obligatorio.** La compra tiene que ser de ese integrador y
  de ese comercio, no estar `bloqueada` ni `vencida` y tener menos de
  `GATEWAY_COMPRA_TTL_S`.
- **El Guard siempre está prendido.** El interruptor de `/guard/state` es de
  la demo y solo afecta al tráfico anónimo.
- **La ficha es la del integrador** (`POST /v1/compras`) y queda fija: el
  `catalog_search`, el `demo_scenario` y el intake no la cambian. No hay
  intake ni repreguntas: solo el porqué (si lo manda o pide que se
  pregunte). La cambia solo el humano, aceptando una oportunidad por `/v1`.
- **Atadura del `contextId`:** cada `contextId` que aparece (en el pedido o
  en la respuesta, JSON o cada evento SSE) se ata a la compra del
  `X-Compra-Id`, en memoria y en el almacén (gana el primero). Si ya es de
  otra compra: en el pedido, 409 `contexto_ajeno` sin reenviar; en la
  respuesta, se corta. Máximo 20 por compra. Un `contextId` que no es un
  identificador seguro también se corta.
- Antes de reenviar se sacan `Authorization` y `X-Compra-Id`: la clave nunca
  llega al comercio. Tope de cuerpo: `GATEWAY_MAX_CUERPO_A2A_BYTES` (413).
- Los errores antes de reenviar son HTTP + error JSON-RPC (`code: -32001`,
  mensaje fijo `KaiAI: …` y `data.codigo`). `comercio_desconocido`
  (404), `comercio_no_permitido` (403) y `pedido_ilegible` (400) salen con
  `id: null` porque se resuelven antes de leer el cuerpo (o sin poder
  leerlo); el resto lleva el `id` del pedido. La
  tabla completa está en [INTEGRACION.md → Errores](../docs/INTEGRACION.md#11-errores).
- **Escalar:** la compra guarda `aprobacion_pendiente`, sale el webhook
  `compra.requiere_aprobacion` y el integrador aprueba por
  `POST /v1/compras/{id}/aprobaciones`. Las reglas de siempre (contexto,
  cart, total firmado, un solo uso, nunca salta un `bloqueado`) no cambian.
  Un segundo POST de la misma aprobación devuelve 200 con la misma.
- **El dashboard no autoriza compras de integrador.**
  `POST /guard/compras/contexto`, `/guard/compras/decision` y
  `/guard/aprobaciones` sobre una compra de API dan 409
  `{"error": "compra_de_integrador"}`: solo autoriza el dueño, por `/v1`.
  No depende de que la compra siga en memoria: el panel también mira el
  almacén y la marca que deja el veredicto en el registro de aprobaciones, y
  para una compra de integrador solo cuenta una aprobación creada por `/v1`
  (también con el almacén en memoria). Sin el token, el dashboard no las ve
  (ver [API de control](#api-de-control-para-el-dashboard-en-8020)).
- **La ficha del Guard** en el tráfico de integrador es siempre la de la
  compra del `X-Compra-Id`; nunca la de la demo. Sin compra o sin ficha, el
  checkout se bloquea.
- **Pedido ilegible:** un cuerpo que no es un objeto JSON estricto (con
  `NaN`, `Infinity`, `1e999`…) o con un `contextId` que no es texto da 400
  `pedido_ilegible` sin reenviar: el comercio lo leería con `json.loads` y el
  `contextId` quedaría sin chequear.
- **Comercios:** con clave, un comercio que no existe da el mismo 403
  `comercio_no_permitido` que uno no permitido (en el JSON-RPC y en la Agent
  Card), como `/v1`: no se enumeran los comercios del operador. El 404
  `comercio_desconocido` queda para el tráfico anónimo local.
- **Eventos:** las compras de API emiten los mismos eventos. `bloqueado`
  suma `datos.compra_id` y, si no hubo veredicto, `datos.sin_veredicto: true`
  (solo en compras de API: los eventos de la demo quedan iguales).
  `contexto_registrado` lleva `datos.origen: "api"`.

### Varios comercios

`GATEWAY_COMERCIOS_ARCHIVO` registra los comercios. Cada uno se atiende en
`/a2a/m/{id}`, y la ruta vieja (`/a2a/merchant_agent`) apunta al comercio
por defecto. **No hay URL libre:** el upstream sale siempre del registro.

```json
{
  "por_defecto": "merchant_1",
  "comercios": [
    {"id": "merchant_1", "nombre": "Demo", "a2a_url": "http://127.0.0.1:8001/a2a/merchant_agent",
     "catalogo_url": "http://127.0.0.1:8011", "merchant_ids": ["merchant_1"], "respaldo_fixture": false},
    {"id": "tienda_b", "nombre": "Tienda B", "a2a_url": "https://b.example/a2a/agent",
     "catalogo_url": null, "merchant_ids": ["tienda-b"]}
  ]
}
```

- `a2a_url` y `catalogo_url`: `https`, o `http` solo contra loopback; sin
  usuario, query ni fragment.
- `merchant_ids` (los `merchant.id` con que firma sus checkouts) es
  **obligatorio** para todo comercio que no sea el por defecto. Si el
  `merchant.id` firmado no está en la lista, se bloquea antes de evaluar, y
  además el Guard recibe `Politica(comercios_aprobados=merchant_ids)`.
- Las ofertas de un contexto tienen dueño: si otro comercio devuelve el
  mismo `contextId`, se corta; un checkout con ofertas de otro comercio, se
  bloquea.
- Sin archivo hay un solo comercio, el de siempre (`MERCHANT_UPSTREAM_URL`,
  `CATALOGO_URL`, id `GATEWAY_COMERCIO_POR_DEFECTO`), sin `merchant_ids`
  (como hoy). Con archivo, esas dos variables se ignoran (se avisa).

### Integradores (`GATEWAY_CLAVES_ARCHIVO`)

```json
{
  "integradores": [
    {
      "id": "acme",
      "nombre": "ACME Compras",
      "claves": [{"kid": "a1b2c3d4e5f6", "sha256": "<64 hex>", "creada": "2026-10-18", "activa": true}],
      "comercios": ["merchant_1", "tienda_b"],
      "webhook": {"url": "https://acme.example/hooks/guard", "secreto_env": "WEBHOOK_SECRETO_ACME"},
      "max_compras_activas": 1000
    }
  ]
}
```

- `comercios` vacío o ausente = solo el por defecto.
- `webhook.secreto_env` nombra la variable de entorno con el secreto (al
  menos 32 caracteres; `proxy.claves secreto-webhook` genera uno `whsec_…`).
  El secreto nunca va en el archivo.
- Claves desconocidas, ids o `kid` repetidos: no arranca. Si otros usuarios
  pueden leer el archivo, se avisa (`chmod 600`).
- Rotar claves o cambiar comercios requiere reiniciar.

### Webhooks

Los manda el `Despachador` (`webhooks.py`) para las compras de integrador con
webhook: `compra.pregunta`, `compra.oportunidad`,
`compra.requiere_aprobacion`, `compra.veredicto`, `compra.bloqueada` y `ping`.
Sin keep-alive: cada POST va a la IP verificada con el SNI del host, y el
pool de httpcore reutiliza conexiones por IP sin mirar el SNI (con
keep-alive, el webhook de un integrador podía salir por la conexión TLS de
otro que apunta su dominio a la misma IP). En IPv6, una dirección del
prefijo NAT64 (`64:ff9b::/96`) se juzga por la IPv4 de adentro, y las
IPv4-compatibles (`::/96`) y las IPv4-traducidas (`::ffff:0:0:0/96`) se
rechazan.
El cuerpo es la `VistaCompra` del momento (la misma del GET), firmada con
HMAC-SHA256 en `X-Guard-Firma: t=<unix>,v1=<hex>`. El integrador ordena por
`compra.actualizada`: `creada` y `actualizada` son marcas monótonas del
proceso (`max(ahora, última + 1 µs)`, también después de la marca de una fila
de otro proceso), porque en Windows dos `datetime.now()` seguidos dan el
mismo instante. Una normalización al leer (una espera que se cierra al
hidratar la compra después de un reinicio) no la mueve. Hasta 6 intentos; solo
`https` a IPs públicas, resolviendo el host en cada intento y conectando a la
IP ya verificada (contra DNS rebinding); no sigue redirecciones. La cola es en
memoria (hasta 1000 pendientes) y **no sobrevive a un reinicio**: el
integrador reconcilia con `GET /v1/compras/{id}`. `GATEWAY_WEBHOOKS_INSEGUROS=1`
permite `http` e IPs privadas, solo para desarrollo.

### Almacén de estado

`GATEWAY_ESTADO` elige dónde vive el estado de las compras de integrador (la
compra, sus contextos, las ofertas, el último veredicto, las aprobaciones y
las entregas de webhooks). El tráfico anónimo de la demo no se persiste.

- `memoria` (default): como hoy, nada sobrevive a un reinicio.
- `supabase`: PostgREST con timeout de 5 s. Exige `SUPABASE_URL` y la clave
  de servicio. **Solo se activa explícito**: las `SUPABASE_*` del `.env` de la
  demo no lo prenden.

Lo que decide se escribe con `await` (crear la compra, atar un contexto,
crear y reclamar una aprobación): si falla, 503 o corte. Lo demás va en
segundo plano; si se pierde y hay un reinicio, falta el dato y se bloquea.
Las escrituras de una misma compra van de a una (un candado por compra) y
cada una con el estado del momento en que escribe, así una vieja no pisa una
nueva; el paso a `bloqueada` o `vencida` no se descarta por el tope de
escrituras en vuelo. Crear una compra cuenta las activas y la guarda bajo un
candado por integrador (el límite aguanta pedidos concurrentes). Dos
reintentos concurrentes del mismo checkout aprobado: pasa exactamente uno.
**Tras un reinicio**, lo que esperaba cierra del lado seguro: un porqué
pendiente sigue literal y una oportunidad pendiente queda `vencida`. La fila
se reescribe así al hidratarla o listarla, y `GET /v1/compras?estado=` la ve
en el estado nuevo. Con
almacén durable, una aprobación se **reclama en la base** antes de dejar
pasar el checkout (un solo uso aunque se reinicie). Es durabilidad, no
escalado: **una sola instancia activa**.

**Migración `sql/003_gateway.sql`:** crea las tablas `gw_*` con RLS activado
y sin políticas (solo entra la clave de servicio). No se aplica sola. Se
aplica a mano, una vez, desde el SQL Editor del proyecto de Supabase (pegar
el archivo y correrlo) o con `psql "<connection string>" -f proxy/sql/003_gateway.sql`.
Con `GATEWAY_ESTADO=supabase` y sin las tablas, crear una compra da 503
(falla cerrado y se ve).

### Variables del gateway

| Variable | Default | Qué es |
|---|---|---|
| `GATEWAY_CLAVES_ARCHIVO` | — | Integradores (claves, comercios y webhook). Sin archivo o sin claves activas no hay oyente público |
| `GATEWAY_COMERCIOS_ARCHIVO` | — | Registro de comercios. Sin archivo, uno solo desde `MERCHANT_UPSTREAM_URL` y `CATALOGO_URL` |
| `GATEWAY_COMERCIO_POR_DEFECTO` | `merchant_1` | Id de ruta del comercio por defecto sin archivo |
| `GATEWAY_HOST` | `127.0.0.1` | Interfaz del oyente público. Sin claves activas no hay oyente: fuera de loopback solo avisa |
| `GATEWAY_PORT` | `8030` | Puerto del oyente público (distinto de `PROXY_PORT`) |
| `GATEWAY_PUBLIC_URL` | `http://localhost:<GATEWAY_PORT>` | Base pública (https en el deploy): Agent Card, `a2a_url` y `servers` del OpenAPI |
| `GATEWAY_ESTADO` | `memoria` | `memoria` o `supabase` (con la migración 003 aplicada) |
| `GATEWAY_WEBHOOKS_INSEGUROS` | `0` | `1`: webhooks por `http` y a IPs privadas (solo desarrollo) |
| `GATEWAY_MAX_CUERPO_BYTES` | `1048576` | Tope de cuerpo de `/v1` |
| `GATEWAY_MAX_CUERPO_A2A_BYTES` | `5242880` | Tope de un pedido A2A de integrador |
| `GATEWAY_COMPRA_TTL_S` | `86400` | Vida de una compra de integrador |
| *(nombres libres)* | — | Secretos de webhook, los que nombra `secreto_env` |

### `/guard/state`

Si hay algo del gateway configurado (claves activas, más de un comercio, un
almacén que no es memoria o webhooks), la respuesta suma
`"gateway": {"integradores": int, "comercios": [id], "almacen": "memoria"|"supabase", "publico": bool}`.
Sin configuración nueva, el `/guard/state` es el de siempre.

### Probarlo en local

```sh
world/ap2/.venv/bin/python -m proxy.claves nueva --integrador acme
# guardá la clave; pegá el objeto en .temp-db/claves.json:
# {"integradores": [{"id": "acme", "claves": [<objeto>]}]}
GATEWAY_CLAVES_ARCHIVO=.temp-db/claves.json GUARD_FILTRO=filtro:filtrar \
  FILTRO_SOLO_HEURISTICAS=1 INTENCION_ACTIVA=0 PYTHONUTF8=1 PYTHONPATH=. \
  world/ap2/.venv/bin/python -m proxy
curl -s http://127.0.0.1:8030/v1/openapi.json | head -c 200
curl -s http://127.0.0.1:8030/v1/comercios -H "Authorization: Bearer psg_…"
```

Desde ahí, el ejemplo de punta a punta de
[INTEGRACION.md](../docs/INTEGRACION.md#5-flujo-completo-paso-a-paso) anda
igual contra `http://127.0.0.1:8030` (con el mundo levantado).

### Riesgos y fuera de alcance

- **Una sola instancia:** las esperas, la caché de decisiones y los candados
  son del proceso. Dos instancias sobre la misma base no son seguras (salvo
  la atadura de contextos y el reclamo de aprobaciones, que son atómicos).
- **El `merchant.id` firmado no se verifica criptográficamente**: da
  coherencia con la ruta, no autenticidad (AP2 verifica la firma después). A
  futuro: `jwks_url` por comercio.
- **El payload del recibo** lleva valores declarados por el comercio. Sale
  como JWS opaco; INTEGRACION.md avisa que no se le pase a un LLM.
- **`/v1/filtrar` con contenido web:** el filtro está calibrado para
  respuestas de comercios; no hay medición sobre páginas. Cada llamada puede
  costar LLM.
- **Sin cuotas por clave** (solo el máximo de compras activas y los topes de
  cuerpo): un integrador puede gastar la cuota de LLM del operador.
- **Contextos predecibles:** un integrador puede atar hasta 20 `contextId`
  arbitrarios. Contra un comercio que los numera (`ctx-1`, `ctx-2`…), podría
  ocuparle uno a otra compra, que quedaría cortada por `contexto_ajeno`. Es
  denegación de servicio, no fuga (no ve nada de la otra compra). Los
  comercios reales usan UUID; si importa, atar solo los que crea el comercio.
- **Memoria:** una compra de integrador viva (no bloqueada ni vencida,
  dentro de su TTL, o con una espera abierta) nunca sale de memoria: la
  acota el límite de compras activas de cada integrador, no el tope de 200
  de la demo. Las terminadas salen cuando hay más de 200.
- **Activas tras un reinicio:** una fila que quedó en `esperando_*` cuenta
  como activa para el 429 hasta que se hidrata o se lista (ahí se
  normaliza) o hasta su TTL.
- **Fuera de alcance:** verificar la firma ES256 del comercio, comparar
  varios comercios en una compra (toca `contracts/`), `Politica` por
  integrador, panel para integradores, facturación, persistir la cola de
  webhooks y recargar la configuración en caliente.

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
| `fuentes.py` | Intent y ficha de la demo (`ficha_del_intent`, con atributos), catálogo público (con su fuente), `intent_ap2(ficha)` y `ap2_check` con el tope de la compra |
| `config.py` | Variables de entorno (también las `GATEWAY_*`), chequeo de loopback y `verificar_gateway` |
| `persistencia.py` | Registro opcional en Supabase (veredictos y aprobaciones), en segundo plano y sin cortar el flujo si falla |
| `compras.py` | Etapa 2: registro de compras en memoria (pedido, parámetros autorizados, contexto, oportunidad, decisión) y las dos esperas (`Compra.nueva_espera()` abre otra para cada repregunta). Las compras de integrador (`origen="api"`) además se guardan en el almacén del gateway y se restauran con `Compra.desde_fila` (el comentario de cabecera "todo vive en memoria" vale para la demo) |
| `gateway.py` | `NucleoProxy`: lo que la API `/v1` le pide al proxy (aislamiento por integrador, comercios permitidos, 503 si el almacén falla, aprobaciones con las reglas de siempre) |
| `api_v1.py` | Rutas `/v1`, validación estricta (ficha con tope, requisitos que el Guard sabe chequear) y errores |
| `vistas.py` | Lo que ve el integrador de una compra (`VistaCompra`), sin texto del vendedor |
| `openapi.py` | OpenAPI 3.1 de `/v1` y la página `/v1/docs` (HTML sin scripts) |
| `claves.py` | Claves `psg_` (sha256), registro de integradores, middleware `AutenticacionGateway` y la CLI `python -m proxy.claves` |
| `comercios.py` | Registro de comercios (`/a2a/m/{id}`, sin URL libre) y la política con sus `merchant_ids` |
| `webhooks.py` | `Despachador`: webhooks firmados (HMAC con `t`), reintentos, SSRF con IP fijada; `verificar_firma` para el receptor |
| `estado.py` | Almacén del gateway: `EstadoMemoria` (default) y `EstadoSupabase` (`GATEWAY_ESTADO=supabase`) |
| `sin_estado.py` | `/v1/evaluar` (el Guard sobre un checkout que manda el integrador) y `/v1/filtrar` |
| `tipos_gateway.py` | Tipos compartidos del gateway (config, errores, `vista_veredicto`, protocolos) |
| `subir_benchmark.py` | `python -m proxy.subir_benchmark results/*.json`: sube corridas del benchmark a Supabase |
| `sql/001_persistencia.sql` | Tablas de Supabase |
| `sql/003_gateway.sql` | Tablas del estado del gateway (`gw_*`); se aplica a mano |
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

Tests del gateway de producción (sin red ni Supabase reales: el almacén y el
receptor de webhooks se inyectan):

| Archivo | Qué cubre |
|---|---|
| `test_claves.py`, `test_comercios.py` | Formato y verificación de claves, middleware por oyente, CLI; registro de comercios y URLs |
| `test_gateway_revision.py` | Regresiones de la revisión adversarial del gateway: desalojo de memoria, aprobaciones solo del dueño, GET de `/guard/*` sin token, escrituras en orden, listado tras un reinicio y límite concurrente |
| `test_webhooks.py` | Firma, reintentos, SSRF (IPs no globales, IP fijada, sin redirecciones), cola y registro de entregas |
| `test_estado.py`, `test_estado_supabase.py`, `test_compras_api.py` | Almacén en memoria y PostgREST (con `MockTransport`), compras de API y su normalización tras un reinicio |
| `test_api_v1.py`, `test_vistas.py` | Validación de `/v1`, errores, OpenAPI y que nada del vendedor salga en las vistas |
| `test_sin_estado.py` | `/v1/evaluar` y `/v1/filtrar` (fail-closed) |
| `test_gateway_integracion.py` | Punta a punta en proceso: compra por `/v1`, A2A con `X-Compra-Id`, veredicto, webhooks, aprobación y una clave generada con la CLI |
| `test_gateway_a2a.py` | Tráfico A2A de integrador: errores, atadura de contextos, comercios, headers que no llegan al comercio |
| `test_gateway_reinicio.py` | Reinicio con `EstadoMemoria(durable=True)` compartido entre dos apps: esperas cerradas del lado seguro y aprobación de un solo uso |
| `test_gateway_main.py` | `python -m proxy` con el gateway: validaciones de arranque y los dos sockets |

El ayudante común es `tests/gateway_ayudantes.py` (dos comercios falsos,
integradores con claves generadas, receptor de webhooks). Para simular el
oyente público en un test, el cliente usa `base_url="http://gw.test:8030"`
(el middleware lee el puerto de la URL).
