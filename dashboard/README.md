# dashboard/ (Dev 3)

Panel de la demo del Pre-Sign Guard: el **escenario B lado a lado**, el flujo de
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

## Fuentes de datos

Arriba a la derecha se elige la fuente de eventos:

- **En vivo (proxy)**: los `Evento` llegan por SSE desde `GET {proxy}/guard/events`.
  El servidor del panel lo reenvía desde el mismo origen (`/api/proxy/...`), así
  que el proxy no necesita CORS. Al conectar (y al reconectar) la página pide
  además `GET /api/proxy/guard/events/history`: aunque el SSE tarde o se corte,
  las compras se arman igual (los eventos no se duplican). El SSE se cierra al
  recargar o salir de la página. El estado del proxy se consulta cada 4 s, con
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
| Ficha del pedido | `fixtures/scenario_<X>.json` → `intent`, con `Ficha.desde_intent()` del contrato; si la compra trae recibo, la ficha del recibo |
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

1. **Momento clave.** Escenario B (se puede cambiar) en dos columnas con los
   mismos pasos alineados. *Sin Guard*: el `ap2.cart` elegido con el
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
5. **Ficha.** El pedido original y sus requisitos (obligatorios y preferidos)
   como **confirmada**, con el criterio de elección.
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
| `GET /api/replay` · `/api/replay/{nombre}` | Archivos `.jsonl` y sus `Evento` validados (las líneas inválidas se cuentan en `X-Lineas-Invalidas`) |
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

## Tests

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest dashboard   # servidor + tests cruzados con node
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
