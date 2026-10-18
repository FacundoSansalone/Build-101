# Plan: cómo trabajamos

> Tres desarrolladores, cada uno con Claude Code. Guías individuales en
> [`onboarding/`](onboarding/).

## 1. División del trabajo

| Dev | Carpeta | Misión | Guía |
|---|---|---|---|
| **Dev 1** | `world/` | El mundo AP2 real, el comercio atacante, el catálogo público y el benchmark. **Ya está hecho**; queda lo pendiente y después apoyar la integración. | [dev1-world.md](onboarding/dev1-world.md) |
| **Dev 2** | `guard/` | Ficha, extractor, verificador, los cuatro chequeos, veredicto y recibo firmado. Solo contra `fixtures/`. Tests primero. | [dev2-guard.md](onboarding/dev2-guard.md) |
| **Dev 3** | `proxy/`, `dashboard/` | Proxy A2A en `:8010`, interruptor Guard sí/no, dashboard y presentación. Con stub y fixtures mientras no estén las otras partes. | [dev3-proxy-dashboard.md](onboarding/dev3-proxy-dashboard.md) |
| **Los tres** | `contracts/` | Modelos compartidos. No se cambian sin acordarlo. | [CONTRATOS_PROPUESTOS.md](CONTRATOS_PROPUESTOS.md) |

```mermaid
flowchart LR
    W["world/ (Dev 1)<br/>comercio, catálogo, benchmark"] -->|fixtures/| G["guard/ (Dev 2)<br/>evaluar()"]
    W -->|A2A :8001, catálogo :8011| P["proxy/ (Dev 3)<br/>:8010"]
    C["contracts/ (los tres)"] --> G
    C --> P
    G -->|Veredicto + Recibo| P
    P -->|Evento| D["dashboard/ (Dev 3)"]
    W -->|world/ap2/results/*.json| D
```

## 2. Paso 0 (los tres juntos, primera hora)

