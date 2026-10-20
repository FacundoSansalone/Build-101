# dashboard/ (Dev 3)

Panel de la demo de KaiAI: **la misma compra sin y con Guard lado a lado**, el flujo de
cada compra, el veredicto con sus chequeos, el recibo firmado (verificado en el
navegador), la ficha del pedido, el gráfico del benchmark "de X% a Y%" y, en la
etapa 2, la conversación con **nuestro agente** (vista Agente).

Sin paso de build ni CDN: Starlette + HTML, CSS y JS vanilla (módulos ES).
Gráficos en SVG hecho a mano. Funciona sin internet.

## Cómo levantarlo

Desde la raíz del repo, con el entorno de `world/ap2`:

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m dashboard     # Linux/macOS: world/ap2/.venv/bin/python
# → http://localhost:8020
```

| Variable | Default | Para qué |
|---|---|---|
| `DASHBOARD_PORT` | `8020` | Puerto del panel (también define qué `Host` y `Origin` acepta su API) |
| `DASHBOARD_HOST` | `127.0.0.1` | Interfaz. Abrilo como `localhost`: WebCrypto (verificar el recibo) no anda por IP de red sin HTTPS. **Fuera de loopback no arranca** salvo `DASHBOARD_ALLOW_REMOTE=1` |
| `DASHBOARD_ALLOW_REMOTE` | `0` | `1` para escuchar en la red (no recomendado: muestra mandatos y checkouts completos) |
| `PROXY_URL` | `http://127.0.0.1:8010` | Proxy del Guard. `127.0.0.1` y no `localhost`: en Windows `localhost` prueba primero `::1`, donde el proxy no escucha, y cada intento fallido suma ~2 s (el panel mostraba "proxy desconectado"). El proxy acepta `Host` `127.0.0.1:8010` y `localhost:8010` |
| `GUARD_API_TOKEN_PATH` | `.temp-db/guard_api_token` | Token de la API de control del proxy (lo escribe el proxy en cada arranque). El servidor lo agrega en los POST que reenvía; tiene que ser el mismo archivo que usa el proxy |
| `DASHBOARD_PUBLIC_ORIGINS` · `DASHBOARD_EXTRA_HOSTS` | — | Deploy: dominio público del panel y `Host` extra de un túnel ([`deploy/README.md`](../deploy/README.md)) |
| `DASHBOARD_ACCESS_TOKEN` | — | Panel **publicado** (cualquiera de las dos de arriba, o escuchando fuera de loopback): contraseña de HTTP Basic que pide todo, páginas, `/api/*` y estáticos (usuario cualquiera; al menos 16 caracteres). **Sin ella, publicado no arranca.** En local no se usa |
| `DASHBOARD_PERMITIR_SIN_CLAVE` | `0` | `1` para publicar el panel sin contraseña igual (no recomendado) |

Atajos de teclado: `1`–`7` cambian de vista (no mientras se escribe en un
campo). La vista Agente es la `7`, última, para no correr los atajos de la
demo de la etapa 1; se puede abrir directo con `#agente`. El tema (auto, claro,
oscuro) se cambia con el botón al lado del nombre.

Con el proxy y el dashboard levantados juntos:

```sh
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m proxy       # :8010, escribe .temp-db/guard_api_token
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m dashboard                # :8020, lo lee en cada POST
```

Si se reinicia el proxy, el token cambia y el panel toma el nuevo solo (lo lee
en cada POST). Conviene una sola pestaña del panel abierta: cada una tiene su
SSE y el navegador limita las conexiones por host.

## `/demo`: la demo en una pantalla

`http://localhost:8020/demo` reemplaza a la UI de ADK para la demo: arriba el
diagrama (Vos → tu agente de compras → **KaiAI** con sus etapas
filtro · intención · verificación → comercio, más fabricante/catálogo y AP2),
al medio el relato en lenguaje natural (plantillas en código, nunca un LLM;
`role="log"` con `aria-live`) y abajo **un solo input**. El panel de siempre
queda como "Detalle técnico" (`/`).

