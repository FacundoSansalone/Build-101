# KaiAI — contexto para Claude Code

> **Si sos un Claude Code nuevo en este repo, leé primero `docs/onboarding/`
> según el dev que te use** (`dev1-world.md`, `dev2-guard.md` o
> `dev3-proxy-dashboard.md`). Si no sabés qué dev es, preguntá.
>
> **Etapa 2 (desde el 2026-10-18): un solo flujo con dos capas, seguridad e
> intención.** Antes que nada, leé [`docs/FLUJO.md`](docs/FLUJO.md) y la tarea
> de tu dev en [`docs/PLAN.md`](docs/PLAN.md#7-etapa-2-flujo-único). Si otro doc
> (también los de onboarding) contradice FLUJO.md, manda FLUJO.md.

## Índice de `docs/`

| Archivo | Para qué |
|---|---|
| [`docs/FLUJO.md`](docs/FLUJO.md) | **El qué:** comportamiento esperado del flujo único (seguridad e intención) y la demo de cuatro corridas. Manda sobre el resto |
| [`docs/CONTEXTO.md`](docs/CONTEXTO.md) | El porqué: problema, investigación, quién cubre qué, posicionamiento |
| [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md) | El cómo: flujo AP2 real, dónde va el Guard, chequeos, veredictos, datos de `world/` |
| [`docs/CONTRATOS_PROPUESTOS.md`](docs/CONTRATOS_PROPUESTOS.md) | **Borrador** de modelos para `contracts/` y decisiones abiertas del paso 0 |
| [`docs/DEMO.md`](docs/DEMO.md) | Escenarios, guion, momento clave, benchmark y pitch |
| [`docs/INTEGRACION.md`](docs/INTEGRACION.md) | Para integradores externos: el gateway de producción, API `/v1`, webhooks, recibos y modo mínimo |
| [`docs/PLAN.md`](docs/PLAN.md) | División del trabajo, fases, puntos de control, plan B, estado por dev |
| [`docs/PREGUNTAS_DEL_JURADO.md`](docs/PREGUNTAS_DEL_JURADO.md) | Objeciones esperables y respuestas |
| [`docs/onboarding/`](docs/onboarding/) | Una guía por dev, con primer prompt sugerido |

Proyecto de hackatón. Equipo de 3 desarrolladores, cada uno con Claude Code.

## Qué construimos

Un sistema del lado del comprador que acompaña a su agente de compras en un
**flujo único con dos capas** ([`docs/FLUJO.md`](docs/FLUJO.md)):

1. **Seguridad del agente.** Un filtro (`filtro/`) revisa cada mensaje del
   proveedor antes que nada y, si hay malicia, corta la conversación. Antes de
   la firma, el Guard (`guard/`) verifica la compra contra los parámetros
   autorizados y los datos públicos del comercio.
2. **Intención del usuario.** Nuestro agente (`intencion/`) le pregunta al
   usuario para qué quiere lo que pide y observa la negociación. Si una oferta
   no cumple el pedido literal pero sí la necesidad, pausa la negociación y se
   la consulta al usuario. Si el usuario acepta, le pasa los parámetros nuevos
   al agente del cliente.

**La garantía es el Guard antes de la firma; el filtro es defensa en
profundidad.** El filtro corta temprano lo que se ve venir, pero la seguridad
no depende de que detecte: el Guard verifica con código determinístico,
contra lo que autorizó el usuario y contra fuentes que el vendedor no
controla, sin leer su texto libre. Los modelos entienden y proponen, el
usuario autoriza, el código verifica.

AP2 ya frena lo que supera el tope firmado. Nuestro valor está en lo que AP2 no
ve: manipulación dentro del mandato, datos falsos del comercio, precios que no
coinciden con su catálogo público y compras viables que un agente literal deja
pasar.

## Estructura y dueños

| Carpeta | Dueño | Qué es |
|---|---|---|
| `world/` | Dev 1 | Flujo AP2 real (copia del sample oficial), comercio atacante, catálogo público, benchmark. Ver `world/README.md`. |
| `filtro/` | Dev 1 | Detección de malicia (ML, heurísticas y LLM): el primer filtro de cada mensaje del proveedor. Etapa 2. |
| `intencion/` | Dev 2 | Contexto del pedido (el porqué) y detector de oportunidades, con LLM. Etapa 2. |
| `guard/` | Dev 2 | Verificación determinística antes de la firma: ficha del pedido, extractor estructurado, verificador, chequeos, veredicto, recibo. |
| `proxy/` | Dev 3 | Proxy A2A entre el Shopping Agent y el Merchant Agent. Corre las etapas del flujo: contexto, filtro, intención, pausa y verificación. |
| `dashboard/` | Dev 3 | Vista de la demo: flujo, veredicto, recibo, gráfico del benchmark. |
| `decisiones/` | Compartido (filtro e intake) | Adaptador de juicios tipados con probabilidad: OpenAI Decisions (`gpt-6-luna`) o Jev. Etapa 2. |
| `taxonomia/` | Compartido (intake) | Taxonomía de productos de Google (árbol con IDs) y clasificación nivel por nivel. Etapa 2. |
| `fabricante/` | Compartido (Guard, proxy, benchmark) | Ficha del fabricante por GTIN: fixture rotulado (por defecto) o Icecat. Etapa 2. |
| `contracts/` | Los tres | Modelos de datos compartidos. **No se cambian sin acordarlo entre los tres.** |
| `fixtures/` | Generado | Escenarios exportados por `world/` para trabajar sin levantar agentes. |

Cada uno edita solo su carpeta. Merges a `main` chicos y frecuentes.

## Reglas

- **No modificar AP2.** El Guard funciona con AP2 tal cual. Los únicos cambios al
  sample están listados en `world/NOTICE.md`.
- **El Guard nunca altera objetos firmados** (checkout JWT, mandatos). Puede
  leerlos, bloquear, avisar o escalar, pero no reescribirlos.
- **La seguridad va primero.** Todo mensaje del proveedor pasa por el filtro
  antes que cualquier otra etapa. Lo bloqueado no llega a ningún LLM ni
  agente, tampoco al nuestro, y no dispara la búsqueda de alternativas. Si el
  filtro falla, se bloquea. Las métricas son tasas antes y después, nunca
  "0%".
- **Los LLM detectan y proponen; nunca autorizan.** Hay LLM en el filtro, en
  el contexto del pedido y en el detector de oportunidades. Cambiar los
  parámetros lo autoriza solo el usuario, y la verificación antes de la firma
  (`guard/`) sigue siendo código determinístico.
- **Los parámetros explícitos siguen vigentes hasta que el usuario acepte
  cambiarlos.** Nuestro agente no negocia ni le habla al proveedor: pregunta,
  observa, propone y comunica. El silencio no es autorización.
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

Catálogo público (verdad): `GET http://127.0.0.1:8011/products` y
`/products/<sku>`. El pedido del usuario y sus requisitos están en
`fixtures/scenario_*.json` → `intent`.

Los carts no están firmados. Lo que sí está firmado: el `ap2.checkout`
(`checkout_jwt` del comercio, que lleva el `item_label` como título) y los
mandatos SD-JWT del usuario (`CheckoutMandate`, `PaymentMandate`). En el flujo
human-present del sample **no se firma un `IntentMandate`**: el pedido viaja
como texto en `catalog_search`. Detalle en `docs/ARQUITECTURA.md`.

Contrato **provisorio** del Guard que usa el benchmark (a confirmar en `contracts/`):

```python
def evaluate(intent: dict, offers_seen: dict, chosen_cart_id: str) -> dict:
    # offers_seen = {"carts": [...], "offer_details": [...]}
    # -> {"status": "aprobado" | "con_avisos" | "escalar" | "bloqueado", ...}
```

## Puertos

8000 Shopping Agent (ADK) · 8001 Merchant Agent · 8002 Credentials Provider ·
8003 Payment Processor · 8010 proxy del Guard (reservado) · 8011 catálogo público ·
8020 dashboard · 8030 gateway público (solo con claves de API).

El Shopping Agent usa `MERCHANT_AGENT_URL` para hablar con el comercio. Para
pasar por el proxy: `MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent`.

## Comandos

```sh
bash world/run_world.sh B                       # levanta todo en el escenario B
bash deploy/start_all.sh computadoras           # demo completa (world, proxy, dashboard en :8020/demo); Windows con Git Bash
cd world/ap2 && uv run --no-sync --package ap2-samples python -m demo_world.benchmark --trials 10
cd world/ap2 && uv run --no-sync --package ap2-samples python -m demo_world.export_fixtures
```

Los resultados del benchmark quedan en `world/ap2/results/`, que hoy está en
`.gitignore`. Cómo commitearlos está en las decisiones abiertas de
`docs/PLAN.md`.

## Decisiones

Anotá acá lo que se acuerde entre los tres (contratos, veredictos, puertos).
Las preguntas pendientes están en `docs/CONTRATOS_PROPUESTOS.md` y
`docs/PLAN.md`.

Paso 0 cerrado el 2026-10-17. Modelos en `contracts/models.py`.

1. **Precio contradicho (C) → `escalar`.** No bloquea y no llega a la firma.
2. **Ranking:** obligatorios cumplidos → preferidos cumplidos (más es mejor)
   → precio (menor es mejor). Da NK-OUT-01 sin tocar `world/`.
3. **Integridad:** `escalar` solo si otra opción es igual o mejor en todo
   (obligatorios, preferidos y precio) que la elegida.
4. **Cumplimiento usa el dato público cuando existe** (B2 → `bloqueado`) y
   además se marca la evidencia `contradicho`.
5. **Ficha genérica:** lista de `Requisito` + `Ficha.desde_intent(intent)`.
6. **Recibo:** JWS compacto ES256 sobre `Recibo.payload_canonico()`. Clave
   del Guard en `.temp-db/guard_signing_key.pem` (ignorado por git),
   `kid: guard-key-1`. `evaluar()` arma el recibo sin firmar; lo firma
   `guard.recibo.firmar_recibo()` afuera, para que `evaluar()` siga sin I/O.
7. **Eventos al dashboard:** SSE en `GET :8010/guard/events` y copia en JSONL
   (`.logs/guard_events.jsonl`) para reproducir offline.
8. **Nombres:** `guard.evaluar()` es el núcleo; `guard.benchmark_hook:evaluate`
   es solo el adaptador del benchmark.
9. **El proxy no limpia los carts: solo evalúa.** El veredicto viaja en un
   artefacto A2A aparte con la clave `guard.veredicto`; `ap2.checkout` intacto.
- **Agregados al contrato:** `evaluar(..., ahora=None)` (fecha del recibo,
  para que sea determinística) y `OfertasVistas.comercio_id` (lo completa
  quien llama; el proxy usa el `merchant.id` **firmado en el checkout**, no el
  del catálogo; lo usa el chequeo de política).
- **El catálogo público no siempre existe (2026-10-18).** Sin catálogo (sin
  `CATALOGO_URL` o si no responde) el Guard evalúa con catálogo vacío: todo
  queda "solo declarado" y lo obligatorio escala; nunca aprueba sin verificar.
  El respaldo `fixtures/public_catalog.json` es solo para la demo offline
  (`CATALOGO_RESPALDO_FIXTURE=1`). Un catálogo que dice ser de otro comercio
  que el que firmó el checkout no se usa.
  El tipo de evento `guard_cambiado` (interruptor sí/no).
- **Resultados del benchmark en git:** se escriben en `results/` (raíz) con
  `--out ../../results`. `.gitignore` no cambia.
- **Puerto del dashboard:** 8020.
- **Ficha en vivo:** desde la etapa 3 manda la que confirmó el usuario en el
  intake (`PROXY_FICHA_DEMO=0`, el default). La de `scenarios.json` → `intent`
  (con `guard.ficha.ficha_desde_intent`: más `attributes` y
  `google_product_category`) solo se usa con `PROXY_FICHA_DEMO=1` (grabaciones,
  benchmark por el proxy), y el dashboard la muestra como confirmada solo si
  el usuario la confirmó.
- **Imports:** `contracts/`, `guard/`, `proxy/` y `dashboard/` corren con el
  entorno de `world/ap2` y la raíz del repo en el path (`pytest.ini` →
  `pythonpath = .`; el benchmark con `PYTHONPATH=../..`).
- **Windows:** usar `PYTHONUTF8=1` (la consola cp1252 rompe el benchmark).
- **Proxy: si no puede evaluar, bloquea** (no vio las ofertas de ese
  `context_id` o `evaluar()` lanzó una excepción). Emite `error` + `bloqueado`.
  Sigue la regla "nunca aprueba en silencio".
- **Guard, dentro de los contratos:** precio = `amount` del cart en mandato,
  ranking e integridad; el resto de los campos usa el dato público si el SKU
  está en el catálogo. Falta un dato para un obligatorio → `escalar`. `stock`
  no se verifica. La evidencia `no_verificable` (calidad, entrega, reputación)
  usa `origen_usado="declarado"`. El `id` del recibo es `rcb-` + sha256 de las
  entradas sin el texto libre. El adaptador del benchmark no firma el recibo;
  el proxy sí.
- **Ronda de revisión (2026-10-17), decidido con el equipo:**
  - **Contrato:** `evaluar(..., checkout: CheckoutVisto | None = None)`, con
    el data part `ap2.checkout` tal cual. Si viene, el Guard ata lo firmado al
    cart evaluado (origen `checkout_firmado`): `checkout_hash` recalculado
    (b64url de sha256 del JWT) distinto del declarado → `bloquea`; `id` del
    JWT o `cart_id` del checkout distinto del elegido → `bloquea`; total
    firmado distinto del `amount` evaluado → mandato con el total firmado
    (sobre el tope → `bloquea`) y si no `escalar`; moneda firmada distinta →
    `escalar`; `merchant.id` firmado fuera de `comercios_aprobados` →
    `bloquea`; JWT ilegible → `bloquea`. El `title` del JWT no se lee. Sin
    `checkout` (benchmark) ese chequeo no se hace.
  - **`escalar`: el proxy retiene el checkout** (tarea `failed`, "requiere
    aprobación humana") y el dashboard muestra la evidencia con "Aprobar
    igual" → `POST :8010/guard/aprobaciones` `{contexto_id, cart_id}`. La
    aprobación es de un solo uso y vale para ese contexto, cart y monto; el
    agente reintenta `create_checkout` y el checkout pasa intacto. Eventos:
    `bloqueado` con `datos.requiere_aprobacion=true`; `reenviado` con
    `datos.aprobacion_humana=true`.
  - **Obligatorio respaldado solo por datos declarados → `escalar`** (SKU
    fuera del catálogo, campo omitido). Un dato solo declarado que no es
    obligatorio sigue dando `con_avisos`.
  - **UI de ADK:** cambio mínimo en `shopping_agent/tools.py` para mostrar el
    motivo cuando la tarea del comercio viene `failed` (en `world/NOTICE.md`).
  - **Proxy fail-closed:** más de un `ap2.checkout`, checkout de otro cart o
    contexto, respuesta que no es una tarea, JSON inválido, stream cortado o
    un segundo checkout en SSE → `bloqueado`. Las ofertas se registran solo
    desde `find_products`. Toda tarea lleva un `guard.veredicto` (el del
    comercio se borra); si el proxy no pudo evaluar, lleva uno `bloqueado`
    con motivo fijo del proxy. Texto al agente: plantilla fija
    `KaiAI: <status>. <motivo>`.
  - **Aprobaciones:** atadas a contexto, cart y centavos + moneda firmados;
    nunca hacen pasar un `bloqueado`. No emiten `Evento` (no hay tipo en el
    contrato): el dashboard consulta `GET /guard/aprobaciones`.
  - **API local protegida:** `/guard/*` (proxy) y `/api/*` (dashboard) exigen
    Host local, Origin del dashboard y JSON en los POST; fuera de loopback no
    arrancan salvo `PROXY_ALLOW_REMOTE=1`. Además, los POST de `/guard/*`
    exigen `X-Guard-Token`: el proxy genera un token por arranque en
    `.temp-db/guard_api_token` (`GUARD_API_TOKEN_PATH`) y el dashboard lo lee
    y lo agrega en su reenvío. Proxy y dashboard se levantan desde la misma
    raíz del repo para compartirlo.
- **Verificación adversarial (2026-10-17):** 12 hallazgos re-verificados (10
  cerrados, 2 parciales que se cerraron después) y 7 nuevos corregidos:
  - **Evaluador fail-closed:** si no se puede importar `guard.evaluar()` (o no
    acepta `checkout=`), el proxy no arranca. El stub solo con
    `PROXY_ALLOW_STUB=1`, y `/guard/state` lo muestra.
  - La aprobación humana se ata al total firmado exacto
    (`totals[type=total]` + moneda del JWT); la caché de decisiones, al data
    part `ap2.checkout` completo.
  - Al Shopping Agent le llega un `guard.veredicto` reducido
    (`status`, `motivo`, `cart_id_elegido`, tipos y resultados de chequeos),
    sin texto del comercio. El completo va al dashboard por eventos.
  - El Guard valida la coherencia interna del JWT: suma de `line_items` =
    total (si no, `bloquea`) y `exp`/`iat` contra `ahora` (vencido → `escalar`).
- **Servicios locales por `127.0.0.1`** (no `localhost`): en Windows,
  `localhost` prueba primero IPv6 y cada intento fallido suma ~2 s.
  `PROXY_PUBLIC_URL` (la URL que ve el cliente A2A) sigue en `localhost`.
- **Benchmark con el plan gratuito de Gemini** (15 pedidos/min):
  `world/bench_con_pausa.py` corre el mismo `benchmark.main()` espaciando las
  llamadas al LLM. No toca `world/ap2/`.
- **Etapa 2: flujo único (2026-10-18).**
  - **Definición y tareas:** el comportamiento está en `docs/FLUJO.md` y las
    tareas por dev, en `docs/PLAN.md` §7.
  - **Pitch y demo:** el pitch dura **4 minutos**. La demo son cuatro corridas
    del mismo flujo, con el mismo pedido ("20 computadoras para el lunes"):
    proveedor malicioso y proveedor honesto, cada uno con y sin sistema.
  - **Detección de malicia:** la hace **Dev 1** (`filtro/`).
  - **Reglas que se reemplazan:** "no prometemos detectar inyecciones" y "el
    LLM solo arma la ficha".
  - **Por defecto:** si el usuario no contesta el porqué, sigue el pedido
    literal sin análisis de oportunidades; si hay varias alternativas, se
    consulta la mejor, una por vez.
  - **Revisado en la integración (abajo):** `docs/CONTEXTO.md` ("no es un
    detector de prompt injection") y `docs/PREGUNTAS_DEL_JURADO.md` ya
    responden con el posicionamiento de la etapa 2.
- **Etapa 2, implementación de la intención (rama `etapa2/intencion`, a
  revisar entre los tres):**
  - **Contrato (aditivo):**
    - `CampoRequisito` suma `cantidad` y `entregas`, con el operador `cubre`
      (cronograma acumulado); `Oferta` suma `cantidad` y `entregas`;
    - modelos nuevos: `Entrega`, `Contexto`, `Flexibilidad`, `Cambio`,
      `Oportunidad`, `ResultadoFiltro` y el protocolo `Filtro`;
    - 4 tipos de `Evento` nuevos y la clave `guard.parametros_autorizados`.
  - **Guard:** `cantidad` y `entregas` son compromisos del comercio que no se
    pueden verificar antes de comprar. No escalan por "solo declarado"; lo
    que cuenta es que cumplan los parámetros que autorizó el usuario.
    `evaluar()` recibe la ficha de la compra (`proxy.ficha_de(contexto)`), y
    si no hay compra, la de la demo.
  - **Proxy:**
    - retiene el `find_products` para el porqué y la respuesta para la
      consulta (plazos `PROXY_ESPERA_*`, dentro de un presupuesto de 570 s
      por llamada);
    - el agente y el filtro los arma `python -m proxy` desde el entorno
      (`GOOGLE_API_KEY`, `GUARD_FILTRO`); `create_app()` no lee el entorno;
    - el benchmark y `smoke_checkout.py` corren con `INTENCION_ACTIVA=0`,
      porque no hay un humano que conteste.
  - **Ficha de la compra:** si el escenario de la demo tiene una ficha
    confirmada, manda esa; la del LLM solo se usa si no hay ninguna (un
    comercio real). Sin tope y moneda en la ficha, el detector no propone
    nada.
  - **`world/`:** cambio mínimo en `shopper/tools.py` para que el agente del
    cliente reciba los parámetros autorizados (anotado en `NOTICE.md`; es
    carpeta de Dev 1).
  - **Datos:** `fixtures/etapa2/computadoras.json`. Ya no se escribe a mano:
    `computadoras` está en `world/` y el fixture lo exporta
    `demo_world.export_fixtures`. Queda fuera de `fixtures/scenario_*`
    porque el dashboard lista esos como la demo de zapatillas.
- **Etapa 2, integración (rama `etapa2/integracion`, a revisar entre los
  tres).** Junta filtro, intake con decisiones, categorías y el proxy.
  - **Posicionamiento:** la garantía es el Guard antes de la firma; el
    filtro es defensa en profundidad. Los modelos entienden y proponen, el
    usuario autoriza, el código verifica. `docs/CONTEXTO.md` y
    `docs/PREGUNTAS_DEL_JURADO.md` ya lo dicen así ("el filtro detecta, pero
    la garantía no depende de detectar").
  - **Adaptador `decisiones/`:** juicios tipados con probabilidad (sí/no,
    elegir, puntaje) por HTTP, OpenAI Decisions (`gpt-6-luna`) o Jev
    (`jev-latest`); `DECISIONES_PROVEEDOR=openai|jev` (sin la variable, el
    que tenga clave: `OPENAI_API_KEY` primero, `TYPESAFE_API_KEY`). El modelo
    nunca genera texto: elige, y el código decide con umbrales. Cualquier
    falla es `DecisionesError` y quien llama falla cerrado.
  - **Filtro (`filtro/`):** `GUARD_FILTRO=filtro:filtrar`. Heurísticas y,
    si pasan, tres preguntas al modelo (umbral 0,5); `FILTRO_SOLO_HEURISTICAS=1`
    para la demo offline. Motivos fijos y señales cortas: nada del proveedor
    sale del filtro. Falla cerrado (sin cliente, con error o tarde, bloquea).
    Con la intención activa es obligatorio: sin filtro el proxy no arranca,
    salvo `PROXY_PERMITIR_SIN_FILTRO=1` (tests y demo). Al filtro no le llega
    el eco exacto de nuestro pedido en el `history` (el sample manda
    `role: agent`; sin eso, el modelo daba un falso positivo en la prueba de
    punta a punta).
  - **Intake (`intencion/intake.py`):** `INTENCION_PROVEEDOR=decisiones|gemini`
    (sin la variable, `decisiones` si hay cliente). Valida con
    probabilidades: ≥ 0,80 resuelto, 0,60–0,80 a confirmar, < 0,60 o "no
    dicho" se repregunta; como mucho 3 repreguntas y al final se confirma la
    ficha (lo inferido solo se autoriza si el usuario confirma). Los
    atributos y `minimo` entran a la ficha autorizada: el Guard publica lo
    que sabe chequear (`guard.chequeos.CAMPOS_SOPORTADOS` y
    `OPERADORES_SOPORTADOS`). Al cerrar, la ficha suma la categoría de la
    taxonomía de Google (`taxonomia.clasificar` con el mismo cliente; sin
    cliente queda vacía).
  - **Oportunidades:** solo cambian campos que el usuario **dijo** flexibles
    (más cantidad y entregas si la necesidad tiene un cronograma que la
    oferta cubre); `por_que` es una plantilla; `Oportunidad.titulo` es el
    título público de la oferta o vacío (lo fuerza el proxy; el dashboard
    muestra el SKU), nunca el del vendedor.
  - **Categorías variadas:** requisitos de atributo con nombres schema.org
    (`Requisito.campo="atributo"`), taxonomía de Google en
    `Ficha.categoria_taxonomia`, fuentes de verdad en orden (comprador >
    fabricante > página pública > catálogo > solo declarado) y la ficha del
    fabricante por GTIN con `FABRICANTE_FUENTE` (`fixture` por defecto,
    rotulado como datos de prueba; `icecat` apagado). El proxy la consulta
    antes de `evaluar()` (y para las oportunidades) y la registra en el
    evento `veredicto` (`datos.fabricante`); sin dato o con falla, lo
    obligatorio escala.
  - **Proxy:** la ficha de la demo sale de `ficha_desde_intent` (con
    atributos); sin compra, la del escenario de esa compra y no la global;
    AP2 (`ap2_violaciones`) con el tope de la ficha de cada compra;
    `PROXY_FICHA_DEMO=0` apaga las fichas de la demo (como un comercio real:
    manda la que confirmó el usuario en el intake). La ficha de la demo
    sigue mandando por defecto sobre la del intake.
  - **`world/`:** `computadoras` ya está en `world/` (y fuera del
    benchmark); escenarios de notebooks y auriculares, y
    `notebook_dato_falso` (RAM falsa **sin inyección**: el filtro no lo ve y
    el Guard igual frena). El proveedor malicioso de computadoras está en
    `world/` (`computadoras_malicioso`, fuera del benchmark); `filtro/casos.py`
    usa su mismo texto.

- **Deploy (2026-10-18, `deploy/`):** toda la demo en una sola máquina
  (`deploy/start_all.sh`) y en la nube en un Tenki Sandbox
  (`deploy/tenki.sh`). Solo se publica el dashboard (:8020, `/demo`). El
  resto sigue en `127.0.0.1`, sin tocar `world/` ni el proxy.
  `DASHBOARD_PUBLIC_ORIGINS` (y `DASHBOARD_EXTRA_HOSTS`) suman el dominio
  público a los `Origin`/`Host` que acepta `/api/*`.
- **Gateway de producción (2026-10-18, rama `etapa2/produccion`, a revisar
  entre los tres).** El proxy abierto a integradores externos. Guía para
  ellos: `docs/INTEGRACION.md`; para el operador: `proxy/README.md` y
  `deploy/README.md`. Sin configuración nueva, la demo no cambia.
  - **Dos oyentes, un proceso:** el local (:8010, como siempre) y el público
    (:8030), que solo se abre con al menos una clave activa en
    `GATEWAY_CLAVES_ARCHIVO`. En el público no existe `/guard/*`.
  - **Claves `psg_<kid>_<secreto>`;** el archivo guarda solo el sha256. Con
    claves, el stub está prohibido y el filtro es obligatorio.
  - **Compras de integrador:** `POST /v1/compras` fija la ficha (con tope
    obligatorio) y nada del tráfico la cambia; no hay intake, solo el porqué.
    El agente manda `X-Compra-Id`, que ata cada `contextId` a la compra.
    Una compra = un comercio. El Guard está siempre prendido para ese
    tráfico. Responder, decidir y aprobar solo por `/v1`: el dashboard da
    409 `compra_de_integrador`.
  - **Comercios por archivo** (`/a2a/m/{id}`, sin URL libre), con
    `merchant_ids` obligatorios: el `merchant.id` firmado tiene que ser de la
    ruta y va a la `Politica` del Guard.
  - **Webhooks** HMAC con `t` (anti-replay) y SSRF con la IP fijada. La cola
    vive en memoria: se reconcilia con `GET /v1/compras/{id}`.
  - **Estado:** `GATEWAY_ESTADO=supabase` solo explícito; la migración
    `proxy/sql/003_gateway.sql` no está aplicada (se aplica a mano). Tras un
    reinicio, las esperas cierran del lado seguro (porqué → literal,
    oportunidad → `vencida`) y la aprobación de un solo uso se reclama en la
    base. Una sola instancia.
  - **Fuera de alcance:** verificar la firma ES256 del comercio, comparar
    comercios en una compra (toca `contracts/`), `Politica` por integrador y
    cuotas por clave.
- **Nombre: KaiAI (2026-10-18).** Reemplaza "Pre-Sign Guard" en lo visible:
  dashboard, docs, la plantilla del proxy (`KaiAI: <status>. <motivo>`), el
  título de la API `/v1` y el `User-Agent` de los webhooks (`KaiAI-Webhooks/1`).
  No cambian las claves técnicas (`guard/`, `guard.veredicto`, `/guard/*`),
  ni `contracts/` ni `world/`: los textos de la herramienta del sample
  ("Pre-Sign Guard blocked …") y las marcas `HACKATHON (Pre-Sign Guard)`
  siguen igual, y el dashboard reconoce las dos marcas
  (`dashboard.demo_api.MARCAS_GUARD`).

- **Mejoras (2026-10-18, ramas `mejoras/*`):**
  - **`DEMO_HOY=2026-10-18`:** fija el "hoy" del agente de intención.
    `deploy/start_all.sh` no lo fija: lo toma solo si viene del entorno o del
    `.env` (una grabación). El Guard no lo usa: los vencimientos del checkout
    firmado se miran con el reloj de verdad.
  - **JSONL privado de integradores:** los eventos de compras de integrador van
    a `GUARD_EVENTS_JSONL_PRIVADO` (`.logs/guard_events_privados.jsonl`). El
    JSONL principal, que sirve el replay, no los tiene.
  - **`[::1]` y `SO_EXCLUSIVEADDRUSE`:** el proxy escucha también en `[::1]`
    (en Windows, `localhost` tardaba ~2,1 s por conexión). Si otro proceso
    tiene ese puerto en `[::1]`, no arranca. En Windows los sockets usan
    `SO_EXCLUSIVEADDRUSE`.
  - **Registro del pedido del usuario:** `/demo` registra, en cada mensaje al
    agente, todo lo que el usuario le dijo en la compra (`POST
    /guard/compras/pedido`, con token; desde el 2026-10-20, no solo el
    primero) y el intake lo lee.
    Vence a los 180 s y se usa una vez. No autoriza nada.
  - **`computadoras_malicioso`** (`world/`): proveedor malicioso de computadoras
    (tablets con inyección). Las ofertas honestas quedan igual.
  - **`.gitattributes`:** `*.sh` con LF, para que Git Bash en Windows y bash en
    la VM de Tenki no fallen con CRLF.
  - **`ADK_HOST`:** `world/run_world.sh` levanta la dev-ui de ADK en
    `127.0.0.1`. Para publicarla, `ADK_HOST=<host>`.
  - **`filtro_modo`:** `filtro.describir()` da el modo efectivo del filtro
    (`heurísticas + modelo …` o `solo heurísticas`). Sale en la línea de
    arranque del proxy y en `GET /guard/state`. Solo informa.

- **Etapa 3: producto general, sin hardcodeos (2026-10-19, ramas `etapa3/*`).**
  Objetivo: cualquier pedido, cualquier día, cualquier comportamiento del
  comercio. Los escenarios fijos quedan para el benchmark y los tests.
  - **Comercio general** (`demo_world`): busca en un dataset propio
    (`demo_world/data/productos.json`, marcas inventadas y GTIN del rango de
    circulación restringida de GS1) según `catalog_search`, sin LLM, y aplica
    un `comportamiento` (`honesto`, `inyeccion`, `otra_categoria_a_tiempo`,
    `dato_falso`, `miente_categoria`, `precio_inflado`, `entrega_partes`,
    `sin_stock`, `sin_resultados`). Presets en `scenarios.json` con
    `"tipo": "general"`. Fechas relativas a hoy; `cart_id` único por contexto.
  - **Una sola fuente de verdad:** catálogo público y ficha del fabricante
    salen del mismo dataset, antes de las mentiras del comercio.
  - **Manda la ficha que confirmó el usuario:** `PROXY_FICHA_DEMO=0` por
    defecto; la del escenario solo con `PROXY_FICHA_DEMO=1` (grabación,
    benchmark por el proxy). Un escenario sin `intent` propio no cae a
    zapatillas.
  - **Categoría en el Guard** (`guard/categorias.py`, sin LLM): con
    `Ficha.categoria_taxonomia`, la oferta tiene que caer en la rama (igual o
    descendiente) con fuente independiente; fuera de la rama, más general o
    sin dato → `escalar`; dentro de la rama solo declarado → aviso. La ficha
    siempre lleva categoría: clasificador o, si no hay,
    `taxonomia.palabras.categoria_por_palabras` (tabla determinística).
  - **Precio por unidad:** con `cantidad` > 1 se verifica amount ≈ precio del
    catálogo × cantidad. Una oferta con varias unidades y una ficha sin
    cantidad → `escalar`.
  - **`DEMO_HOY`** queda solo para reproducir grabaciones.
  - **Subconjunto de la taxonomía** ampliado a 18 ramas (computadoras,
    monitores, TV, proyectores, periféricos, almacenamiento, impresoras,
    celulares, consolas, routers, cámaras, muebles de oficina, sillas,
    mochilas, cafeteras, audio y calzado).
  - **Intake (etapa 3):** lo que el usuario dijo y quedó resuelto no se
    des-resuelve en el turno siguiente. Acepta un tipo general de producto
    ("computadoras" es la categoría 278 de la taxonomía). Un día de la semana
    es el próximo, nunca hoy: dicho un lunes, "el lunes" es el de la semana
    que viene; en una misma oración, con otra fecha antes, sigue la secuencia
    (no se corre entre oraciones ni ante "si no"). Un número de cosas ("20
    computadoras") nunca es el tope.
  - **Escenarios fijos:** `/demo` los ofrece solo con `DEMO_ESCENARIOS_FIJOS=1`
    (benchmark, tests y grabaciones). Sin la variable, el selector trae solo
    los comportamientos del comercio general.
  - **Sin resultados:** una búsqueda sin ofertas emite `ofertas_recibidas`
    con `carts: []` y `offer_details: []`, y la corrida termina ("sin
    resultados").
  - **Instrucción del shopper:** la búsqueda (`find_products`) conserva todas
    las restricciones del pedido (cantidad, plazo, tope y especificaciones).
    Sin eso el agente perdía "para el lunes" y el comercio general no podía
    planificar las entregas (`world/NOTICE.md`).
  - **Smoke de `/v1`:** `proxy/scripts/smoke_v1_general.py` prueba una compra
    de integrador sin LLM de nuestro lado: crea la compra con `POST
    /v1/compras`, busca y pide el checkout con `X-Compra-Id`, e imprime el
    `status` del veredicto. Por defecto arma un gateway de prueba en el
    proceso; con `--gateway` y `KAIAI_CLAVE` apunta a uno ya levantado.
    `--esperado` sale con error si el status no es ese.

- **Motor en Vercel (2026-10-19, ramas `vercel/*`).** Se despliega solo el
  motor (`contracts`, `guard`, `filtro`, `intencion`, `decisiones`,
  `taxonomia`, `fabricante`, `proxy`) en Vercel Hobby, con estado en
  Supabase. La simulación (`world/`, `dashboard/`) queda afuera: en la vida
  real los agentes son externos, y para probar se expone por túnel. Guía:
  `deploy/vercel/README.md`.
  - **Frontera:** `[tool.kaiai.motor]` en el `pyproject.toml` raíz. La
    simulación entra solo por `create_app(..., demo=FuentesDemo())`
    (`proxy/demo_local.py`), desde `python -m proxy`; lo exige
    `proxy/tests/test_frontera_motor.py`.
  - **Entrada:** `index.py` → `proxy/vercel.py` (`crear_app_serverless`).
    Solo expone `/a2a/m/{id}` y `/v1/*`: no hay `/guard/*` ni nada en disco.
    Si la configuración está incompleta o trae una variable prohibida, da
    503 `motor_no_configurado` a todo.
  - **Secretos por entorno:** `GUARD_SIGNING_KEY_PEM_B64` (en Vercel nunca se
    crea una clave), `GATEWAY_CLAVES_JSON` y `GATEWAY_COMERCIOS_JSON`
    (`proxy/arranque.py`, `ClaveRecibo`).
  - **Varias instancias** (`GATEWAY_INSTANCIAS=varias`, forzado en Vercel).
    Reemplaza "una sola instancia" del gateway:
    - el porqué y la decisión esperan en `gw_esperas`, con PATCH
      condicionados: gana una sola respuesta;
    - la compra se escribe antes de avisar, y hay una barrera antes de
      responder: si una escritura decisiva falla, 503 o bloqueado;
    - las escrituras se fusionan con lo que ya está en la base;
    - una espera huérfana se cierra del lado seguro después de
      `vence + GATEWAY_GRACIA_ESPERA_S`.

    Con `una` (la demo local) no cambia nada.
  - **Topes de Hobby:** presupuesto de 270 s por llamada, porqué de 60 s,
    decisión de 150 s y cuerpos de 4 MB. Webhooks en línea (2 intentos)
    antes de responder; la cola persistente queda para después.
  - **Base:** `proxy/sql/004_serverless.sql` va después de 003. El motor de
    Vercel (https://kaiai-motor.vercel.app) usa un proyecto de Supabase
    propio (`liylqznfyqvyppuupqra`), no el del equipo, con 003 y 004
    aplicadas por `deploy/vercel/aplicar_migraciones.py` (2026-10-19). En el
    proyecto del equipo no están aplicadas.
  - **Túnel de la demo:** `bash deploy/vercel/tunel_demo.sh` levanta
    `tunel_router.py` (prefijo secreto) y un túnel sin cuenta: cloudflared
    rápido si la red deja salir al puerto 7844, y si no pinggy por SSH en el
    443 (dura 60 min; el script lo reabre). La URL cambia en cada arranque: el
    script actualiza `GATEWAY_COMERCIOS_JSON` en Vercel y redespliega.
  - **Prueba local sin Supabase:** `deploy/vercel/postgrest_local.py`,
    `local_multi.py` (balanceador por pedido, como Vercel) y
    `proxy/scripts/smoke_v1_pausas.py`.
  - **El porqué de una compra de integrador** no pide lo que ya está en la
    ficha (tope, moneda): va solo la plantilla del porqué.
  - **Consola web: https://kaiai-motor.vercel.app/consola** (`proxy/consola/`,
    `create_app(..., consola=True)`; `/` → 307): hace lo del agente de un
    integrador con la clave que pega el usuario, sin permisos propios; CSP
    estricta y todo texto del comercio con `textContent`. Local:
    `GATEWAY_CONSOLA=1`.

- **Revisión para producción (2026-10-20, ramas `arreglos/*`, a revisar
  entre los tres).** Seis revisiones por área y corridas de punta a punta con
  agentes reales; ~50 arreglos con su test. Lo que cambia de comportamiento:
  - **Guard:** la ficha del fabricante respalda un obligatorio solo si el
    GTIN está atado a la oferta por una fuente por SKU (un GTIN que solo
    declara el comercio no alcanza: `notebook_honesto` y
    `auriculares_honesto`, sin catálogo, escalan). "1,500 g" (miles ambiguo)
    no se interpreta; "16 Gb" (bits) no son bytes; los textos conservan
    cualquier alfabeto; los ids de comercio se comparan exactos y no van al
    motivo; `exp`/`iat` con decimales valen.
  - **Filtro:** revisa el `history` con cualquier `role` (el eco exacto lo
    saca el proxy); un JWT solo es opaco si sus partes decodifican como tal;
    el texto pegado (camelCase, guiones) se parte para revisarlo. Sin falsos
    positivos con "α7R" ni con publicidad para personas.
  - **Proxy:** tras un corte, al agente no le llega nada del comercio (ni
    `id`, `contextId` o `kind`); una tarea retenida o bloqueada lleva solo lo
    del proxy; del sobre JSON-RPC pasa solo `jsonrpc`, `id` y `result`/`error`;
    tipos raros del comercio dan un bloqueo limpio, no un 500. "Hoy" con
    `KAIAI_ZONA_HORARIA` (por defecto Buenos Aires). Sin 30 s para la
    consulta de oportunidad, no se consulta (pedido literal). La categoría de
    las palabras del usuario gana si la del clasificador es de otra rama.
    `/v1/filtrar` y `/v1/evaluar`: 2 en curso por integrador (429). Las
    cabeceras de la plataforma (`X-Forwarded-*`, `X-Vercel-*`, `CF-*`) no
    llegan al comercio.
  - **Intención:** las flexibilidades del camino de decisiones habilitan su
    campo (con p ≥ 0,80); un cronograma a medias no se arma; el cronograma
    solo habilita la cantidad que contó el usuario; "prefiere" dudoso no
    afloja un obligatorio; fechas como "el jueves 29" o "por la mañana".
  - **Panel:** "sí, pero…" no confirma ni acepta (va como corrección o pide
    sí/no). Publicado, pide contraseña (`DASHBOARD_ACCESS_TOKEN`, HTTP
    Basic); `tenki.sh` la genera y no sube `SUPABASE_ACCESS_TOKEN`.
  - **`world/`:** otra búsqueda de la misma compra sigue en su contexto A2A
    (nuestro agente no repregunta el porqué); el catálogo público escucha en
    `127.0.0.1` (`PUBLIC_CATALOG_HOST` para publicarlo).
  - **Pendiente:** el N+1 de `GET /v1/compras` con varias instancias, la
    retención de las tablas `gw_*` y el origen del recibo de `/v1/evaluar`
    (toca `contracts/`).

- **Panel del cliente, fase 1: historial por `/v1` (2026-10-21, ramas
  `panel/claves` y `panel/historial`, a revisar entre los tres).** Decidido
  con el usuario: panel en el motor (`/panel`, fase 2), acceso con **clave de
  solo lectura**, **retención de 90 días**, usuario = la empresa cliente.
  - **Claves de solo lectura:** `"permisos": ["lectura"]` en la clave del JSON
    de integradores; solo los `GET` de `RUTAS_LECTURA` (`proxy/claves.py`,
    lista blanca); todo `POST` de `/v1` y todo `/a2a/*` → 403 `solo_lectura`.
    `generar_secretos.py --clave-lectura <integrador>`.
  - **Historial:** los eventos de las compras de integrador van a `gw_eventos`
    (004) con una proyección saneada (`proxy/historial.py`): la base nunca
    guarda texto libre del comercio. `busqueda_enviada`/`ofertas_recibidas`,
    hasta 20 por compra.
  - **Alertas** (`proxy/alertas.py`, en cada veredicto de la compra con
    `chequeos_detalle` saneado, `monto` y `moneda`): `manipulacion` (el filtro
    detectó algo; un corte por falla nuestra no acusa), `dato_falso` (un dato
    declarado contradice una fuente independiente; si el Guard no pudo
    comparar, es `sin_verificar`), `precio_distinto`, `checkout_alterado`
    (las cuatro son "intento de estafa"), `no_cumple` y `sin_verificar`.
  - **Explicación** (`proxy/explicacion.py`, determinística, plantillas en
    español): `GET /v1/compras/{id}/explicacion`, `GET /v1/resumen`, y en
    `GET /v1/compras` filtros `desde`/`hasta`/`referencia`/`alerta` con
    `cursor` (sin `chequeos_detalle` en el listado). N+1 del listado arreglado.
  - **Retención:** `GATEWAY_RETENCION_DIAS` (90), `purgar()` y
    `/interno/purgar` (fuera de `/v1`, solo con `GATEWAY_PURGA_SECRETO` o
    `CRON_SECRET`, cron diario en `vercel.json`; sin secreto la ruta no
    existe). `proxy/sql/005_historial.sql` (índices) **no está aplicada**.

- **Panel del cliente, fase 2: `/panel` (2026-10-21, rama `panel/web`, a
  revisar entre los tres).** `proxy/panel/` (`create_app(..., panel=True)`,
  prendido en Vercel; local con `GATEWAY_PANEL=1`): estáticos sin clave, solo
  GET, CSP de la consola. La empresa entra con su clave de solo lectura
  (validada con `GET /v1/resumen`) y ve resumen, lista con filtros y "Cargar
  más", y el detalle con estafa, línea de tiempo, chequeos y la firma del
  recibo verificada en el navegador (`recibo.mjs`, extraído de la consola y
  servido por las dos). Sin permisos propios ni `POST`. Guía:
  `docs/INTEGRACION.md` §5.9.
  - **Lo que sumó a `/v1` (aditivo):** `GET /v1/clave` (integrador, `kid` y
    `permisos`; en `RUTAS_LECTURA`): el panel avisa si la clave es completa;
    en `GET /v1/compras`, los filtros `resultado` e `intento_de_estafa`
    (`cortada_por_seguridad` y `vencida` van a la base como `estado`; el resto
    se filtra en el gateway con el tope de escaneo de `alerta`) y `monto` y
    `moneda` por compra (la regla de la explicación); y en la explicación,
    `pedido`, `referencia`, `comercio`, `creada` y `tope`, así el detalle es
    un solo pedido.
  - **Pulido tras la prueba en vivo (rama `panel/pulido`):** resultado
    `sin_ofertas` (la última búsqueda volvió vacía y no hubo checkout; marca
    en la compra con fusión de tres vías); la línea de tiempo se ordena por
    `orden` de `gw_eventos`, y un candado por compra guarda las filas del
    historial en el orden en que se emitieron; `monto_retenido` en
    `/v1/resumen` (el panel muestra "Frenado o retenido"); una evidencia que
    repite la conclusión de su chequeo se fusiona en una sola línea.
  - **Probado de punta a punta (2026-10-21):** motor con varias instancias
    (`local_multi.py` + `postgrest_local.py`) y la simulación real, siete
    compras (honesto, precio inflado, miente la categoría, otra categoría,
    inyección, sin resultados y entrega en partes con la propuesta aceptada)
    y el panel en el navegador con la clave de lectura: resultados y alertas
    correctos, firma verificada, consola sin errores.

- **Demo siempre prendida en Railway (2026-10-21, rama `deploy/railway`, a
  revisar entre los tres).** El comercio simulado ya no depende de una
  máquina con `tunel_demo.sh`. Guía: `deploy/railway/README.md`.
  - **Un servicio, un contenedor** (`deploy/railway/Dockerfile`, contexto la
    raíz): `start_all.sh general` adentro, todo por `127.0.0.1`. La única
    puerta en `$PORT` es `tunel_router.py`: `/<TUNEL_PREFIJO>/a2a/*` al
    comercio, `/<TUNEL_PREFIJO>/…` al catálogo, `/salud` (healthcheck, no
    dice qué falta) y el resto al panel, que pide `DASHBOARD_ACCESS_TOKEN`.
    ADK, el proxy, los otros agentes y el panel (`DASHBOARD_SOLO_LOOPBACK=1`)
    quedan en loopback. El gateway (:8030) no se abre: es del motor de Vercel.
  - **Enrutador:** `--resto` solo con prefijo y solo por opción (con una
    variable olvidada, `tunel_demo.sh` publicaría el panel local sin
    contraseña); al panel le llega el `Host` público y lo chequea contra
    `DASHBOARD_PUBLIC_ORIGINS`. Si el cliente corta, suelta el upstream (el
    SSE del panel no termina nunca). Sin `--resto`, igual que antes.
  - **`arrancar.sh`:** valida `GOOGLE_API_KEY`, `DASHBOARD_ACCESS_TOKEN` (16
    o más), `TUNEL_PREFIJO` (el de `.temp-db/vercel/TUNEL_PREFIJO.txt`) y
    `DASHBOARD_PUBLIC_ORIGINS` (https) sin mostrarlas; abre el enrutador recién
    con el panel y ADK arriba y el panel contestando 401 sin contraseña. Si un
    proceso muere, el contenedor sale con error (`ON_FAILURE`).
  - **`railway.json`** (en `deploy/railway/`: la ruta se fija en el panel de
    Railway, Settings → Config-as-code): Dockerfile, `/salud` con 600 s,
    siempre prendido y **una réplica** en `us-east4-eqdc4a` (el estado vive
    en memoria).
  - **`.dockerignore`** en la raíz con lista blanca (semántica de Docker,
    emulada en `proxy/tests/test_despliegue_railway.py`): nunca entran
    `.env*`, `.temp-db/`, `.logs/`, `results/`, venvs, `.vercel/`, `.git` ni
    claves (salvo los certificados públicos del sample). El gráfico del
    benchmark queda vacío en Railway.
  - **Apuntar el motor:** `generar_secretos.py --apuntar <dominio>` reescribe
    `GATEWAY_COMERCIOS_JSON` con el mismo prefijo (`--dominio` genera otro);
    `deploy/vercel/apuntar_vercel.sh` suma `vercel env add` y el redeploy (lo
    usan `tunel_demo.sh` y `deploy/railway/apuntar_vercel.sh`, que antes
    comprueba `/salud` y el catálogo con el prefijo).
  - **Probado sin Docker** (Docker Desktop no corría): `arrancar.sh` de punta
    a punta en Windows (rutas, contraseña, `Host`/`Origin` de `/api/*`, SSE,
    caída de un proceso y `SIGTERM`), ~1 GB de RAM quieta. La imagen no se
    construyó todavía.

Tests de las carpetas nuevas, desde la raíz:

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest   # Linux/macOS: world/ap2/.venv/bin/python
```