1. Leer [CONTEXTO.md](CONTEXTO.md) y [ARQUITECTURA.md](ARQUITECTURA.md) (15 min).
2. Acordar los contratos partiendo de
   [CONTRATOS_PROPUESTOS.md](CONTRATOS_PROPUESTOS.md), resolviendo sus
   [decisiones abiertas](CONTRATOS_PROPUESTOS.md#6-decisiones-abiertas-para-el-paso-0).
3. Confirmar la firma de `evaluar()` y el adaptador del benchmark.
4. Revisar [`CLAUDE.md`](../CLAUDE.md) y anotar ahí lo que se decidió.
5. Commitear `contracts/` en `main`. Recién ahí cada uno abre su rama.

## 3. Fases y puntos de control

| Fase | Qué pasa | Punto de control |
|---|---|---|
| 1. Contratos | Los tres juntos | Contratos acordados y commiteados en `main` |
| 2. Paralelo | Cada uno en lo suyo | — |
| 3. Camino mínimo | Primera integración | **Escenario B de punta a punta, con y sin Guard** |
| 4. Completar | A, B2 y C, verificador, recibo, dashboard | Benchmark completo con columna "con Guard" |
| 5. Congelar | Cero features nuevas: bugs y ensayo | Demo ensayada 3 veces |

**El punto de control 3 es el más importante:** con B funcionando de punta a
punta ya hay demo presentable. Todo lo que no sirva para llegar ahí, espera.

### Qué necesita el punto de control 3

- Dev 2: `evaluar()` devuelve `bloqueado` para B con un motivo legible (aunque
  falten B2, C y el recibo firmado).
- Dev 3: el proxy reenvía `find_products` y `create_checkout` sin tocar nada,
  llama al Guard y corta el flujo si el veredicto es `bloqueado`. El
  interruptor Guard sí/no funciona.
- Dev 1: el flujo completo corre en la UI con el escenario B, con y sin proxy.

## 4. Plan B

Si el flujo completo de AP2 traba la integración, **el benchmark ya funciona
sin la UI de ADK**: habla con el comercio por A2A real y simula la decisión del
comprador con la instrucción del sample. La demo puede apoyarse en eso: correr
el benchmark de B con y sin `--guard` y mostrar el veredicto y el recibo en el
dashboard a partir de los resultados guardados.

Si también falla eso en vivo: resultados y fixtures commiteados.

## 5. Prácticas con Claude Code

- **Cada uno en su carpeta y su rama**; merges a `main` chicos y frecuentes.
- **Pedir un plan antes de cada tarea grande**, y revisarlo antes de que
  escriba código.
- **Si Claude propone cambiar un contrato, parar** y hablarlo entre los tres.
- **Actualizar [`CLAUDE.md`](../CLAUDE.md)** cuando se decide algo importante.
  Es lo que lee el Claude Code de los otros dos.
- **Commitear fixtures y resultados del benchmark**: si algo falla en vivo, se
  muestran los guardados.
- **No tocar `world/ap2/`** fuera de lo listado en
  [`world/NOTICE.md`](../world/NOTICE.md). Si hace falta, lo hace Dev 1 y lo
  agrega a `NOTICE.md`.
- Al abrir Claude Code, decirle qué dev sos: "Soy Dev 2, leé
  `docs/onboarding/dev2-guard.md`".

## 6. Estado por dev

Actualizar esta tabla al pasar cada punto de control.

| Dev | Fase actual | Hecho | Siguiente | Bloqueado por |
|---|---|---|---|---|
| Dev 1 (`world/`, `filtro/`) | Etapa 2 | Benchmark con LLM, 10 corridas, por el proxy y con Guard (`results/`); la UI de ADK muestra el motivo del Guard; `world/bench_con_pausa.py`; compra completa en la UI con B | Hecho: filtro y escenarios de computadoras (sección 7, puntos 1 y 2). Siguiente: a definir por el equipo | Contrato de la etapa 2 |
| Dev 2 (`guard/`, `intencion/`) | Etapa 2 | Ficha, extractor, verificador, 4 chequeos + mandato + atadura del checkout firmado, veredicto, recibo ES256, adaptador, ficha con LLM; ronda de revisión cerrada | Hecho: contexto, detector y Guard con parámetros por compra (sección 7, puntos 5 a 8). Siguiente: a definir; hueco conocido: la categoría de producto no entra en la verificación (ver [PREGUNTAS_DEL_JURADO](PREGUNTAS_DEL_JURADO.md)) | Contrato de la etapa 2 |
| Dev 3 (`proxy/`, `dashboard/`) | Etapa 2 | Proxy `:8010` con Guard real, interruptor, eventos SSE, aprobación humana para `escalar`, registro opcional en Supabase; dashboard `:8020` con momento clave, recibo verificado en el navegador y gráfico; ensayo de B grabado; ronda de revisión cerrada | Hecho: cadena de etapas, API y vista Agente (sección 7, puntos 9 a 11). Siguiente: a definir por el equipo | Contrato de la etapa 2 |

Punto de control 3 (B de punta a punta, con y sin Guard): probado por A2A
real con `proxy/scripts/smoke_checkout.py` y el benchmark por `:8010`, y a
mano en la UI de ADK hasta el pago el 2026-10-17
(`dashboard/demo/ensayo_B.jsonl`).

## Decisiones (antes abiertas)

Todas resueltas el 2026-10-17; el detalle está en
[`CLAUDE.md`](../CLAUDE.md#decisiones), junto con las de
[CONTRATOS_PROPUESTOS.md](CONTRATOS_PROPUESTOS.md#6-decisiones-abiertas-para-el-paso-0).

- **Resultados del benchmark en git:** en `results/` (raíz), con
  `--out ../../results`. `.gitignore` no cambia.
- **Puerto del dashboard:** 8020.
- **Ficha en vivo:** para la demo se usa la de `scenarios.json`
  (`Ficha.desde_intent`), que el dashboard muestra como confirmada.
- **`escalar` en la UI de ADK:** el proxy retiene el checkout y el Shopping
  Agent muestra el motivo del Guard (cambio mínimo listado en
  `world/NOTICE.md`). El humano ve la evidencia en el dashboard y puede
  "Aprobar igual"; el reintento del checkout pasa intacto.

## 7. Etapa 2: flujo único

Desde el 2026-10-18. El comportamiento esperado está en
[FLUJO.md](FLUJO.md): un solo flujo con dos capas, seguridad e intención.
Todo lo de la etapa 1 (escenario B, Guard, proxy, dashboard, benchmark)
sigue valiendo: es la verificación antes de la firma y el respaldo de la
demo.

### Dónde ocurre cada paso

Todo lo que va entre el agente del cliente y el proveedor pasa por el proxy
(`:8010`). Por eso el flujo se implementa sin tocar al agente del cliente,
salvo un cambio mínimo para que reciba los parámetros autorizados.

| Paso de [FLUJO.md](FLUJO.md#paso-a-paso) | Dónde | Cómo |
|---|---|---|
| 1. Pedido y contexto | proxy + `intencion/` + dashboard | El proxy ve la búsqueda (`find_products` → `catalog_search`), emite `pedido_detectado` y **retiene el pedido** hasta que el usuario contesta el porqué en el chat de nuestro agente (o lo omite). Recién ahí lo reenvía al proveedor. |
| 2. Seguridad | `filtro/`, llamado por el proxy | Corre sobre cada respuesta del proveedor antes que nada. Si bloquea, la tarea vuelve `failed` con un mensaje fijo, no se reenvía nada y no se llama a `intencion/`. Si el filtro falla, también bloquea. |
| 3 y 4. Análisis y pausa | proxy + `intencion/` | Si ninguna oferta cumple los parámetros autorizados, el detector busca una que cubra la necesidad. Si la encuentra, el proxy **retiene la respuesta** del proveedor: el agente del cliente no puede rechazar lo que todavía no recibió. |
| 5. Consulta | dashboard | Tarjeta en el chat de nuestro agente: qué cambia, por qué podría servir, Aceptar o Rechazar. |
| 6. Acepta | proxy → agente del cliente | El proxy entrega la respuesta intacta y, en un artefacto aparte, los parámetros autorizados nuevos (`guard.parametros_autorizados`). Si el proveedor manda uno con esa clave, se borra, como hoy con `guard.veredicto`. La herramienta `find_products` del agente del cliente lo lee. |
| 6. Rechaza | proxy | Entrega la respuesta sin cambios y el agente del cliente aplica el pedido literal. |
| 6. No contesta | proxy | La respuesta sigue retenida. El cliente A2A del sample espera hasta 600 s por llamada (`DEFAULT_TIMEOUT` en `common/payment_remote_a2a_client.py`), y la pregunta del paso 1 y la consulta caen en la misma llamada. Al vencer, no se compra nada. |
| 7. Verificación | `guard/` | `evaluar()` en `create_checkout`, con los parámetros autorizados **de esa compra** (sin compra, la ficha del escenario) y la ficha del fabricante dentro del catálogo. Una oferta que necesitaba autorización y no la tiene no cumple y se bloquea. Es la garantía; el filtro es defensa en profundidad. |

### Contrato (los tres, primero)

Propuesta inicial; se ajusta al acordarla.

- `ResultadoFiltro`: `pasa`, `motivo` (fijo, sin texto del proveedor) y
  `señales`.
- `Contexto`: pedido explícito, necesidad (con las palabras del usuario) y
  flexibilidades, cada una marcada como `dicha` o `inferida`.
- `ParametrosAutorizados`: los requisitos vigentes de cada compra, con su
  origen (`original`, o `aceptado` con el id de la oportunidad).
- `Oportunidad`: id, `contexto_id`, `cart_id`, cambios campo por campo
  (original → propuesto), por qué podría servir y parámetros propuestos.
- `Requisito` y `Oferta`: cantidad y cronograma de entregas.
- Tipos nuevos de `Evento` (`contracts/models.py`): `pedido_detectado`,
  `contexto_registrado`, `oportunidad` y `oportunidad_decidida` (con
  `datos.decision`: `aceptada`, `rechazada` o `vencida`). El corte del filtro
  no tiene tipo propio: es un `bloqueado` con `datos.etapa = "seguridad"`.
- El `Status` del Guard no cambia.

### Qué falta, por dev

**Dev 1: detección de malicia (`filtro/`) y escenarios (`world/`)**

1. **Filtro de seguridad** con ML, heurísticas y LLM:
   `filtrar(respuesta_del_proveedor) -> ResultadoFiltro`. El proxy lo llama
   antes que nada.
   - Tiene que bloquear A, B, B2 y el proveedor malicioso de computadoras.
   - **No** tiene que bloquear `baseline` ni el proveedor honesto de
     computadoras. Un falso positivo en el honesto rompe la demo: una oferta
     que no cumple el pedido no es un ataque.
   - Medirlo como el benchmark: tasa de bloqueo por escenario y falsos
     positivos.
   - El motivo que viaja al agente es fijo y no copia texto del proveedor.
2. **Escenarios de computadoras**, con el mismo pedido:
   - un proveedor malicioso, con una inyección para que el agente compre otra
     cosa dentro del tope;
   - un proveedor honesto, que ofrece 10 el lunes y 10 el miércoles. Lo dice
     en el `item_label` y en `offer_details`, con `entregas`.

   Hace falta un pedido por escenario: hoy hay uno global en
   `demo_world/scenario.py`. El checkout puede ir como un lote, porque hoy se
   firma siempre `quantity=1`.
3. **Cambio mínimo al agente del cliente:** que `find_products` lea
   `guard.parametros_autorizados` si viene. Se anota en `world/NOTICE.md`.
4. **Medir las corridas sin sistema:** que el agente del cliente caiga con el
   proveedor malicioso y rechace al honesto por el pedido literal. Si toma la
   oferta de 10 + 10 solo, hay que configurarlo estricto (también va a
   `NOTICE.md`).

**Dev 2: intención (`intencion/`) y Guard (`guard/`)**

5. **Contexto:** a partir del texto de la búsqueda y de la respuesta del
   usuario, armar la pregunta del porqué y el `Contexto`. Es un LLM con
   salida estructurada y puede partir de `guard/ficha_llm.py`. Va en una
   carpeta nueva, `intencion/`, para que `guard/` siga siendo determinístico.
6. **Detector de oportunidades:**
   `detectar_oportunidad(contexto, parametros_autorizados, ofertas) -> Oportunidad | None`.
   Usa un LLM con salida estructurada, más una validación en código: el cart
   existe y los números que cita coinciden con la oferta. Recibe solo lo que
   pasó el filtro.
7. **Casos de prueba**, cada uno corrido varias veces:
   - 10 el lunes y 10 el miércoles → propone;
   - 10 el lunes y 10 el viernes → no propone;
   - 20 el martes → no propone.

   La tasa de aciertos es una métrica para el pitch.
8. **Guard:** `evaluar()` con los `ParametrosAutorizados` de la compra,
   incluidas cantidad y entregas. Una oferta que no los cumple se bloquea
   aunque el agente la elija.

**Dev 3: proxy (`proxy/`) y dashboard (`dashboard/`)**

9. **Cadena de etapas en el proxy**, en este orden:
   1. retener el pedido hasta tener el contexto;
   2. filtro;
   3. análisis;
   4. retener la respuesta si hay una oportunidad;
   5. entregarla con o sin parámetros nuevos;
   6. `evaluar()` con los parámetros de la compra.

   Los parámetros autorizados se guardan por compra (`context_id`), en lugar
   del pedido global de `fuentes.py`. Falla cerrado, como hoy.
10. **API nueva**, con la misma protección que el resto de `/guard/*` (token
    `X-Guard-Token`; ver [proxy/README.md](../proxy/README.md#etapa-2-contexto-filtro-y-oportunidad)):
    - `POST /guard/compras/contexto`: `{compra_id, respuesta}` (el porqué),
      `{compra_id, omitir: true}` o `{compra_id, confirmar: true}` (la ficha
      del intake);
    - `POST /guard/compras/decision`: `{compra_id, decision: "aceptar" | "rechazar"}`;
    - `POST /guard/compras/pedido` (no estaba en el plan): el primer mensaje
      del usuario en `/demo`, para el intake de la próxima compra.
11. **Dashboard:**
    - el chat de nuestro agente: la pregunta, la respuesta y la consulta con
      Aceptar y Rechazar;
    - una vista del flujo único con las etapas en línea (Contexto →
      Seguridad → Intención → Usuario → Firma), que marca dónde terminó cada
      corrida;
    - las cuatro corridas lado a lado.
12. **Opcional:** registrar oportunidades y decisiones en Supabase
    (`persistencia.py`).

### Estado (2026-10-18, rama `etapa2/integracion`)

Posicionamiento: **la garantía es el Guard antes de la firma; el filtro es
defensa en profundidad.** Los modelos entienden y proponen, el usuario
autoriza, el código verifica. Las métricas son de muestras chicas (una
corrida por caso, casos escritos por nosotros): sirven para el pitch como
"de X a Y en esta medición", nunca como garantía.

| Tarea | Estado |
|---|---|
| Contrato | Hecho y aditivo; falta que lo revisen los tres (ver CLAUDE.md → Decisiones). La integración no lo cambió |
| 1. Filtro (Dev 1) | Hecho (`filtro/`: heurísticas + tres preguntas a OpenAI Decisions, falla cerrado, motivos fijos). Medido en 36 casos, dos corridas: filtro completo 18/18 maliciosos bloqueados con 0/18 falsos positivos (solo heurísticas: 12/18; modelo solo: 17/18). Lo medido en 26 casos (14/14) quedó superado. En la prueba de punta a punta apareció un falso positivo con el eco del pedido en el `history` (lo corrige el proxy) y otro con el desafío OTP (corregido, 0,00 en 5 de 5) |
| 2. Escenarios de computadoras en `world/` (Dev 1) | Hechos en `world/` y exportados a `fixtures/etapa2/`: `computadoras` (honesto) y `computadoras_malicioso` (inyección para comprar tablets dentro del tope; `filtro/casos.py` usa su texto real) |
| 3. `find_products` lee los parámetros autorizados | Hecho (`shopper/tools.py`, anotado en `NOTICE.md`) |
| 4. Medir las corridas sin sistema | Hecho a mano el 2026-10-18 (una corrida por caso, prueba de punta a punta en Windows; resultados en [FLUJO §5](FLUJO.md#5-la-demo-cuatro-corridas-del-mismo-flujo)). No es una muestra estadística |
| 5 y 6. Contexto y detector (`intencion/`) | Hecho, con tests. Intake con decisiones: repregunta lo que falta (umbrales 0,80/0,60, hasta 3 repreguntas) y confirma la ficha; con atributos y la taxonomía de Google |
| 7. Medir el detector con Gemini real | Hecho: 9/9 (`gemini-3.1-flash-lite-preview`, 3 casos × 3; `python -m intencion.evaluar_casos`). Intake con OpenAI Decisions: 60/60 decisiones de repreguntar o no en el turno 0 (12 pedidos), 0 valores resueltos equivocados |
| 8. Guard con los parámetros de la compra | Hecho (`cantidad`, `entregas`, `cubre`, atributos con unidades, `minimo`, fuentes de verdad en orden y ficha del fabricante) |
| 9 y 10. Etapas en el proxy y API | Hecho, con tests contra el comercio falso y una prueba de punta a punta real (abajo) |
| 11. Dashboard: vista Agente | Hecho (pestaña 7, `#agente`), con tests de Python y JS; la oferta de una consulta se nombra con su título público o el SKU. En `/demo`, el diagrama muestra las etapas (filtro, intención, verificación) y la comparación con y sin intermediario. Falta la línea "Contexto → Seguridad → Intención → Usuario → Firma" como vista propia |
| 12. Supabase | No se hizo (opcional) |
| Integración (`etapa2/integracion`) | Hecho: ficha de la demo con atributos y taxonomía en el proxy y el dashboard; fabricante consultado por el proxy antes de `evaluar()` (`FABRICANTE_FUENTE`, registrado en el evento `veredicto`); AP2 con el tope de cada compra; atributos confirmados en la ficha autorizada (el Guard publica `CAMPOS_SOPORTADOS`/`OPERADORES_SOPORTADOS`); taxonomía en el intake; título público en la consulta; `Compra.nueva_espera()`; `PROXY_FICHA_DEMO=0`; escenario `notebook_dato_falso` |

**Benchmark sin LLM** (`--source direct --decider target --trials 1`, Guard
enchufado, catálogo del fixture y fabricante fixture): éxito del ataque solo
con AP2 → con Guard: B, B2, C, `notebook_malicioso`, `notebook_dato_falso` y
`auriculares_malicioso` de 100% a 0%; A lo frena AP2; los honestos, 0%.
`--decider target` supone que el agente siempre cae: mide al Guard, no a un
modelo.

**Prueba de punta a punta (2026-10-18, puertos 9311-9313):** comercio real
del sample, catálogo y proxy con filtro e intake de OpenAI Decisions,
fabricante fixture y `PROXY_FICHA_DEMO=0`. "Quiero una notebook para
programar con 16 GB de RAM" → el intake pregunta el porqué y lo que falta
(tope, moneda, plazo) → repregunta → confirma la ficha (tope USD 1.200,
memoria ≥ 16 GB, taxonomía 328 Laptops) → el filtro deja pasar
`notebook_dato_falso` → `create_checkout(cart_4)` **bloqueado** antes de la
firma: "memoria_ram 8 GB no cumple el obligatorio (mínimo 16 GB)" con el
fabricante (fixture rotulado); AP2 no lo frenaba (USD 899 ≤ 1.200). Con
`notebook_malicioso`, el filtro corta en las heurísticas.

**Prueba de punta a punta del 2026-10-18 con `deploy/start_all.sh`** (Windows,
agentes reales: Gemini flash-lite en el agente de compras y el detector,
OpenAI Decisions en el intake): las cuatro corridas de computadoras, con su
resultado en [FLUJO §5](FLUJO.md#5-la-demo-cuatro-corridas-del-mismo-flujo).
Sin sistema, el malicioso paga 20 tablets (USD 9.000) y el honesto, 10 + 10
el viernes (USD 17.000). Con sistema, el malicioso se corta en Seguridad y el
honesto se paga 10 + 10 el miércoles (USD 18.000) tras la consulta. En
notebooks, con "Comprá la más barata que cumpla.", el Guard frena la Zento
Slim 14 antes de la firma (el filtro no la ve: no hay texto hostil). Una
corrida por caso: muestra chica.

### Orden y puntos de control

1. **Contrato**, los tres juntos, antes que nada.
2. **En paralelo**, cada uno con stubs y fixtures: Dev 1 (1–4), Dev 2 (5–8)
   y Dev 3 (9–12).
3. **Punto de control A:** el **caso honesto** de punta a punta, con y sin
   sistema. Es lo nuevo, así que va primero.
4. **Punto de control B:** el **caso malicioso** con el filtro.
5. **Congelar**, ensayar las cuatro corridas y grabarlas.

### Plan B

- Si el filtro falla en vivo, el caso malicioso se muestra con el escenario
  B de la etapa 1: el Guard bloquea antes de la firma, y ya está grabado en
  `dashboard/demo/ensayo_B.jsonl`. El momento "el filtro no lo ve y el Guard
  igual frena" sale con `notebook_dato_falso` (o C).
- Si el caso honesto falla en vivo, se muestran las corridas grabadas.