- **Input único.** `POST /api/demo/mensaje {"texto"}`: si nuestro agente (el
  intake del proxy) tiene una pregunta pendiente **de esta corrida**, la
  respuesta va al proxy con el token (`/guard/compras/contexto`: respuesta,
  `confirmar` si es el turno de confirmar la ficha y dice solo "sí", u
  `omitir`; o `/guard/compras/decision`: "sí" acepta, "no" rechaza, otra cosa
  da 422). "Solo" quiere decir sin nada más que cortesía ("sí, gracias", "no,
  gracias"): un sí o un no con un número, un plazo o una condición ("sí, pero
  hasta 300", "no, pero mandalas el viernes") en la confirmación va como
  corrección de la ficha y en la decisión da 422, nunca se descarta lo que
  agregó el usuario.
  Si no, va al agente de compras por la API de `adk web` (`ADK_URL`): una
  sesión por compra (`POST /apps/shopping_agent/users/demo/sessions`) y
  `POST /run_sse`, en segundo plano (202). Con el agente trabajando, 409.
- **El pedido con tus palabras.** El primer mensaje de cada compra que va al
  agente se registra antes en el proxy (`POST /guard/compras/pedido
  {"texto"}`, con el token): el intake de la próxima compra lee lo que
  escribiste además de la paráfrasis que el agente le pasa al comercio, y no
  repregunta lo que ya dijiste ("para el lunes", "hasta 20000"). Vence a los
  180 s y se usa una vez; si el proxy no está o no lo acepta, el mensaje va
  igual al agente. Detalle y garantías en
  [proxy/README.md](../proxy/README.md#etapa-2-contexto-filtro-y-oportunidad).
- **Lo que pregunta y entiende nuestro agente.** Las preguntas del intake se
  ven enteras. Al pedir confirmar la ficha, la línea trae **la ficha**
  (`ficha_parcial` del `pedido_detectado`): parámetro, valor y si lo dijiste,
  lo dedujo (confirmalo), está a confirmar o falta, y qué pasa si no
  confirmás. Si el evento trae la ruta de la categoría en la taxonomía de
  Google (`categoria_ruta`, lista o texto con `>`), la ficha suma la fila
  "Categoría" (p. ej. "Electrónica › Computadoras › Laptops") después del
  producto, y la línea que cierra la ficha la repite; sin ese dato, la ficha
  queda como siempre. "Nuestro agente entendió" sale del contexto estructurado (el
  cronograma y lo que dijiste flexible: "necesitás 10 el lun 19/10 y 10 el
  jue 22/10; aceptás recibir en partes"), una vez por compra; sin eso, tu
  respuesta de ese turno. Nunca `contexto.necesidad` (texto de un modelo o
  el eco de la primera respuesta).
- **Modo demo** (`DEMO_AUTO=1` por defecto; botón "Respuestas automáticas"):
  contesta solos billetera y permiso (Google Wallet, "sí"), la tarjeta
  terminada en 4444 (de la lista que devolvió `get_payment_methods`), la
  confirmación del resumen y el OTP `123`. Decide con el **estado de la
  sesión de ADK y las herramientas que se llamaron** (nunca con texto del
  comercio): nada antes de que el usuario elija el producto, nada después de
  un bloqueo del Guard, confirma solo con `checkout_data` en el estado y
  manda el OTP solo si la última respuesta del pago es el desafío. Topes por
  paso y 12 en total. El relato lo resume en una línea ("Dirección y tarjeta
  de la billetera ✓"). `DEMO_PAUSA_AUTO_S` espacia las respuestas (con el
  plan gratuito de Gemini, 15 pedidos/min, usar 6–8).
- **Pedido libre.** El input acepta cualquier pedido: el comercio general
  busca lo que le pidas. Debajo, cuatro **sugerencias** que no dependen del
  comportamiento ni de una fecha (monitor de 27" para diseño hasta 400 USD
  para el viernes; 20 computadoras para el lunes hasta 20000 USD; auriculares
  con cancelación de ruido hasta 150 USD; zapatillas para correr talle 42
  hasta 120 USD): un toque llena el input y enviar sigue siendo tuyo. Con un
  escenario fijo activo, la sugerencia es su pedido (el `user_request` del
  fixture) o ninguna, porque ese comercio responde siempre lo mismo.
- **Selector "Comportamiento del comercio"**: cómo responde el comercio
  general (los presets `"tipo": "general"` de `scenarios.json`): Honesto,
  Inyección en la oferta, Ofrece otra categoría a tiempo, Miente un dato,
  Miente la categoría, Precio inflado, Entrega en partes, Entrega en partes +
  inyección, Sin stock suficiente y Sin resultados. Un preset general que
  world/ sume sin etiqueta acá aparece al final con el nombre armado desde su
  id. Los **escenarios fijos** (zapatillas, notebook, auriculares,
  computadoras) quedan en el grupo "Escenarios fijos (prueba)", que se
  muestra solo con `DEMO_ESCENARIOS_FIJOS=1`. `POST /api/demo/escenario
  {"escenario"}` escribe `demo_scenario` donde lo lee el comercio
  (`WORLD_TEMP_DB_DIR`, por defecto `world/ap2/.temp-db`, el `TEMP_DB_DIR` de
  `run_world.sh`) y empieza una compra nueva; acepta solo lo que el selector
  ofrece. `GET /api/demo/escenarios` solo ofrece lo que `demo_world` conoce
  (sin `demo_world`, todo): un preset que world/ todavía no tiene no aparece y,
  si no queda ninguno, el selector lo dice.
- **Interruptor "Intermediario: activado/desactivado"**: `POST
  /api/proxy/guard/state` (el reenvío con token de siempre) después de una
  compra nueva. Apagado, el proxy deja fuera filtro, intake, oportunidades y
  Guard (verificado en `proxy/app.py`: todo depende de `guard_activo`); el
  diagrama baja al Guard atenuado y los mensajes van directo agente ↔
  comercio; el relato lo dice en una línea.
- **Sin resultados**: si el comercio termina la búsqueda sin carts, el proxy
  emite `ofertas_recibidas` con `carts: []` y la corrida termina con "El
  comercio no tiene nada que cumpla tu pedido" (relato, historial y
  contraste; en el panel, "Sin resultados"). Si después el agente busca otra
  cosa y encuentra, o pide el checkout de una oferta de antes, sigue.
- **Con y sin intermediario**: al terminar una corrida (pagada, frenada,
  cortada, retenida, vencida, sin resultados) se guarda su resultado **por
  comportamiento del comercio, pedido y lado** en `localStorage`
  (`psg.demo.contraste`, las 40 más nuevas; el pedido se compara sin
  mayúsculas, tildes ni puntuación) y se muestra lado a lado cuando volvés a
  hacer el mismo pedido al mismo comportamiento ("Se compró Zento Slim 14,
  con 8 GB de memoria RAM (pediste 16 GB)" / "Frenada antes de la firma: no
  se pagó nada"). Lo real va en la unidad de lo pedido ("con 24 in (61 cm) de
  pantalla (pediste 27 in)"), igual que el motivo del Guard. `/demo/clasico`
  sigue guardando por escenario en `psg.demo.comparacion`.
- **Replay (plan B)**: en `/demo/clasico`, el selector "Grabación…"
  reproduce un `.jsonl` de `dashboard/demo/` o `.logs/` con el mismo
  diagrama y relato. `/demo` no lo usa.

| Variable | Default | Para qué |
|---|---|---|
| `ADK_URL` | `http://127.0.0.1:8000` | La API de `adk web` (Shopping Agent) |
| `WORLD_TEMP_DB_DIR` | `world/ap2/.temp-db` | Carpeta donde el comercio lee `demo_scenario` |
| `DEMO_AUTO` | `1` | `0` apaga las respuestas automáticas |
| `DEMO_PAUSA_AUTO_S` | `0` | Segundos entre respuestas automáticas (0–60) |
| `DEMO_ESCENARIOS_FIJOS` | `0` | `1` suma al selector los escenarios fijos de prueba (benchmark y tests) |

### API de `/demo` (para rehacer la vista sin romper la lógica)

| Ruta | Qué hace |
|---|---|
| `GET /demo` | La página (`static/demo.html`) |
| `GET /api/demo/estado` | `{sesion: {id, creada}, ocupado, auto, fin, escenario, mensajes: [...]}`. Mensajes: `usuario {texto, destino}`, `agente {texto, autor, oculto}`, `herramienta {nombre, args}` (solo `find_products.catalog_search` y `select_cart.cart_id`), `auto {paso, texto, detalle}`, `pago {cart_id}`, `error_agente {guard}` (sin texto crudo), `error {codigo}` |
| `GET /api/demo/escenarios` | `{activo, grupos: [{categoria, tipo, opciones: [{id, etiqueta, honesto, pedido}]}], nombres: {id: {etiqueta, tipo, pedido}}, sugerencias: [{etiqueta, texto}]}`. `tipo`: `general` o `fijo`; `honesto`: `true`, `false` o `null` (solo pinta el punto del botón); `pedido` = el `user_request` del fixture de un escenario fijo (`null` en los generales); `nombres` cubre todo lo conocido, aunque no se ofrezca (el activo puede ser uno fijo) |
| `POST /api/demo/mensaje` `{"texto"}` | 202 `{destino: "agente"}`; 200/404/409 del proxy `{destino: contexto\|confirmar\|omitir\|decision}`; 422 `se_espera_si_o_no`; 409 `agente_ocupado`; 400 |
| `POST /api/demo/escenario` `{"escenario"}` | `{activo}`; 400 si el selector no lo ofrece (fuera de la lista, desconocido para `demo_world` o fijo sin `DEMO_ESCENARIOS_FIJOS=1`) |
| `POST /api/demo/nueva` `{}` | Compra nueva (otra sesión de ADK; cancela la que corría) |
| `POST /api/demo/auto` `{"auto": bool}` | Prende o apaga el modo demo |

Los cuatro POST tienen las mismas protecciones que el resto (`Host`,
`Origin`, `Sec-Fetch-Site`, JSON); el token del proxy no sale del servidor.

Lógica sin DOM (la vista solo la pinta; testeada con `node --test`):

- `lib/demo.js`: `lineaDeTiempo(eventos, mensajes, desde)` (eventos del proxy
  + mensajes de la conversación, desde el inicio de la corrida),
  `modoDelInput({compras, ocupado, desde, ahora, replay})` → `{modo,
  etiqueta, placeholder, enviar, compraId}`, `desenlace(items)`,
  `resultadoCorrida(items, {escenario, guardActivo})`,
  `resumenComparacion(sin, con)`, `guardarResultado(mapa, resultado,
  {clave, maximo})`, `claveComparacion(escenario, pedido)`,
  `pedidoDeCorrida(items)`, `sugerenciasPara(escenarios, activo)`,
  `nombreDeEscenario(escenarios, id)`, `textoDelComercio(escenarios, activo)`.
- `lib/relato.js`: `armarRelato(items, {filtro})` → `[{id, ts, hora, texto,
  tono, actor, ficha?, nota?}]` (tono: `neutro|tenue|ok|aviso|crit|usuario|agente`; actor:
  `usuario|agente|guard|comercio|sistema|ap2`; `ficha` solo al pedir
  confirmarla), `filasFicha(ficha_parcial, {categoriaRuta})` → `[{nombre,
  valor, marca, tono, preferido}]`, `rutaCategoria(ruta)`,
  `textoEntendido(contexto)`, `resumirAgente`, `limpiarTexto`,
  `humanizarAtributo`, `formatearMedida`, `medidaEnUnidad(medida, unidad)`
  ("24 in (61 cm)"; las mismas unidades que `guard/atributos.py`, solo para
  mostrar) y `unidadLegible` (`E34` → GB).
- `lib/diagrama.js`: `geometria("horizontal"|"vertical")` (nodos, paths de
  las aristas, cuánto se corre el Guard apagado), `estadoInicial`,
  `paso(estado, item)` → `{estado, flujos: [{de, a, tono, clave,
  invertida}]}`, `recorrer(items)`. Estado: `guard`
  (`inactivo|trabajando|ok|aviso|bloqueado|apagado`), `etapas.{filtro,
  intencion, verificacion}`, `nodos.{fuentes, ap2}`.
- `demo.js` (controlador): sondea `/api/demo/estado` (0,9 s),
  `/api/proxy/guard/compras` (1,5 s) y `/api/proxy/guard/state` (4 s), escucha
  el SSE del proxy, y pinta en `#diagrama`, `#relato`, `#contraste`,
  `#escenarios` (y `#escenarios-vacio`), `#intermediario`,
  `#formulario/#entrada`, `#sugerencias`, `#nueva` y `#historial`.
  `tests/js/pagina.test.mjs` revisa que los controladores carguen como
  módulos y que cada `#id` que busca `demo.js` esté en `demo.html`.

## Fuentes de datos

Arriba a la derecha se elige la fuente de eventos:

- **En vivo (proxy)**: los `Evento` llegan por SSE desde `GET {proxy}/guard/events`.
  El servidor del panel lo reenvía desde el mismo origen (`/api/proxy/...`), así
  que el proxy no necesita CORS. Al conectar (y al reconectar) la página pide
  además `GET /api/proxy/guard/events/history`: aunque el SSE tarde o se corte,
  las compras se arman igual (los eventos no se duplican). El SSE se cierra al
  recargar o salir de la página, y el servidor cierra en ese momento el que
  abrió con el proxy (`background` de la respuesta: no espera al recolector,
  así no se acumulan suscriptores en el proxy). El estado del proxy se consulta cada 4 s, con
  un solo pedido en vuelo, y el panel dice **"proxy desconectado"** recién
  después de 3 sondeos fallidos seguidos (uno suelto no cambia nada); reintenta
  solo. Con el evaluador `stub`, cada evento `veredicto` trae `evaluador: "stub"`.
- **Replay (archivo)**: lee un `.jsonl` (un `Evento` por línea) de
  `.logs/guard_events.jsonl` (lo escribe el proxy) o de `dashboard/demo/*.jsonl`.
  **Reproducir** los vuelve a pasar con su ritmo (entre 0,3 y 1,4 s por evento).

El resto sale de archivos del repo:

| Qué | De dónde |
|---|---|
| Resultados del benchmark | `results/*.json` y después `world/ap2/results/*.json`. Por defecto, el más nuevo con `meta.guard` no nulo; si no hay, el más nuevo |
| Ficha del pedido | Como la usa el proxy (`lib/eventos.js` → `fichaVigente`): si la compra trae recibo, la del recibo; si no, la del escenario (`fixtures/scenario_<X>.json` → `intent`, con `Ficha.desde_intent()`) **solo** si el proxy corre con `PROXY_FICHA_DEMO=1` (`GET /guard/state` → `ficha_demo`) o en un replay; si no, la que armó nuestro agente con el usuario (los requisitos vigentes de `GET /guard/compras`, confirmada solo si el usuario la confirmó), o ninguna. Con `PROXY_FICHA_DEMO=0`, el default, un fixture se ve solo si se elige a mano y va rotulado ("el proxy no la usa") |
| Catálogo público | `fixtures/public_catalog.json` (para marcar qué parte del texto libre no figura en el catálogo y nombrar lo que se compró) |
| Clave pública del Guard | `GET {proxy}/guard/public-key`; si el proxy no responde, la parte pública de `.temp-db/guard_signing_key.pem`, o `dashboard/demo/guard_public_key.json` |

**No se inventan datos.** Si no hay eventos, resultados o recibo, la vista lo
dice con un estado vacío y explica cómo conseguirlos. Lo único que se muestra
sin compra en vivo es el `ap2.cart` del fixture del escenario, rotulado como
fixture. Si el proxy usa el evaluador `stub`, una franja avisa que los
veredictos son de prueba. Si el archivo del benchmark es de `--decider target`
o `best`, el panel avisa que no mide un modelo.

## Modo offline (plan B)

Para no depender del flujo en vivo el día de la demo:

1. **Grabar un ensayo real.** Con todo levantado y el Shopping Agent pasando por
   el proxy, hacer la compra de B con el Guard apagado y después con el Guard
   prendido (el interruptor del panel hace `POST {proxy}/guard/state`). Después,
   una de dos:
   - en la vista **Flujo**, botón **Guardar eventos (.jsonl)**, y mover el
     archivo a `dashboard/demo/`;
   - o copiar `.logs/guard_events.jsonl` a `dashboard/demo/ensayo-B.jsonl`.

   Commitear el archivo. No usar el nombre `guard_events.jsonl` en `demo/`.
2. **Clave pública para verificar el recibo sin proxy.** Si en la máquina de la
   demo está `.temp-db/guard_signing_key.pem`, no hace falta nada. Si no:
   `curl localhost:8010/guard/public-key > dashboard/demo/guard_public_key.json`
   mientras el proxy está levantado, y commitearlo (es solo la parte pública).
3. **Resultados del benchmark** en `results/` (ver `CLAUDE.md` → Decisiones).
4. El día de la demo: **Replay (archivo)** → elegir el ensayo. El panel recuerda
   la fuente y el archivo elegidos.

## Escalar: retenido y "Aprobar igual"

Cuando el Guard da `escalar`, el proxy **retiene** el checkout (la tarea vuelve
`failed`, sin el `ap2.checkout`) y emite `bloqueado` con
`datos.requiere_aprobacion=true`. El panel lo muestra como **"Retenido —
esperando decisión humana"** (en Flujo, Veredicto y el momento clave) con la
evidencia del Guard (motivo y chequeos que no dieron OK) y el botón
**Aprobar igual**:

1. El botón hace `POST /api/proxy/guard/aprobaciones` con
   `{"contexto_id", "cart_id"}` (JSON). El servidor del panel lo valida y lo
   reenvía al proxy (`POST :8010/guard/aprobaciones`).
2. Con la aprobación pendiente, el panel dice **"Aprobado por humano: pedile al
   agente que reintente el checkout"** (lo sabe por
   `GET /api/proxy/guard/aprobaciones`, que consulta cada 4 s en vivo).
3. El agente reintenta `create_checkout`; el Guard vuelve a evaluar. Si sigue
   en `escalar` con el mismo monto, el checkout pasa intacto y el proxy emite
   `reenviado` con `aprobacion_humana=true`: el panel muestra **"Aprobado por
   humano"**. La aprobación es de un solo uso y no salta un `bloqueado`.

El botón solo está activo en vivo con el proxy conectado; en replay se ven los
estados pero no se puede aprobar.

### Controles estables

El interruptor del Guard, el selector y el botón **Reproducir** del replay son
nodos fijos de `index.html`: cada sondeo o evento solo actualiza sus atributos
y su texto si cambian (el `<select>` y el botón se rearman solo si cambia su
contenido). El interruptor se deshabilita únicamente sin proxy (después de los
3 sondeos fallidos), nunca mientras cambia: durante el POST tiene
`aria-busy="true"` y se ignoran los clics. En las vistas, si un render dibuja
exactamente lo mismo que había, vuelven los nodos anteriores: botones como
**Verificar la firma en este navegador** o **Aprobar igual** no se reemplazan
bajo el puntero y un clic en curso no se pierde. Probado en Chrome con
DevTools: el clic del interruptor manda el `POST`, **Aprobar igual** da 201,
**Verificar la firma** y **Reproducir** responden, y los nodos siguen siendo
los mismos después de varios sondeos.

## Nuestro agente (vista 7, etapa 2)

El chat de nuestro agente con el usuario ([`docs/FLUJO.md`](../docs/FLUJO.md)):
el panel no decide nada, reenvía al proxy lo que contesta el usuario. Una
conversación por compra (`compra_id`), la más nueva primero; si hay varias, un
selector, y un aviso cuando otra compra espera respuesta. La pestaña muestra
cuántas compras esperan al usuario (solo en vivo).

1. **Pedido** del agente del cliente con sus parámetros autorizados
   (`requisitos`; las entregas `cubre` son acumuladas: "10 para el lun 19/10 ·
   20 en total para el mié 21/10") y la **pregunta** de nuestro agente.
2. **`esperando_contexto`**: cuadro de respuesta (1 a 1000 caracteres,
   Ctrl+Enter envía), **Enviar**, **Omitir** y la cuenta regresiva hasta
   `vence`. Lo escrito no se pierde si la vista se redibuja, y escribir fija la
   compra elegida (si llega otra, la vista no cambia).
3. **Lo que entendí** (`contexto_registrado`): la necesidad, cuándo se necesita
   (`necesidad_entregas`) y lo flexible, con cada flexibilidad marcada como
   *lo dijiste* o *inferida*. Si se omitió, venció o el agente no pudo armar el
   contexto: la negociación sigue con el pedido literal.
4. **`esperando_decision`**: tarjeta **Posible punto de encuentro** con la
   tabla de cambios (campo: pediste → ofrece, entregas como "10 el lun 19/10"),
   la oferta (título, SKU, precio, cart), el por qué, los parámetros que
   quedarían autorizados, la cuenta regresiva y **Aceptar** / **Rechazar**.
5. **Final**: aceptada ("Parámetros nuevos enviados al agente del cliente" y
   los `requisitos_autorizados`), rechazada ("Se mantiene el pedido original"),
   vencida ("Sin respuesta: no se compra nada. El silencio no es
   autorización"), sin oportunidad ("La negociación siguió con el pedido
   original") o bloqueada por el filtro de seguridad.

De dónde sale: los eventos `pedido_detectado`, `contexto_registrado`,
`oportunidad` y `oportunidad_decidida` (SSE o replay, agrupados por
`datos.compra_id`) y, en vivo, `GET /api/proxy/guard/compras` cada 4 s, como
las aprobaciones. El proxy manda (trae `sin_oportunidad` y `bloqueada`, que no
tienen evento propio) salvo que el SSE ya muestre un estado más avanzado que
el último sondeo.

Los botones solo andan en vivo con el proxy conectado; en replay la vista es de
solo lectura. Al vencer el plazo se deshabilitan (el proxy lo cierra). Después
de un OK del proxy quedan trabados hasta que la compra cambia de estado: el
proxy contesta enseguida pero aplica la decisión después. Todo texto que llega
del proxy (pedido, pregunta, título, por qué…) se dibuja como texto, nunca como
HTML. Si `GET /guard/state` dice `intencion: null` o `filtro: false`, la vista
avisa que esa capa está apagada.

## Qué se ve en cada vista

1. **Momento clave.** El escenario (o comportamiento del comercio general) de
   la compra más nueva, sin compras el B de los fixtures; se puede elegir otro
   (`escenarioDelMomento`). Dos columnas con los mismos pasos alineados. *Sin Guard*: el `ap2.cart` elegido con el
   `item_label` marcado como texto libre del vendedor (y subrayado lo que no
   figura en su catálogo público), el `checkout_jwt` con header, payload y
   **firma ES256 del comercio**, el `checkout_hash` recalculado en el navegador,
   **AP2: OK** con el tope (`149 ≤ 150`, de `ap2_violaciones`), la línea de
   firma y los mandatos SD-JWT del usuario decodificados (si el proxy los
   registró), con el control de que referencian el mismo `checkout_hash`.
   *Con Guard*: el texto libre descartado, los campos tipados que sí se usan,
   el mismo checkout intacto, AP2 OK, el verificador contra `:8011`, los
   chequeos, la **atadura checkout firmado ↔ cart evaluado** (chequeos con
   `origen="checkout_firmado"`), la fuente del catálogo y si el recibo quedó
   firmado, el ranking propio, **bloqueado antes de la firma** (o retenido con
   "Aprobar igual") y el recibo con su verificación. Arriba, la frase: "Todas
   las firmas son válidas y AP2 aprobó todo. Y aun así el usuario compró algo
   que no quería."
2. **Flujo.** Lista de compras (agrupadas por `contexto_id`) y la línea de
   tiempo de cada una, con los datos de cada evento. El interruptor del Guard
   está siempre en la barra.
3. **Veredicto.** Status grande con color, ícono y texto; motivo; desenlace
   (bloqueado, retenido, aprobado por humano); fuente del catálogo (`http` =
   `:8011`, `fixture` = copia local) y recibo firmado o no; panel de
   aprobación si está retenido; tabla **"Checkout firmado ↔ cart evaluado"**
   con los chequeos de origen `checkout_firmado`; chequeos agrupados por tipo
   (cumplimiento, evidencia, integridad, política, mandato) con esperado,
   observado y origen; ranking propio contra el elegido.
4. **Recibo.** Consideradas, filtradas con motivos, verificaciones contra el
   catálogo, por qué ganó, lo que quedó sin verificar, `checkout_hash` (contra
   el `ap2.checkout` y contra el SHA-256 del JWT), `kid` y **verificación de la
   firma en el navegador**: JWS compacto ES256 (r‖s) verificado con WebCrypto
   (ECDSA P-256 / SHA-256) contra la clave pública del Guard, y el payload
   firmado comparado con el recibo mostrado (`Recibo.payload_canonico()`:
   claves ordenadas, sin `firma` ni `kid`).
5. **Ficha.** El pedido original y sus requisitos (obligatorios y preferidos,
   con atributos y unidades), confirmada o sin confirmar según lo que hizo el
   usuario, de dónde sale (recibo, nuestro agente o el escenario con
   `PROXY_FICHA_DEMO=1`) y el criterio de elección.
6. **Benchmark.** Columnas por escenario: éxito del ataque solo con AP2 contra
   con Guard, con la etiqueta "X% → Y%", el caso B destacado y una tabla con
   todas las columnas (elige objetivo, AP2 frena, corridas, errores, elegidos).
   Muestra archivo, fecha, modelo, corridas y Guard. Si `with_guard` es `null`,
   lo dice.

## API del servidor

| Ruta | Qué devuelve |
|---|---|
| `GET /api/config` | `{"proxy_url": ...}` |
| `GET /api/results` | Archivos de resultados válidos, del más nuevo al más viejo |
| `GET /api/results/latest` | El JSON del benchmark elegido, con `_archivo` agregado (nombre, carpeta, fecha) |
| `GET /api/results/{results\|world}/{nombre}` | Un archivo puntual |
| `GET /api/fixtures` · `/api/fixtures/{escenario}` · `/api/fixtures/{escenario}/ficha` | Fixtures y la ficha armada con el contrato |
| `GET /api/catalog` | `fixtures/public_catalog.json` |
| `GET /api/replay` · `/api/replay/{nombre}` | Archivos `.jsonl` y sus `Evento` validados (las líneas inválidas se cuentan en `X-Lineas-Invalidas`). Con `DASHBOARD_PUBLIC_ORIGINS` (panel publicado), solo los de `dashboard/demo/`: `.logs/guard_events.jsonl` es el registro entero del proxy y no se sirve |
| `GET /api/public-key` | Clave pública del Guard (proxy o local) |
| `GET/POST /api/proxy/guard/state`, `GET /api/proxy/guard/events` (SSE), `GET /api/proxy/guard/events/history` | Reenvío al proxy (el POST, con el token) |
| `GET /api/proxy/guard/aprobaciones` | Reenvío de `GET :8010/guard/aprobaciones` (`{"pendientes", "usadas"}`) |
| `POST /api/proxy/guard/aprobaciones` con `{"contexto_id": str, "cart_id": str}` | Valida (400 si no es exactamente eso) y reenvía al proxy con el token; devuelve su status (201, 200, 409). 503 `sin_token_del_proxy` si no está `.temp-db/guard_api_token` |
| `GET /api/proxy/guard/compras` | Reenvío de `GET :8010/guard/compras` (`{"compras": [Compra]}`, la más nueva primero) |
| `POST /api/proxy/guard/compras/contexto` con `{"compra_id": str, "respuesta": str}` o `{"compra_id": str, "omitir": true}` | La respuesta al porqué (1 a 1000 caracteres sin los espacios de los bordes; `compra_id` de 1 a 200). Valida (400 `se_espera` si no es exactamente eso) y reenvía con el token; devuelve el status del proxy: 200 con la `Compra`, 404 `compra_desconocida`, 409 `estado` |
| `POST /api/proxy/guard/compras/decision` con `{"compra_id": str, "decision": "aceptar" \| "rechazar"}` | Igual: valida, reenvía con el token y devuelve 200, 404 o 409 |

Los nombres de archivo se buscan en una lista blanca armada listando las
carpetas: no hay path traversal.

### Seguridad de la API

El servidor del panel es la puerta al proxy (reenvía el interruptor y las
aprobaciones), así que se protege igual que `/guard/*`:

- `/api/*` solo atiende `Host` = `localhost:<DASHBOARD_PORT>` o
  `127.0.0.1:<DASHBOARD_PORT>` (403 si no): frena el DNS rebinding.
- Los POST (interruptor, aprobaciones y las dos rutas de nuestro agente)
  exigen el `Origin` del propio panel (403 `origen_requerido` si
  falta, `origen_no_permitido` si es otro) y, si viene `Sec-Fetch-Site`, que
  sea `same-origin`; además `Content-Type: application/json` (415). El
  navegador siempre manda `Origin` en un POST: una página ajena no puede
  apagar el Guard ni aprobar un checkout, y curl o una herramienta HTTP del
  agente no pueden usar el panel para saltear el token del proxy (tendrían que
  falsificar a propósito los headers de un navegador; para un proceso local con
  esa intención el límite es el usuario del sistema y el permiso 0600 del
  token).
- Al reenviar un POST, el servidor lee `.temp-db/guard_api_token` (el que el
  proxy escribió en este arranque) y agrega `X-Guard-Token`; el navegador
  nunca lo ve ni aparece en ninguna respuesta del panel. Manda JSON, sin
  `Origin` y con el `Host` de `PROXY_URL` (`127.0.0.1:8010`), así pasa los
  mismos controles del proxy. Los GET reenviados no llevan el token.
- Los mandatos y el `checkout_jwt` completos se muestran (la demo lo exige);
  por eso el panel y el proxy escuchan solo en loopback.
- **Publicado** (`DASHBOARD_PUBLIC_ORIGINS`, `DASHBOARD_EXTRA_HOSTS` o
  escuchando en la red), Host, Origin y Sec-Fetch-Site no alcanzan: `curl`
  manda los del dominio público y el panel le agregaría el token del proxy
  (apagar el Guard, aprobar un `escalar`, decidir una oportunidad). Por eso
  todo pide HTTP Basic con `DASHBOARD_ACCESS_TOKEN` (401 con
  `WWW-Authenticate` si no; comparación con `hmac.compare_digest`), antes
  que los demás controles, y sin contraseña el panel no arranca (salvo
  `DASHBOARD_PERMITIR_SIN_CLAVE=1`). El navegador pide la contraseña una vez y
  la vuelve a mandar por su cuenta en los `fetch` y el `EventSource` del mismo
  origen: el JS no cambia. En local no se pide nada.

## Tests

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest dashboard   # servidor (incluye /demo con un ADK falso) + tests cruzados con node
node --test "dashboard/tests/js/*.test.mjs"                        # lógica JS pura (con comillas: Node 22 no acepta la carpeta)
```

Los datos de los tests son sintéticos y se escriben en `tmp_path`. Hay dos
tests cruzados Python → JS: un recibo firmado en Python con
`Recibo.payload_canonico()` se verifica con el mismo código del navegador, y un
`CheckoutMandate` armado con el `MandateClient` del SDK de AP2 se decodifica
con el decodificador SD-JWT del panel.

## Notas para el equipo

- `busqueda_enviada` llega del proxy con su `contexto_id` (el del pedido o el
  de la respuesta del comercio). Solo para registros viejos que la traen con
  `contexto_id: null` queda el respaldo: se asocia a la primera compra del
  mismo escenario que empieza dentro de los 60 s siguientes y que todavía no
  tiene su búsqueda, y la línea de tiempo lo marca como asociada por tiempo.
- El estado de una compra sale del **último desenlace** de checkout
  (`bloqueado` o `reenviado` de `create_checkout`): una compra puede pasar de
  retenida a aprobada por humano en un reintento.
- Etapa 2: un `bloqueado` con `datos.etapa` es un corte antes del checkout:
  `seguridad` (el filtro cortó la conversación; trae `senales`) o `intencion`
  (venció la consulta de nuestro agente). Flujo los muestra como "Corte de
  seguridad" y "La consulta venció"; los de siempre (sin `etapa`) siguen siendo
  "Bloqueado antes de la firma". Los eventos de nuestro agente que llegan sin
  `contexto_id` (la pregunta se hace antes de reenviar la búsqueda) van a la
  lista "Proxy" de Flujo; la vista Agente los agrupa por `compra_id`.
- Los mandatos se leen de `reenviado.datos.mandatos` si el proxy los registra
  (un objeto `clave A2A → SD-JWT`, una lista o un string). Sin eso, la columna
  "Sin Guard" muestra que el checkout llegó intacto a la firma pero no puede
  mostrar los mandatos.
