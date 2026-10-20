# Integrar KaiAI en tu agente de compras

> Para equipos que tienen un agente de compras (AP2 sobre A2A) y quieren que
> cada compra pase por KaiAI antes de que el usuario la firme. El
> operador del gateway (quien lo levanta) tiene su guía en
> [`proxy/README.md`](../proxy/README.md#gateway-de-producción) y
> [`deploy/README.md`](../deploy/README.md).

## 1. Qué es

KaiAI es un **gateway** que se pone entre tu agente de compras y el
agente del comercio. Tu agente le habla al gateway como si fuera el comercio
(mismo A2A, mismos mensajes de AP2) y el gateway reenvía todo. En el camino
revisa cada respuesta del comercio con un filtro de seguridad y, cuando llega
el checkout, lo verifica contra lo que autorizó tu usuario y contra datos
públicos del comercio. Si la compra no cumple, el checkout no le llega a tu
agente y no hay nada para firmar.

Vos le decís al gateway qué autorizó el usuario (qué compra, tope de gasto,
moneda y lo que tiene que cumplir) **antes** de que tu agente empiece a
negociar. Ese pedido queda fijo: nada de lo que diga el comercio ni tu propio
modelo lo cambia. Solo lo cambia tu usuario, si acepta una propuesta que le
mostramos.

### Qué garantiza y qué no

- **La garantía es la verificación antes de la firma.** Es código
  determinístico: compara el checkout firmado por el comercio con los
  parámetros que autorizó el usuario y con fuentes que el comercio no
  controla (su catálogo público, la ficha del fabricante). No lee el texto
  libre del vendedor.
- **El filtro es defensa en profundidad.** Corta temprano las respuestas que
  intentan manipular a tu agente, pero la seguridad no depende de que
  detecte todo.
- **No firma ni paga por vos.** El gateway solo deja pasar o retiene el
  checkout. La firma sigue en tu lado, con AP2.
- **No protege lo que no pasa por él.** Si tu agente habla con un comercio
  directo, o lee páginas por su cuenta, eso no se revisa (para lo segundo
  está [`/v1/filtrar`](#9-v1filtrar-lo-que-tu-agente-lee-fuera-del-gateway)).
- **No verifica la firma criptográfica del comercio.** Comprueba que el
  checkout sea coherente con el comercio de la ruta; la firma la verifica AP2
  después.

## 2. Dos maneras de usarlo

| Modo | Qué hacés | Qué te da |
|---|---|---|
| **Gateway completo** (recomendado) | Creás una compra por `/v1`, y tu agente usa la URL A2A del gateway en lugar de la del comercio | Filtro sobre cada respuesta, pregunta del "para qué", propuestas cuando una oferta cubre la necesidad, verificación del checkout, retención de lo dudoso, recibo firmado y webhooks |
| **Mínimo** (`POST /v1/evaluar`) | Tu agente habla directo con el comercio. Antes de firmar, nos mandás el checkout y la ficha | Solo la verificación del checkout y el recibo. Sin filtro, sin estado, sin aprobaciones |

En los dos modos podés usar además `POST /v1/filtrar` para revisar contenido
que tu agente lee fuera del gateway (resultados de búsqueda, páginas web,
salidas de herramientas).

## 3. Alta

El operador te da:

- **Una clave de API** con la forma `psg_<kid>_<secreto>`. Se muestra una
  sola vez: guardala como un secreto. El gateway no la guarda en claro.
- **La lista de comercios** que podés usar. `GET /v1/comercios` te la
  devuelve con la URL A2A de cada uno.
- **La URL base del gateway**, por ejemplo `https://gw.example.com`.
- **Opcional, un webhook:** vos le das una URL `https://` de tu backend y el
  operador te da un secreto `whsec_…` para verificar las firmas.

La especificación completa está en `GET /v1/openapi.json` (OpenAPI 3.1) y una
versión legible en `GET /v1/docs`. Los dos son públicos.

### Si el gateway corre en Vercel

El operador puede publicar el gateway en Vercel
([`deploy/vercel/README.md`](../deploy/vercel/README.md)). La API y las rutas
son las mismas; cambia esto:

- **La URL base es `https://`**, la del dominio de producción que te da el
  operador (por ejemplo `https://<proyecto>.vercel.app`). Usá esa: las URLs
  de cada deploy pueden pedir login de Vercel.
- **Cada pedido dura como mucho 300 s.** Si se pasa, Vercel responde 504
  (`FUNCTION_INVOCATION_TIMEOUT`) y no hay nada para firmar. Por eso las
  esperas son más cortas: la pregunta del porqué hasta **60 s** y una
  propuesta hasta **150 s**, y una llamada A2A nunca pasa de **270 s**. Tu
  usuario tiene que contestar dentro de esos plazos (§12).
- **Cuerpo de hasta 4,5 MB** por pedido y por respuesta (más, 413 de Vercel
  antes de llegar al gateway).
- **Latido durante una pausa.** Una conexión sin bytes se corta a los 60 s,
  así que un pedido A2A retenido (el porqué, una propuesta) empieza a
  responder a los 15 s: el cuerpo JSON puede traer **espacios al principio**
  (sigue siendo JSON válido) y el stream SSE, comentarios `: latido`. Como
  el status `200` ya salió, si el final es un error llega como error
  JSON-RPC, con el status que iba a tener en `error.data.http_status`.
- **Varias instancias.** Tus pedidos pueden caer en instancias distintas: la
  compra y sus esperas viven en la base, así que contestar el porqué, decidir
  o aprobar funciona desde cualquier pedido, aunque la búsqueda siga abierta
  en otra.
- **`tasks/get` y `tasks/resubscribe` de un checkout se bloquean si caen en
  otra instancia** que la que evaluó la tarea: la decisión del Guard sobre
  esa tarea vive en la memoria de la instancia que la evaluó, y una tarea
  con `ap2.checkout` que el gateway no evaluó nunca pasa (fail-closed). Para
  el checkout, mandá otra vez `create_checkout` (se evalúa de nuevo).
- **Webhooks en línea:** 2 intentos de 5 s (con 1 s entre medio) antes de
  que el gateway responda el pedido que los disparó; no hay reintentos en
  segundo plano (ver §7). Lo que no se entregó ahí no se vuelve a intentar:
  reconciliá con `GET /v1/compras/{id}`.

## 4. Autenticación

Todo pedido lleva la clave en el header `Authorization`:

```
Authorization: Bearer psg_a1b2c3d4e5f6_…
```

Vale para `/v1/*` y para el tráfico A2A (`/a2a/m/{comercio}`). Solo
`/v1/public-key`, `/v1/openapi.json` y `/v1/docs` no la piden.

- Sin clave: 401 `no_autenticado`. Con una clave que no existe o está dada de
  baja: 401 `clave_invalida`. También es `clave_invalida` una clave mal
  presentada (`Token psg_…`, la clave sola, sin el espacio después de
  `Bearer`): nunca pasa como un pedido sin clave.
- **Tus compras son tuyas.** Una compra de otro integrador da 404
  `compra_desconocida`, igual que una que no existe.
- **La clave nunca llega al comercio.** El gateway saca `Authorization` y
  `X-Compra-Id` antes de reenviar.

## 5. Flujo completo, paso a paso

Usamos `GW=https://gw.example.com` y `CLAVE=psg_…`.

### 5.1 Crear la compra con lo que autorizó el usuario

```sh
curl -s -X POST "$GW/v1/compras" \
  -H "Authorization: Bearer $CLAVE" -H "Content-Type: application/json" \
  -d '{
    "pedido": "20 computadoras para el lunes",
    "comercio": "merchant_1",
    "ficha": {
      "requisitos": [
        {"campo": "precio",   "operador": "maximo", "valor": 20000, "obligatorio": true},
        {"campo": "moneda",   "operador": "igual",  "valor": "USD", "obligatorio": true},
        {"campo": "cantidad", "operador": "minimo", "valor": 20,    "obligatorio": true}
      ]
    },
    "intencion": {"preguntar": true},
    "referencia": "ord-123"
  }'
```

Respuesta 201 (abreviada):

```json
{"compra_id": "cmp-0123456789abcdef", "referencia": "ord-123",
 "comercio": "merchant_1", "a2a_url": "https://gw.example.com/a2a/m/merchant_1",
 "estado": "creada", "pendiente": null, "veredictos": [], "aprobaciones": [], "…": "…"}
```

Lo que tenés que saber de la ficha:

- **Siempre lleva tope:** un `precio` `maximo` obligatorio mayor que 0 y una
  `moneda` `igual` obligatoria (código ISO de 3 letras). Sin eso, 400
  `ficha_sin_tope`: una autorización sin tope de gasto no autoriza nada.
- **Solo lo que el Guard sabe verificar.** `campo`: `precio`, `moneda`,
  `categoria`, `uso`, `talle`, `marca`, `cantidad`, `entregas`, `comercio` o
  `atributo` (con el nombre en `atributo`, por ejemplo `"memoria"`, y si hace
  falta `unidad`). `operador`: `igual`, `maximo`, `minimo`, `incluye` o
  `cubre` (`cubre` lleva un cronograma: lista de `{"cantidad", "fecha"}`).
  Cualquier otra cosa da 400 `requisito_no_soportado`: nunca se descarta en
  silencio.
- `obligatorio: false` es un preferido: suma en el ranking, pero no bloquea.
- `categoria_taxonomia` (opcional) es el ID de la taxonomía de productos de
  Google, solo dígitos. Con él, el Guard aplica la categoría: una oferta
  fuera de esa rama, o más general que ella, escala; dentro de la rama, con
  el dato solo declarado por el comercio, da aviso. Sin
  `categoria_taxonomia` no hay chequeo de categoría.
- **Precio por unidad:** si la oferta declara `quantity` mayor que 1 en
  `demo.offer_details`, el `amount` del ap2.cart es el total y el Guard lo
  compara con el precio público por unidad multiplicado por `quantity`. Una
  oferta con varias unidades y una ficha sin `cantidad` escala.
- La ficha entera pesa como mucho 32 KB en JSON compacto (sobra para 50
  requisitos); si no, 400 `validacion` en `ficha`.
- Claves desconocidas en el cuerpo dan 400 `validacion`, con la lista de
  problemas en `detalle.campos`.

**`intencion`** decide si el gateway le pregunta a tu usuario para qué quiere
lo que pide (con eso puede proponerle alternativas que cubren la necesidad):

| Mandás | Qué pasa |
|---|---|
| `{"porque": "para un aula que arranca el lunes"}` | Usa ese porqué, sin preguntar |
| `{"preguntar": true}` | En la primera búsqueda pregunta (webhook `compra.pregunta`) y espera tu respuesta |
| nada | Sigue el pedido literal, sin propuestas |

Si el gateway no tiene la capa de intención activa, `intencion_activa` viene
en `false` y todo es literal.

**Una compra = un comercio.** Para comparar vendedores, creá una compra por
cada uno.

### 5.2 Apuntar tu agente al gateway

Tu agente usa la `a2a_url` de la compra en lugar de la URL del comercio, y
manda dos headers en **todos** los pedidos A2A de esa compra:

```
Authorization: Bearer psg_…
X-Compra-Id: cmp-0123456789abcdef
```

La Agent Card (`{a2a_url}/.well-known/agent-card.json`) viene reescrita con
la URL del gateway, así el cliente A2A no se saltea el gateway. Con
`a2a-sdk` (Python), alcanza con pasarle un `httpx.AsyncClient` con esos
headers:

```python
import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory

http = httpx.AsyncClient(
    headers={
        "Authorization": f"Bearer {CLAVE}",
        "X-Compra-Id": compra["compra_id"],
        "X-A2A-Extensions": "https://github.com/google-agentic-commerce/ap2/v1",
    },
    # La pregunta del porqué y una propuesta retienen la llamada: hasta ~10 min.
    timeout=httpx.Timeout(600.0, connect=10.0),
)
card = await A2ACardResolver(httpx_client=http, base_url=compra["a2a_url"]).get_agent_card()
cliente = ClientFactory(ClientConfig(httpx_client=http)).create(card)
```

Usá un cliente HTTP por compra (el `X-Compra-Id` cambia).

### 5.3 Buscar (`find_products`)

Tu agente manda el mismo mensaje de siempre. Con curl, para ver la forma:

```sh
curl -s -X POST "$GW/a2a/m/merchant_1" \
  -H "Authorization: Bearer $CLAVE" -H "X-Compra-Id: cmp-0123456789abcdef" \
  -H "Content-Type: application/json" \
  -H "X-A2A-Extensions: https://github.com/google-agentic-commerce/ap2/v1" \
  -d '{"jsonrpc": "2.0", "id": "1", "method": "message/send",
       "params": {"message": {"kind": "message", "messageId": "m-1", "role": "agent",
         "parts": [
           {"kind": "text", "text": "Find products that match the user request."},
           {"kind": "data", "data": {"catalog_search": "20 notebooks para el lunes"}},
           {"kind": "data", "data": {"shopping_agent_id": "trusted_shopping_agent"}}
         ]}}}'
```

(Las partes `data` son las que espera el comercio. El gateway solo lee
`catalog_search` y, en el checkout, `cart_id`.)

Lo que puede pasar mientras esa llamada está abierta:

1. **La pregunta del porqué** (si pediste `preguntar`). El gateway retiene la
   llamada, te manda el webhook `compra.pregunta` y la compra queda en
   `esperando_contexto`, con `pendiente: {"tipo": "porque", "pregunta", "vence"}`.
   Mostrale la pregunta a tu usuario y contestá:

   ```sh
   curl -s -X POST "$GW/v1/compras/cmp-0123456789abcdef/respuesta" \
     -H "Authorization: Bearer $CLAVE" -H "Content-Type: application/json" \
     -d '{"respuesta": "Es para un aula; si llegan 10 el lunes y 10 el jueves, sirve."}'
   ```

   O `{"omitir": true}`. Si no contesta antes de `vence`, sigue el pedido
   literal.

2. **El filtro.** Si la respuesta del comercio intenta manipular a tu agente,
   tu agente recibe una tarea `failed` con un texto fijo y **nada** del
   comercio. La compra queda `bloqueada` (webhook `compra.bloqueada`).

3. **Una propuesta.** Si ninguna oferta cumple lo autorizado pero una cubre
   la necesidad, el gateway retiene la respuesta y te manda
   `compra.oportunidad`. La compra queda en `esperando_decision`, con
   `pendiente.oportunidad` (qué cambia, precio, por qué). **Decide tu
   usuario:**

   ```sh
   curl -s -X POST "$GW/v1/compras/cmp-0123456789abcdef/decision" \
     -H "Authorization: Bearer $CLAVE" -H "Content-Type: application/json" \
     -d '{"oportunidad_id": "opo-…", "decision": "aceptar"}'
   ```

   El `oportunidad_id` es el `pendiente.oportunidad.id` (empieza con `opo-`).
   La respuesta ya trae la compra con la decisión aplicada (`aceptada` o
   `rechazada`, y la ficha nueva si aceptó).

   Si acepta, la respuesta le llega a tu agente con un artefacto
   `guard.parametros_autorizados` y la ficha de la compra pasa a ser esa. Si
   rechaza, pasa igual y siguen los parámetros originales. **Si nadie
   contesta antes de `vence`, no se compra nada:** tu agente recibe una
   tarea `failed` y la compra queda `vencida`. El silencio no es
   autorización.

Si nada de eso aplica, la respuesta del comercio llega intacta.

**Con `message/stream` no se buscan propuestas.** En un stream la respuesta
del comercio sale evento por evento (después del filtro): no se puede
retener para consultar a tu usuario. La pregunta del porqué y el filtro
funcionan igual; la compra queda `sin_oportunidad` y el Guard evalúa el
checkout con la ficha que autorizaste. Si querés propuestas, buscá con
`message/send`.

### 5.4 Pedir el checkout (`create_checkout`)

Tu agente pide el checkout del cart elegido, con el `contextId` de la
búsqueda, como siempre. El gateway verifica el checkout firmado **antes** de
dárselo y agrega a la tarea un artefacto con el veredicto:

```json
{"guard.veredicto": {"status": "aprobado", "motivo": "…", "cart_id_elegido": "cart_2",
                     "chequeos": [{"tipo": "cumplimiento", "resultado": "ok"}]}}
```

| `status` | Qué recibe tu agente | Qué hacés |
|---|---|---|
| `aprobado`, `con_avisos` | La tarea intacta, con el `ap2.checkout` del comercio byte a byte | Firmar (con `con_avisos`, mostrale el motivo al usuario) |
| `escalar` | Tarea `failed`, **sin** el checkout ni nada más del comercio (solo el `guard.veredicto` y el texto): "requiere aprobación humana" | Mostrarle la evidencia a tu usuario y, si aprueba, ver abajo |
| `bloqueado` | Tarea `failed`, sin el checkout ni nada más del comercio | No hay nada que firmar. Tu agente puede elegir otro cart |

**Regla para tu agente: firmá solo si la tarea trae el `ap2.checkout` y el
`guard.veredicto` dice `aprobado` o `con_avisos`.** El texto de la tarea es
siempre `KaiAI: <status>. <motivo>`, armado por el gateway, sin
texto del comercio.

**`escalar` y la aprobación humana.** Llega el webhook
`compra.requiere_aprobacion` y la compra muestra
`pendiente: {"tipo": "aprobacion", "contexto_id", "cart_id", "monto", "centavos", "moneda", "motivo", "chequeos"}`.
Si tu usuario aprueba:

```sh
curl -s -X POST "$GW/v1/compras/cmp-0123456789abcdef/aprobaciones" \
  -H "Authorization: Bearer $CLAVE" -H "Content-Type: application/json" \
  -d '{"cart_id": "cart_2", "contexto_id": "ctx-…"}'
```

201 con la aprobación (200 si ya existía la misma). Después tu agente
**reintenta `create_checkout`**. El gateway vuelve a evaluar y deja pasar el
checkout intacto solo si da otra vez `escalar` y el total firmado es el mismo
que se aprobó. La aprobación:

- es de un solo uso y vale para ese contexto, ese cart y ese total firmado;
- nunca hace pasar un `bloqueado`.

### 5.5 Consultar la compra

```sh
curl -s "$GW/v1/compras/cmp-0123456789abcdef" -H "Authorization: Bearer $CLAVE"
```

Devuelve el estado, la ficha vigente, lo pendiente, los veredictos (con el
recibo firmado) y las aprobaciones. Es la misma vista que va en los
webhooks: si te perdiste uno, el GET tiene todo.

Estados: `creada` → `esperando_contexto` → `negociando` →
`esperando_decision` → `aceptada` | `rechazada` | `sin_oportunidad`, y los
terminales `bloqueada` (lo cortó el filtro) y `vencida` (nadie decidió una
propuesta a tiempo). Con una compra `bloqueada` o `vencida`, o pasadas 24 h
desde que se creó, el tráfico A2A da 409: creá otra.

### 5.6 Qué necesita el Guard de tu comercio

- **`ap2.cart`** (AP2 estándar): `cart_id`, `item_label`, `amount` (el total) y
  `currency`. De ahí toma el Guard el precio, la moneda y el `cart_id` que se
  cobra.
- **`demo.offer_details`** (extensión de KaiAI, no es AP2): una entrada por
  `cart_id` con lo que el comercio **declara**: `sku`, `title`, `brand`,
  `category`, `use`, `stock`, `sizes` y, si los tiene, `quantity` (unidades),
  `deliveries` (`[{quantity, date}]`, el cronograma), `gtin`,
  `categoria_taxonomia` y `atributos` (nombre → `{valor, unidad}`). El
  texto libre (`description`, `item_label`) no decide nada. Sin esta lista, el
  Guard no tiene nada declarado: un requisito obligatorio sobre un dato que
  falta (marca, categoría, uso, talle, un atributo) escala.
- **Catálogo público por SKU**: `GET <CATALOGO_URL>/products/<sku>` (y, si no
  alcanza, `GET <CATALOGO_URL>/products`). Es la fuente que verifica precio y
  datos. Sin catálogo, todo queda "solo declarado" y lo obligatorio escala. Un
  catálogo que dice ser de otro comercio que el firmado en el checkout no se
  usa.
- **Ficha del fabricante por GTIN**: si la oferta trae `gtin`, el proxy consulta
  la ficha del fabricante (`FABRICANTE_FUENTE`: `fixture` por defecto, rotulado
  como datos de prueba; `icecat` apagado). Sin ficha, o con falla, lo
  obligatorio escala.

### 5.7 Probarlo sin LLM: `smoke_v1_general`

`proxy/scripts/smoke_v1_general.py` recorre la integración de punta a punta por
`/v1`, sin LLM de nuestro lado: crea la compra (`POST /v1/compras`), busca
(`find_products`) y pide el checkout (`create_checkout`) con `X-Compra-Id`, e
imprime el `status` del veredicto. Por defecto arma un gateway de prueba en el
mismo proceso y necesita el comercio y el catálogo levantados (`bash
deploy/start_all.sh general`, en :8001 y :8011). El `create_checkout` del
comercio del sample usa su propio LLM (su `GOOGLE_API_KEY`).

```sh
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m proxy.scripts.smoke_v1_general \
    --pedido "un monitor de 27 pulgadas" --tope 400 --categoria 305 --esperado aprobado
```

Contra un gateway ya levantado: `--gateway http://localhost:8030 --comercio
merchant_1`, con la clave en `KAIAI_CLAVE` (o `--clave`). `--esperado` sale con
error si el `status` no es ese.

En esta medición (2026-10-19): una notebook (categoría 328) → `aprobado`; una
tablet "a tiempo" del comercio general (`general_otra_categoria`) → `escalar`
por categoría; un pedido en ARS → `bloqueado` por moneda.

### 5.8 Probarlo desde el navegador: la consola web

**Consola web: https://kaiai-motor.vercel.app/consola** (la raíz `/` redirige
ahí). Hace en el navegador lo mismo que tu agente: pegás tu clave `psg_…`,
creás la compra (pedido, comercio, tope y moneda, y opcionales cantidad, plazo
y categoría), buscás, contestás el porqué y la propuesta con su cuenta
regresiva, pedís el checkout, ves el `guard.veredicto`, aprobás un `escalar`
y verificás la firma del recibo en el navegador contra `GET /v1/public-key`.
También tiene una pestaña para probar `/v1/filtrar`.

- **No tiene permisos propios:** cada paso es una llamada a `/v1` o a
  `/a2a/m/{comercio}` con tu clave, y la API decide igual que con tu agente.
- **La clave** queda en la memoria de la pestaña (o en `sessionStorage` si
  marcás "recordar en esta pestaña") y solo viaja a ese mismo origen: la
  página tiene una CSP con `connect-src 'self'`, sin scripts de terceros.
- **El comportamiento del comercio simulado** (`demo_scenario`) solo lo
  entiende el comercio de la simulación; un comercio real lo ignora.
- Lo que escribe el comercio (por ejemplo `item_label`) se muestra como texto,
  nunca como HTML: puede traer una inyección.

## 6. Referencia de la API

Todo es JSON. Los POST exigen `Content-Type: application/json`. Las fechas
van en ISO 8601 UTC. Los errores tienen siempre la misma forma:

```json
{"error": {"codigo": "validacion", "mensaje": "…", "detalle": {"campos": [{"campo": "ficha.requisitos", "problema": "…"}]}}}
```

| Método y ruta | Para qué | Respuesta |
|---|---|---|
| `POST /v1/compras` | Crear una compra (§5.1) | 201 `VistaCompra` |
| `GET /v1/compras?limite=50&estado=…` | Tus compras, la más nueva primero (`limite` de 1 a 200) | `{"compras": [VistaCompra]}` |
| `GET /v1/compras/{id}` | Una compra | `VistaCompra` |
| `POST /v1/compras/{id}/respuesta` | `{"respuesta": "…"}` (hasta 1000 caracteres) u `{"omitir": true}`. Solo en `esperando_contexto` | `VistaCompra` |
| `POST /v1/compras/{id}/decision` | `{"oportunidad_id", "decision": "aceptar"\|"rechazar"}`. Solo en `esperando_decision` | `VistaCompra` |
| `POST /v1/compras/{id}/aprobaciones` | `{"cart_id", "contexto_id"?}`. Solo con un `escalar` retenido | 201 o 200 `VistaAprobacion` |
| `POST /v1/evaluar` | Modo mínimo (§8) | `VistaVeredicto` + `comercio_firmado` |
| `POST /v1/filtrar` | Filtrar contenido externo (§9) | `{"pasa", "status", "motivo", "senales"}` |
| `GET /v1/comercios` | Los comercios que podés usar | `{"comercios": [{"id", "nombre", "a2a_url"}]}` |
| `GET /v1/webhooks/entregas?limite=50` | Las últimas entregas de tu webhook | `{"entregas": [...]}` |
| `POST /v1/webhooks/prueba` (cuerpo `{}`) | Manda un `ping` a tu webhook, un solo intento | La entrega |
| `GET /v1/public-key` | Clave pública de los recibos (sin clave de API) | `{"kid", "jwk", "keys": [jwk]}` |
| `GET /v1/openapi.json`, `GET /v1/docs` | La especificación (sin clave de API) | — |

`VistaVeredicto`:

```json
{"status": "escalar", "motivo": "…", "cart_id": "cart_2", "llega_a_firma": false,
 "chequeos": [{"tipo": "evidencia", "resultado": "escalar"}],
 "recibo": {"id": "rcb-…", "jws": "eyJ…", "kid": "guard-key-1"},
 "contexto_id": "ctx-…", "ts": "…", "evaluador": "guard"}
```

**Qué nunca te mostramos:** el texto libre del comercio (descripciones,
`item_label`, mensajes de sus tareas), el `checkout_jwt` ni los mandatos.
Los identificadores que escribe el comercio (`cart_id`, `sku`) salen solo si
tienen un formato seguro (si no, `null`). En una propuesta, un valor de
texto que no es corto y simple sale como `null` con `"omitido": true`. El
título de una propuesta es el **público** (catálogo o fabricante), nunca el
que declara el vendedor.

## 7. Webhooks

El gateway le avisa a tu backend cuando necesita a tu usuario o cuando pasa
algo importante:

| Tipo | Cuándo |
|---|---|
| `compra.pregunta` | Hay que contestar el porqué |
| `compra.oportunidad` | Hay una propuesta para decidir |
| `compra.requiere_aprobacion` | Un checkout quedó retenido por `escalar` |
| `compra.veredicto` | El Guard evaluó un checkout (cualquier status) |
| `compra.bloqueada` | El filtro cortó la compra, venció una consulta o el gateway no pudo evaluar |
| `ping` | `POST /v1/webhooks/prueba` |

**Cuerpo** (JSON):

```json
{"id": "whe-…", "tipo": "compra.oportunidad", "creado": "…", "integrador": "acme",
 "compra": { "…la VistaCompra del momento, la misma del GET…" }, "datos": {}}
```

**Headers:** `X-Guard-Evento` (el tipo), `X-Guard-Entrega` (el `id`, igual en
todos los reintentos) y `X-Guard-Firma: t=<unix>,v1=<hex>`, con
`v1 = HMAC-SHA256(secreto, "<t>." + cuerpo)`.

### Verificar la firma

Verificá **siempre** antes de usar el cuerpo, con los bytes crudos (antes de
parsear el JSON):

```python
import hashlib, hmac, time

TOLERANCIA_S = 300

def verificar_webhook(secreto: str, cuerpo: bytes, cabecera: str) -> bool:
    """True si `X-Guard-Firma` es válida y reciente. `cuerpo`: los bytes
    crudos del POST, antes de parsear el JSON."""
    partes = dict(p.strip().split("=", 1) for p in cabecera.split(",") if "=" in p)
    t, recibida = partes.get("t", ""), partes.get("v1", "")
    # isascii: "²".isdigit() es True, pero int("²") lanza.
    if not (t.isascii() and t.isdigit()) or abs(time.time() - int(t)) > TOLERANCIA_S:
        return False
    esperada = hmac.new(
        secreto.encode(), f"{t}.".encode() + cuerpo, hashlib.sha256
    ).hexdigest()
    # En bytes: con un str que no es ASCII, compare_digest lanza en vez de dar False.
    return hmac.compare_digest(esperada.encode(), recibida.encode("utf-8", "replace"))
```

Además:

- **Deduplicá por el `id` del cuerpo** (es el mismo valor que
  `X-Guard-Entrega`, pero el header no está firmado: quien reenvía una
  entrega dentro de la tolerancia puede cambiarlo; el cuerpo no). Un
  reintento trae el mismo id (y una firma nueva: `t` es el momento de cada
  intento). Por lo mismo, el tipo leelo de `tipo` en el cuerpo, no de
  `X-Guard-Evento`.
- **El orden no está garantizado.** Ordená por `compra.actualizada`.
- **Contestá 2xx rápido** (cada intento tiene 10 s en total, cuerpo de la
  respuesta incluido) y procesá aparte.
- **Reintentos:** hasta 6 intentos, con espera creciente (1 s, 4 s, 16 s,
  1 min, 5 min, con algo de azar), ante error de red, timeout, 5xx, 408 o
  429. Otro 4xx o una redirección cuentan como falla y no se reintentan.
  **En Vercel no:** son 2 intentos de 5 s, con 1 s entre medio, antes de
  responder el pedido que los disparó (§3), y después no hay más.
- **Solo `https`** y a una IP pública: un webhook que resuelve a una IP
  privada se descarta (`destino_no_permitido`).
- **No sobreviven a un reinicio del gateway.** Si el gateway se reinicia con
  entregas pendientes, se pierden. **Reconciliá con
  `GET /v1/compras/{id}`** (o `GET /v1/compras`) cuando algo no te cierra:
  esa es la fuente de verdad.

`GET /v1/webhooks/entregas` muestra cómo le fue a cada entrega (`estado`:
`pendiente`, `entregada`, `fallida` o `descartada`, y `error` con un código
fijo: `red`, `timeout`, `http_4xx`, `http_5xx`, `redireccion`,
`destino_no_permitido`, `cola_llena`, `cerrado`). `cola_llena` quiere decir
que tu webhook ya tiene demasiadas entregas pendientes (200): mientras no
contesta, las nuevas se descartan.

## 8. Modo mínimo: `POST /v1/evaluar`

Para cuando tu agente no pasa por el gateway. Antes de firmar, mandás la
ficha autorizada y el checkout tal como te lo dio el comercio:

```sh
curl -s -X POST "$GW/v1/evaluar" \
  -H "Authorization: Bearer $CLAVE" -H "Content-Type: application/json" \
  -d '{
    "ficha": {"pedido": "20 computadoras para el lunes",
              "requisitos": [
                {"campo": "precio", "operador": "maximo", "valor": 20000, "obligatorio": true},
                {"campo": "moneda", "operador": "igual",  "valor": "USD", "obligatorio": true}
              ]},
    "checkout": {"checkout_jwt": "eyJ…", "checkout_hash": "…", "cart_id": "cart_2",
                 "amount": 19800.0, "currency": "USD"},
    "ofertas": {"carts": [ … ], "offer_details": [ … ]},
    "comercio": "merchant_1"
  }'
```

- `checkout`: el data part `ap2.checkout` sin tocar.
- `ofertas` (opcional): lo que te mostró el comercio en la búsqueda. Con
  ofertas, el Guard además compara el cart elegido con los otros. Sin
  ofertas, evalúa solo lo **firmado** (el total del JWT).
- `comercio` (opcional): uno de tus comercios. Da el catálogo público para
  verificar y los `merchant.id` válidos. Sin `comercio`, se evalúa sin
  catálogo: todo queda "solo declarado" y lo obligatorio escala.

Respuesta: un `VistaVeredicto` (con `contexto_id: null`) más
`comercio_firmado` (el `merchant.id` del checkout).

**El recibo atestigua solo lo que mandaste.** En este modo el gateway no vio
la negociación: el recibo firma el veredicto sobre la ficha, el checkout y
las ofertas de **tu** pedido (más el catálogo del comercio, si lo indicaste).
No prueba que esas fueran las ofertas que el comercio le mostró a tu agente
ni que la ficha sea la que autorizó tu usuario: eso queda de tu lado.

**Firmá solo si la respuesta es 200 y `llega_a_firma` es `true`.** Cualquier
otra cosa (error, timeout, `false`) es no firmar. Este modo no tiene estado:
no hay aprobaciones ni retención. Un `escalar` se lo mostrás a tu usuario y
la decisión queda de tu lado.

**Concurrencia:** a lo sumo 2 pedidos en curso por clave entre
`/v1/evaluar` y `/v1/filtrar` (por instancia del gateway; corren en hilos que
comparte todo el tráfico). El tercero responde enseguida 429
`demasiadas_solicitudes` (`detalle.maximo`): reintentá cuando termine uno.

## 9. `/v1/filtrar`: lo que tu agente lee fuera del gateway

El mismo filtro que revisa las respuestas de los comercios, para el contenido
que tu agente lee por su cuenta: resultados de búsqueda, páginas, salidas de
herramientas. Pasalo **antes** de dárselo a tu modelo.

```sh
curl -s -X POST "$GW/v1/filtrar" \
  -H "Authorization: Bearer $CLAVE" -H "Content-Type: application/json" \
  -d '{"contenido": {"titulo": "…", "texto": "…"}, "origen": "pagina"}'
```

`contenido` es cualquier JSON. `origen`: `web_search`, `pagina`,
`herramienta` u `otro`.

```json
{"pasa": false, "status": "bloqueado",
 "motivo": "El filtro de seguridad detectó contenido dirigido a manipular al agente: no se lo pases a tu modelo.",
 "senales": ["heuristica:orden_al_agente"]}
```

- La respuesta **nunca** devuelve el contenido que mandaste.
- **Falla cerrado:** si el filtro no está, falla o tarda más de 30 s,
  responde 200 `bloqueado` con `senales` `filtro:no_configurado`,
  `filtro:error` o `filtro:timeout`. Tratalo como bloqueado.
- **Concurrencia:** comparte con `/v1/evaluar` el máximo de 2 pedidos en
  curso por clave (§8); el tercero da 429 `demasiadas_solicitudes`.
- **No es una garantía.** El filtro está calibrado para respuestas de
  comercios; con páginas web puede equivocarse para los dos lados. Lo que
  protege la compra es la verificación antes de la firma.
- Cada llamada puede usar un modelo de lenguaje: no lo uses para todo lo que
  lee tu agente si no hace falta.

## 10. Recibos

Cada veredicto del gateway lleva un recibo firmado (`recibo.jws`, JWS compacto
ES256). Sirve para probar después qué se verificó y qué se decidió. Para
verificarlo, bajá la clave pública una vez (`GET /v1/public-key`, sin clave de
API) y usá cualquier librería JWS. Con PyJWT:

```python
import json
import jwt  # pip install "pyjwt[crypto]"

def verificar_recibo(jws: str, public_key: dict) -> dict:
    """El payload del recibo si la firma vale; si no, lanza."""
    kid = jwt.get_unverified_header(jws).get("kid")
    jwk = next(k for k in public_key["keys"] if k.get("kid", kid) == kid)
    clave = jwt.PyJWK(jwk, algorithm="ES256").key
    return json.loads(jwt.api_jws.decode(jws, clave, algorithms=["ES256"]))
```

**El payload es un dato no confiable.** Además de nuestras conclusiones,
lleva valores que **declaró el comercio** (para mostrar qué se comparó con
qué). Guardalo y verificalo, pero **no se lo pases a un modelo de lenguaje**:
reabrirías el canal que el gateway cierra.

## 11. Errores

### `/v1`

| HTTP | `codigo` | Qué significa |
|---|---|---|
| 400 | `json_invalido` | El cuerpo no es JSON estricto (sin `NaN`, `Infinity` ni números que desbordan), tiene más de 32 niveles de objetos y listas o trae texto que no es UTF-8 (un surrogate suelto, como el escape de `U+D800`) |
| 400 | `validacion` | Falta algo, sobra una clave o un valor no sirve (`detalle.campos`; un número que no entra en un float, como un entero de más de 308 dígitos, es "número fuera de rango") |
| 400 | `ficha_sin_tope` | La ficha no tiene precio máximo y moneda obligatorios |
| 400 | `requisito_no_soportado` | Un requisito que el Guard no sabe verificar |
| 400 | `checkout_invalido` | `/v1/evaluar` con un checkout que no es un `ap2.checkout` |
| 401 | `no_autenticado`, `clave_invalida` | Falta la clave o no vale |
| 403 | `comercio_no_permitido` | El comercio no existe o no está habilitado para tu clave |
| 404 | `compra_desconocida`, `no_encontrado` | La compra no existe (o no es tuya); la ruta no existe |
| 405 | `metodo_no_permitido` | La ruta existe pero no acepta ese método (los que acepta van en `Allow`) |
| 409 | `estado` | La compra no está esperando eso (`detalle.estado`) |
| 409 | `oportunidad_distinta` | Esa no es la propuesta vigente |
| 409 | `sin_escalar` | No hay un checkout retenido para aprobar |
| 409 | `cart_distinto` | El checkout retenido es de otro cart o contexto |
| 409 | `sin_webhook` | Tu clave no tiene webhook |
| 413 | `cuerpo_demasiado_grande` | Más de 1 MB |
| 415 | `se_espera_json` | Falta `Content-Type: application/json` |
| 429 | `demasiadas_solicitudes` | Ya tenés 2 pedidos en curso entre `/v1/evaluar` y `/v1/filtrar` (`detalle.maximo`); reintentá cuando termine uno |
| 429 | `demasiadas_compras` | Llegaste al máximo de compras activas (`detalle.maximo`): las que todavía no resolvieron la negociación (`creada`, `esperando_contexto`, `negociando`, `esperando_decision`) |
| 503 | `estado_no_disponible` | El gateway no pudo leer o guardar su estado: reintentá |
| 503 | `sin_clave_recibo` | El gateway no tiene clave de recibos publicada |
| 500 | `interno` | Error interno (sin detalle) |

### Tráfico A2A

Si el gateway rechaza un pedido A2A antes de reenviarlo, responde con el
status HTTP y un error JSON-RPC con el código en `data.codigo`:

```json
{"jsonrpc": "2.0", "id": "1",
 "error": {"code": -32001, "message": "KaiAI: la compra está vencida.",
           "data": {"codigo": "compra_vencida"}}}
```

| HTTP | `codigo` | Cuándo |
|---|---|---|
| 400 | `falta_compra` | Sin `X-Compra-Id` o con un formato inválido |
| 400 | `pedido_ilegible` | El cuerpo no es un objeto JSON estricto (`NaN`, `Infinity`, `1e999`…) o el `contextId` no es texto (`id: null`). No se reenvía |
| 403 | `comercio_no_permitido` | El comercio de la ruta no existe o no está habilitado para tu clave (`id: null`) |
| 404 | `compra_desconocida` | La compra no existe o no es tuya |
| 409 | `comercio_distinto` | La compra es de otro comercio |
| 409 | `compra_bloqueada`, `compra_vencida` | La compra terminó o pasaron 24 h |
| 409 | `contexto_ajeno` | El `contextId` del pedido ya es de otra compra |
| 413 | `cuerpo_demasiado_grande` | El pedido supera 5 MB |
| 503 | `estado_no_disponible` | El gateway no pudo leer su estado: reintentá |

El 401 de la clave sale sin JSON-RPC, con la forma de `/v1`. Si el gateway ya
reenvió el pedido y algo sale mal después (el comercio no responde, el
checkout no se puede evaluar), tu agente recibe una tarea `failed` con un
`guard.veredicto` `bloqueado`: nunca un checkout sin verificar.

## 12. Reglas para tu integración

- **Las aprobaciones y las decisiones las toma una persona.** Mostrale a tu
  usuario la pregunta, la propuesta o la evidencia del `escalar`, y mandá lo
  que **él** decidió. Nunca las conteste tu modelo de lenguaje: el gateway no
  puede distinguirlo, y es la única puerta para cambiar lo autorizado.
- **El silencio vence.** Una propuesta sin respuesta vence y no se compra
  nada. Un porqué sin respuesta sigue el pedido literal.
- **Una compra = un comercio.** Para comparar vendedores, una compra por
  cada uno.
- **Vida de una compra: 24 h.** Pasado ese plazo el tráfico A2A da 409
  `compra_vencida`. La compra se sigue pudiendo leer.
- **Timeouts largos en tu cliente A2A.** La pregunta del porqué (hasta 2 min)
  y una propuesta (hasta 7 min) retienen la llamada abierta; el total nunca
  pasa de 570 s. Con un timeout más corto, tu cliente corta, la compra queda
  `vencida` y no se compra nada. En un gateway en Vercel los plazos son 60 s,
  150 s y 270 s ([§3](#si-el-gateway-corre-en-vercel)).
- **Firmá solo lo que pasó.** En el gateway: la tarea trae el `ap2.checkout`
  y el veredicto es `aprobado` o `con_avisos`. En `/v1/evaluar`: 200 y
  `llega_a_firma: true`.

## 13. Límites conocidos

- **Una sola instancia** (salvo en Vercel, [§3](#si-el-gateway-corre-en-vercel),
  donde corre en varias con el estado en la base). El gateway corre como un proceso. Si se reinicia,
  las compras guardadas siguen (si el operador configuró almacenamiento
  durable), pero lo que estaba esperando cierra del lado seguro: una pregunta
  del porqué pendiente sigue literal y una propuesta pendiente queda
  `vencida`. Una aprobación ya usada no se puede volver a usar.
- **Los webhooks pendientes se pierden en un reinicio** (§7): reconciliá
  con GET.
- **No se comparan vendedores dentro de una compra.**
- **Sin cuotas por clave**, salvo el máximo de compras activas (1000 por
  defecto) y los topes de tamaño. Rotar una clave o cambiar comercios
  requiere que el operador reinicie el gateway.
- **La firma del comercio no se verifica en el gateway.** El gateway
  comprueba que el `merchant.id` firmado sea el del comercio de la ruta;
  la firma criptográfica la verifica AP2.
- **Hasta 20 `contextId` por compra.** Un `contextId` queda atado a la
  primera compra que lo usa: si otra compra lo manda, se corta
  (`contexto_ajeno`). Con comercios que generan contextos predecibles, otro
  integrador podría ocupar uno de antemano y cortarte esa conversación (no
  ve nada de tu compra); los comercios reales usan UUID.
