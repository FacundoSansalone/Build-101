# Pre-Sign Guard — contexto para Claude Code

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

Catálogo público (verdad): `GET http://localhost:8011/products` y
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
8003 Payment Processor · 8010 proxy del Guard (reservado) · 8011 catálogo público.

El Shopping Agent usa `MERCHANT_AGENT_URL` para hablar con el comercio. Para
pasar por el proxy: `MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent`.

## Comandos

```sh
bash world/run_world.sh B                       # levanta todo en el escenario B
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
  quien llama con `merchant.id` del catálogo; lo usa el chequeo de política).
  El tipo de evento `guard_cambiado` (interruptor sí/no).
- **Resultados del benchmark en git:** se escriben en `results/` (raíz) con
  `--out ../../results`. `.gitignore` no cambia.
- **Puerto del dashboard:** 8020.
- **Ficha en vivo:** para la demo se usa la de `scenarios.json` → `intent`
  (`Ficha.desde_intent`), que el dashboard muestra como confirmada. La ficha
  con LLM es opcional.
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
    `Pre-Sign Guard: <status>. <motivo>`.
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
  - **Pendiente de revisar:** `docs/CONTEXTO.md` ("no es un detector de
    prompt injection") y `docs/PREGUNTAS_DEL_JURADO.md` todavía responden
    con el posicionamiento anterior.

Tests de las carpetas nuevas, desde la raíz:

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest   # Linux/macOS: world/ap2/.venv/bin/python
```
