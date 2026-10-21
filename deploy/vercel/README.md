# El motor de KaiAI en Vercel

En Vercel corre **solo el motor**: el gateway (`/v1` y `/a2a/m/{comercio}`)
con el filtro, la intención y el Guard, y el estado en Supabase. La
simulación (`world/`: el Merchant Agent, el catálogo y el resto de los
agentes; `dashboard/`) sigue en tu máquina y el motor le habla por **un
túnel**. Los agentes de compras son externos: hablan con el motor como
integradores (clave `psg_…` y `X-Compra-Id`, ver
[`docs/INTEGRACION.md`](../../docs/INTEGRACION.md)).

```
agente del integrador ──https──▶ Vercel: index.py (motor, varias instancias) ──▶ Supabase
                                        │
                                        └──https──▶ túnel (ngrok) ──▶ :8090 tunel_router.py
                                                                         ├─ /a2a/* ──▶ :8001 Merchant Agent
                                                                         └─ resto  ──▶ :8011 catálogo
```

| Archivo | Qué es |
|---|---|
| [`vercel.json`](../../vercel.json) | La función `index.py`: `maxDuration: 300` (el máximo de Hobby) y `excludeFiles` (lo que se sube pero no es runtime; la simulación ya la deja afuera `.vercelignore`; Vercel acepta hasta 256 caracteres); y el cron diario de la purga (`/interno/purgar`, [paso 9](#9-retención-y-purga)) |
| [`.vercelignore`](../../.vercelignore) | Lo que **no** sube `vercel deploy`: lista blanca en la raíz (solo el motor) y lista negra adentro (secretos, simulación, tests). Lo verifica `proxy/tests/test_despliegue_vercel.py` |
| `env.ejemplo` | Todas las variables, obligatorias, opcionales y **prohibidas** |
| `comercios.ejemplo.json` | Formato de `GATEWAY_COMERCIOS_JSON` con la simulación detrás del túnel |
| `generar_secretos.py` | Clave del recibo, clave de integrador (y, con `--clave-lectura`, una de solo lectura para el panel) y los registros JSON, cada uno en su archivo. No imprime secretos |
| `verificar_supabase.py` | Solo lectura: ¿están las tablas y columnas de las migraciones 003 y 004? |
| `tunel_router.py` | Un solo puerto (:8090) para la simulación: `/a2a/*` al comercio, el resto al catálogo |
| `tunel_demo.sh` | Un comando para la demo contra Vercel: enrutador + túnel sin cuenta (`TUNEL=auto`: cloudflared rápido si hay salida al puerto 7844; si no, pinggy por SSH en el 443, que dura 60 min: a los 55 (`TUNEL_RENOVAR_S`) el script abre otro, apunta Vercel al nuevo y recién después corta el viejo; si pinggy no da dos túneles a la vez, espera a que se corte y queda una ventana de 1-2 min sin túnel). Reescribe `GATEWAY_COMERCIOS_JSON` con la URL nueva y redespliega; si `vercel env add` o el deploy fallan, muestra el error y corta. Ctrl+C corta todo |
| `aplicar_migraciones.py` | 003, 004 y 005 por la API de administración de Supabase, con `SUPABASE_ACCESS_TOKEN` |
| `local_multi.py` | Vercel en tu máquina: N instancias de `index:app` y un balanceador por pedido en :8040 |
| `postgrest_local.py` | PostgREST falso por HTTP en :54321 (las tablas `gw_*` en memoria): la prueba local **sin Supabase** |
| `local_sin_supabase.env.ejemplo` | El entorno de esa prueba (sin secretos) |
| [`proxy/scripts/smoke_v1_pausas.py`](../../proxy/scripts/smoke_v1_pausas.py) | Prueba de las pausas (porqué y oportunidad) con las respuestas por otras instancias |
| [`proxy/consola/`](../../proxy/consola/) | La consola web del motor (`/consola`): probar una compra desde el navegador ([paso 8](#8-consola-web)) |
| [`proxy/panel/`](../../proxy/panel/) | El panel del cliente (`/panel`): el historial de compras de la empresa, con su clave de solo lectura ([paso 8b](#8b-panel-del-cliente)) |

Los comandos van desde la raíz del repo, con el venv de `world/ap2`
(`PY=world/ap2/.venv/Scripts/python` en Windows con Git Bash,
`world/ap2/.venv/bin/python` en Linux y macOS) y `PYTHONUTF8=1`.

## Antes de empezar

- **La entrada:** `index.py` (raíz, `from proxy.vercel import app`) y
  `proxy/vercel.py` (`crear_app_serverless`), el `pyproject.toml` raíz con las
  dependencias del motor y `[tool.vercel] entrypoint = "index:app"`,
  `.python-version` (3.12) y `proxy/sql/004_serverless.sql`.
- **Qué arma `proxy/vercel.py`:** solo `/a2a/m/{comercio}` y `/v1/*` (sin
  `/guard/*` ni la ruta vieja, todo pedido con clave), la clave del recibo
  solo del entorno, el estado en Supabase, varias instancias y webhooks en
  línea, y nada en disco. Si falta algo o algo no sirve, responde **503
  `{"error": "motor_no_configurado"}` a todo** (también a
  `/v1/openapi.json`) y el motivo va a los logs, sin secretos. No arranca:
  sin una clave activa en `GATEWAY_CLAVES_JSON`; sin `GATEWAY_COMERCIOS_JSON`
  o con un comercio por http fuera de loopback; sin `GATEWAY_ESTADO=supabase`
  y sus credenciales; sin `GUARD_SIGNING_KEY_PEM_B64` (o `_PEM`); sin
  `GUARD_FILTRO` o con uno fuera del motor; sin el Guard real; con una
  variable **prohibida** prendida; con `GATEWAY_PUBLIC_URL` o
  `SUPABASE_URL` que no son https (http solo contra loopback, para la
  prueba local); o con un tiempo por encima de su tope (abajo).
- **Sin credenciales del LLM** (`GOOGLE_API_KEY`, `OPENAI_API_KEY` o
  `TYPESAFE_API_KEY`) la intención queda apagada: no hay pregunta del porqué
  ni oportunidades y las compras siguen el pedido literal. El registro de
  auditoría de la demo (`Persistencia`, tablas de `001_persistencia.sql`) no
  se usa: el estado del gateway va a las tablas `gw_*`.
- Una cuenta de Vercel (Hobby alcanza), Node para `npx`, y ngrok (o
  cloudflared) para el túnel.
- **Límites de Hobby que el motor respeta:** 300 s por pedido (con Fluid
  compute, que viene prendido en los proyectos nuevos: revisalo en Project
  Settings → Functions), cuerpo de hasta 4,5 MB, bundle de hasta 500 MB,
  varias instancias sin memoria compartida, disco de solo lectura (salvo
  `/tmp`). Por eso las esperas son más cortas que en local (porqué 60 s,
  decisión 150 s, presupuesto por llamada 270 s) y todo el estado va a
  Supabase.

## Paso a paso

### 1. Login y proyecto

```sh
npx vercel login        # lo hacés vos: abre el navegador
npx vercel link         # desde la raíz del repo; crea .vercel/ (ignorado por git)
```

`vercel link` pregunta el scope y el nombre del proyecto. Vercel detecta la
app por `index.py` en la raíz (con una variable `app` ASGI) y le manda
**todas** las rutas: no hacen falta `rewrites`. La app es Starlette, que no
tiene un preset con nombre como FastAPI o Flask, así que `pyproject.toml`
fija la entrada con `[tool.vercel] entrypoint = "index:app"` (la clave que
documenta el [runtime de Python](https://vercel.com/docs/functions/runtimes/python):
`module:variable`; la clave de `functions` sigue siendo `index.py`). Después del deploy, una
ruta que no existe tiene que devolver el 404 JSON del motor, no la página
`NOT_FOUND` de Vercel (paso 7).

**Región:** sin `regions` en `vercel.json` (Hobby admite una sola región y
el default es `iad1`, Washington). Elegí la más cercana al proyecto de
Supabase en Project Settings → Functions → Function Region, porque cada
pedido hace varias idas y vueltas a la base. Por ejemplo: Supabase
`sa-east-1` (São Paulo) → `gru1`; `us-east-1` → `iad1`; `us-west-1` →
`sfo1`; `eu-central-1` → `fra1`; `eu-west-1` → `dub1`; `eu-west-2` → `lhr1`.

### 2. Supabase

Aplicá las migraciones **a mano**, una vez y en orden, en el SQL Editor del
proyecto (pegar el archivo y correrlo) o con `psql`:

```sh
psql "<connection string>" -f proxy/sql/003_gateway.sql
psql "<connection string>" -f proxy/sql/004_serverless.sql
psql "<connection string>" -f proxy/sql/005_historial.sql
```

**005 (historial, 2026-10-21) todavía no está aplicada en ningún proyecto:**
hay que aplicarla en el del motor (`liylqznfyqvyppuupqra`) con
`aplicar_migraciones.py --solo 005_historial.sql` (o con las de arriba). Solo
suma índices (los filtros de `GET /v1/compras`, el cursor, `GET /v1/resumen`
y la purga), es idempotente y no cambia datos: sin ella el motor anda igual,
más lento con muchas compras.

O con un token de acceso personal (supabase.com/dashboard/account/tokens) en
el `.env` como `SUPABASE_ACCESS_TOKEN=sbp_…`, por la API de administración
(la clave secreta del proyecto no alcanza: no crea tablas). No imprime el
token y se puede correr otra vez:

```sh
$PY deploy/vercel/aplicar_migraciones.py        # 003, 004 y después 005
$PY deploy/vercel/aplicar_migraciones.py --solo 005_historial.sql   # solo la nueva
```

Después verificá (solo lectura; lee `SUPABASE_URL` y
`SUPABASE_SERVICE_ROLE_KEY` del entorno y nunca muestra la clave):

```sh
$PY deploy/vercel/verificar_supabase.py
#   ok gw_compras (9 columnas, 003 + 004)
#   FALTA la tabla gw_esperas (004)   ← si no aplicaste 004
```

Con `SUPABASE_ANON_KEY` además comprueba que la clave pública no lea las
tablas `gw_*`. Los índices, el trigger y RLS no se ven por PostgREST; en el
SQL Editor:

```sql
select tablename, rowsecurity from pg_tables where schemaname = 'public' and tablename like 'gw\_%';
select indexname, indexdef from pg_indexes where schemaname = 'public' and tablename like 'gw\_%';
select tgname, tgrelid::regclass from pg_trigger where not tgisinternal and tgrelid::regclass::text like '%gw\_%';
```

### 3. Secretos

```sh
$PY deploy/vercel/generar_secretos.py --integrador demo --dominio <tu-dominio>.ngrok-free.app
```

Escribe en `.temp-db/vercel/` (ignorado por git y por `.vercelignore`), con
permisos 600, y **no imprime ningún secreto** (solo los nombres de los
archivos y el `kid` público de la clave del integrador):

| Archivo | Para |
|---|---|
| `GUARD_SIGNING_KEY_PEM_B64.txt`, `GUARD_SIGNING_KID.txt` | La clave del recibo: una sola para todas las instancias. `guard_signing_key.pem` es la copia de respaldo |
| `GATEWAY_CLAVES_JSON.json` | El registro de integradores (solo el sha256 de la clave) |
| `clave_integrador_demo.txt` | La clave `psg_…`: se la das al integrador (y al smoke, con `KAIAI_CLAVE`). **No** va a Vercel |
| `GATEWAY_COMERCIOS_JSON.json`, `TUNEL_PREFIJO.txt` | El comercio de la simulación detrás del túnel, con un prefijo secreto de ruta (`--sin-prefijo` lo saca) |
| `WEBHOOK_SECRETO_<ID>.txt` | Con `--webhook-url https://…`: el secreto del webhook |
| `vercel.env` | Todo lo que va al servidor, en formato `.env` (para `local_multi.py --env`) |

No pisa nada salvo `--forzar`: si cambia la clave del recibo, los integradores
tienen que volver a bajar `/v1/public-key`.

**Clave de solo lectura (para el panel).** El panel web corre en el navegador
con una clave pegada por el usuario: tiene que ser una de **solo lectura**
(GET en `/v1`; no sirve para comprar, contestar, aprobar ni para A2A; ver
[`docs/INTEGRACION.md` §4](../../docs/INTEGRACION.md#claves-de-solo-lectura)).
Para un integrador que ya está en `GATEWAY_CLAVES_JSON.json`:

```sh
$PY deploy/vercel/generar_secretos.py --clave-lectura demo
```

| Archivo | Qué cambia |
|---|---|
| `clave_lectura_demo.txt` | Nuevo: la clave `psg_…` de lectura, para el usuario del panel. **No** va a Vercel |
| `GATEWAY_CLAVES_JSON.json` | Suma la entrada `{"kid", "sha256", "creada", "activa": true, "permisos": ["lectura"]}` a las `claves` del integrador; lo demás queda igual |
| `vercel.env` | La línea `GATEWAY_CLAVES_JSON=` con el registro nuevo (si el archivo está) |

No imprime la clave (solo el `kid`). Si `clave_lectura_<id>.txt` ya existe no
toca nada; con `--forzar` suma **otra** clave de lectura y la anterior sigue
activa hasta que le pongas `"activa": false`. `--clave-lectura` va solo: no
se combina con `--dominio`, `--webhook-url`, `--comercio` ni `--nombre`.

Las claves se leen al arrancar, así que **hasta redesplegar la clave nueva no
vale**. Estos dos comandos cambian producción: corrélos vos (si los corre un
agente, que te pida confirmación antes):

```sh
npx vercel env add GATEWAY_CLAVES_JSON production --force < .temp-db/vercel/GATEWAY_CLAVES_JSON.json
npx vercel deploy --prod
```

Para darla de baja, `"activa": false` en esa entrada (o sacarla) y los mismos
dos comandos.

### 4. Variables en Vercel

Cada una con `vercel env add <NOMBRE> production`, leyendo el valor de su
archivo (así no queda en la historia del shell). La lista completa, con las
opcionales y las **prohibidas**, está en [`env.ejemplo`](env.ejemplo).

```sh
D=.temp-db/vercel
npx vercel env add GATEWAY_CLAVES_JSON production < $D/GATEWAY_CLAVES_JSON.json
npx vercel env add GATEWAY_COMERCIOS_JSON production < $D/GATEWAY_COMERCIOS_JSON.json
npx vercel env add GUARD_SIGNING_KEY_PEM_B64 production < $D/GUARD_SIGNING_KEY_PEM_B64.txt
npx vercel env add GUARD_SIGNING_KID production < $D/GUARD_SIGNING_KID.txt
npx vercel env add SUPABASE_URL production
npx vercel env add SUPABASE_SERVICE_ROLE_KEY production    # pegala cuando la pide
npx vercel env add GOOGLE_API_KEY production               # intención (y OPENAI_API_KEY o TYPESAFE_API_KEY para el filtro)
printf 'https://<tu-proyecto>.vercel.app' | npx vercel env add GATEWAY_PUBLIC_URL production
printf supabase       | npx vercel env add GATEWAY_ESTADO production
printf filtro:filtrar | npx vercel env add GUARD_FILTRO production
printf 270 | npx vercel env add PROXY_PRESUPUESTO_LLAMADA_S production
printf 60  | npx vercel env add PROXY_ESPERA_CONTEXTO_S production
printf 150 | npx vercel env add PROXY_ESPERA_DECISION_S production
npx vercel env ls production                               # nombres, sin valores
```

- `GATEWAY_PUBLIC_URL` es el dominio de **producción** del proyecto (https,
  sin barra final): sale en la Agent Card, en la `a2a_url` de cada compra y
  en el OpenAPI. Si tenés Deployment Protection prendido, las URLs de cada
  deploy piden login de Vercel: a los integradores dales siempre el dominio de
  producción.
- El filtro es obligatorio. Con el modelo necesita un cliente de decisiones
  (`OPENAI_API_KEY` o `TYPESAFE_API_KEY`); sin cliente y sin
  `FILTRO_SOLO_HEURISTICAS=1`, bloquea todo (falla cerrado).
- **Prohibidas:** `PROXY_ALLOW_STUB`, `PROXY_PERMITIR_SIN_FILTRO`,
  `PROXY_FICHA_DEMO`, `CATALOGO_RESPALDO_FIXTURE` y
  `GATEWAY_WEBHOOKS_INSEGUROS`. Si alguna está prendida, el motor no
  arranca (responde 503 `motor_no_configurado` a todo, con el motivo en los
  logs y sin secretos).
- **Tiempos:** sin las variables, el presupuesto por llamada es 270 s, la
  espera del porqué 60 s y la de la decisión 150 s. Se pueden bajar;
  subirlas por encima de esos topes no arranca. También tiene que valer
  presupuesto + `GATEWAY_BARRERA_S` (10 s) + 15 ≤ 300. Los cuerpos
  (`GATEWAY_MAX_CUERPO_BYTES`, `GATEWAY_MAX_CUERPO_A2A_BYTES`) y la
  respuesta del comercio (`PROXY_MAX_RESPUESTA_BYTES`), hasta 4 MB.
- **Latido:** entre el cliente y la función, Vercel corta una conexión sin
  bytes a los 60 s. Un pedido A2A retenido (el porqué, una decisión)
  empieza a responder a los `GATEWAY_LATIDO_S` (15 s) con `200` y manda un
  espacio cada 15 s antes del JSON (en `message/stream`, un comentario
  `: latido`); un error final va como error JSON-RPC con
  `data.http_status`. Se puede bajar (hasta 30 s como mucho); apagarlo
  (`0`, `off`) o subirlo de 30 no arranca.
- **Retención:** `GATEWAY_RETENCION_DIAS` (90 por defecto; en Vercel, hasta
  365: más no arranca). La purga la dispara el cron de `vercel.json`, que
  necesita `CRON_SECRET` (ver el [paso 9](#9-retención-y-purga)).
- Las variables nuevas valen recién en el deploy siguiente.

### 5. El túnel a la simulación

Levantá la simulación y el enrutador, y exponé **un solo** puerto:

```sh
bash world/run_world.sh general                               # comercio :8001 y catálogo :8011
$PY deploy/vercel/tunel_router.py --prefijo "$(cat .temp-db/vercel/TUNEL_PREFIJO.txt)"
ngrok http 8090 --url https://<tu-dominio>.ngrok-free.app     # en otra terminal
```

- **ngrok con dominio estático** (recomendado: la cuenta gratis trae uno). La
  URL no cambia entre corridas, así `GATEWAY_COMERCIOS_JSON` no cambia. El
  aviso de ngrok para navegadores no afecta al motor (no es un navegador).
- **cloudflared:** con un túnel con nombre y un dominio tuyo la URL es fija
  (`cloudflared tunnel create kaiai-sim`, `cloudflared tunnel route dns
  kaiai-sim sim.<tu-dominio>`, `cloudflared tunnel run --url
  http://127.0.0.1:8090 kaiai-sim`). El túnel rápido
  (`cloudflared tunnel --url http://127.0.0.1:8090`) da una URL
  `trycloudflare.com` nueva cada vez: hay que regenerar
  `GATEWAY_COMERCIOS_JSON` (`generar_secretos.py --sin-recibo
  --sin-integrador --dominio … --forzar`), cargarla de nuevo y redeployar.
  Cloudflare corta las respuestas que tardan más de 100 s (524): un
  `create_checkout` lento del comercio del sample puede caer ahí.
- El **prefijo** hace que el enrutador solo atienda
  `https://<dominio>/<prefijo>/…`: el dominio del túnel es público y el
  comercio llama a un LLM en cada checkout. El registro de comercios ya lo
  lleva.
- El registro exige `https` fuera de loopback: el túnel termina el TLS.

### 6. Deploy

```sh
npx vercel deploy --prod
```

### 7. Smoke

```sh
GW=https://<tu-proyecto>.vercel.app
curl -s "$GW/v1/openapi.json" | head -c 200          # público, sin clave
curl -s "$GW/v1/public-key"                          # el kid de GUARD_SIGNING_KID
curl -si "$GW/no-existe" | head -n 12                # 404 del motor (JSON), no NOT_FOUND de Vercel
export KAIAI_CLAVE="$(cat .temp-db/vercel/clave_integrador_demo.txt)"
$PY -m proxy.scripts.smoke_v1_general --gateway "$GW" --comercio merchant_1 \
    --pedido "un monitor de 27 pulgadas" --tope 400 --categoria 305 --esperado aprobado
$PY proxy/scripts/smoke_v1_pausas.py --gateway "$GW"                 # aceptar
$PY proxy/scripts/smoke_v1_pausas.py --gateway "$GW" --caso sin-decidir
```

En Vercel no hay `X-Balanceador-Instancia`: `--otra-instancia` y los casos
`matar-*` son solo para la prueba local. Los logs: `npx vercel logs <url>`.

### 8. Consola web

**Consola web: https://kaiai-motor.vercel.app/consola** (y `GET /` → 307 a
`/consola`). La sirve el mismo motor (`create_app(..., consola=True)`,
estáticos en `proxy/consola/`, sin clave y sin estado) y hace lo que el agente
de un integrador: con la clave de `.temp-db/vercel/clave_integrador_<id>.txt`
pegada en la página, compra, búsqueda con las pausas (porqué y propuesta),
checkout, aprobación de un `escalar`, verificación del recibo y `/v1/filtrar`.
No tiene permisos propios: cada paso es una llamada a la API con esa clave.
CSP estricta (`default-src 'none'`, `connect-src 'self'`), `nosniff`,
`no-referrer` y `no-store`; sin recursos externos.

```sh
curl -sI "$GW/" | grep -i '^location'            # /consola
curl -s -o /dev/null -w '%{http_code}
' "$GW/consola"   # 200
```

En la prueba local con varias instancias es la misma página:
`http://127.0.0.1:8040/consola`.

### 8b. Panel del cliente

**Panel del cliente: https://kaiai-motor.vercel.app/panel.** Lo sirve el mismo
motor (`create_app(..., panel=True)`, estáticos en `proxy/panel/`, sin clave
y sin estado; lo prende `crear_app_serverless` junto a la consola). Es para la
empresa cliente: entra con su **clave de solo lectura** y ve el resumen del
período, la lista de sus compras con filtros y la explicación de cada una (si
la intentaron estafar, la línea de tiempo, qué revisamos y el recibo, con la
firma verificada en el navegador). Detalle para el integrador:
[`docs/INTEGRACION.md` §5.9](../../docs/INTEGRACION.md#59-el-panel-del-cliente).

1. Generá la clave de lectura del integrador (solo suma la entrada al registro;
   no imprime el secreto):

   ```sh
   $PY deploy/vercel/generar_secretos.py --clave-lectura <integrador>
   ```

   Cargá el `GATEWAY_CLAVES_JSON` nuevo en Vercel y redesplegá (paso 3), y
   pasale a la empresa la clave de `.temp-db/vercel/clave_lectura_<integrador>.txt`
   por un canal seguro.
2. Comprobá la página y que la clave sea de lectura (el `HEAD` es lo mismo que
   usa el panel para avisar si entraron con una completa):

   ```sh
   LECTURA=$(cat .temp-db/vercel/clave_lectura_<integrador>.txt)
   curl -s -o /dev/null -w '%{http_code}\n' "$GW/panel"                                         # 200
   curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $LECTURA" "$GW/v1/resumen"  # 200
   curl -s -o /dev/null -w '%{http_code}\n' -I -H "Authorization: Bearer $LECTURA" "$GW/v1/comercios"  # 403
   ```

Misma CSP que la consola (`default-src 'none'`, `connect-src 'self'`),
`nosniff`, `no-referrer` y `no-store`; sin recursos externos. En la prueba
local: `http://127.0.0.1:8040/panel`; con `python -m proxy`, `GATEWAY_PANEL=1`.

### 9. Retención y purga

Las tablas `gw_*` (compras, contextos, ofertas, últimos veredictos,
aprobaciones, esperas, eventos del historial y entregas de webhooks) se
guardan `GATEWAY_RETENCION_DIAS` días (90). Lo más viejo lo borra una purga
diaria: `vercel.json` tiene un cron (`"crons"`, todos los días a las 07:00
UTC, lo que permite Hobby) que llama a `GET /interno/purgar`.

- **El secreto:** Vercel manda `Authorization: Bearer $CRON_SECRET` en cada
  cron. Generá uno (16 caracteres o más, sin espacios; Vercel pide 16) y cargalo:

  ```sh
  $PY -c "import secrets; print(secrets.token_urlsafe(32))" | npx vercel env add CRON_SECRET production
  ```

  El motor lo acepta como `CRON_SECRET` o como `GATEWAY_PURGA_SECRETO` (si
  están las dos, tienen que ser iguales). **Sin secreto la ruta no existe**
  (404) y el cron no borra nada: no hay que apagarlo. Un secreto mal puesto
  (corto, con espacios, o los dos distintos) deja la purga apagada y lo dice
  en el log, sin tumbar el motor.
- **Quién puede:** solo ese secreto. La ruta no está bajo `/v1`: ninguna
  clave de API (completa ni de solo lectura) la usa (401), no está en la
  OpenAPI y en el oyente local de `python -m proxy` no existe.
- **A mano:** `curl -s "$GW/interno/purgar" -H "Authorization: Bearer $CRON_SECRET"`
  responde `{"antes_de", "retencion_dias", "borradas": {"gw_compras": n, …}}`.
  Si Supabase falla, 503: lo que ya se borró queda borrado y la próxima
  sigue.
- Con `005_historial.sql` aplicada, cada `DELETE` va por un índice de fecha.

## Prueba local con varias instancias

Antes de deployar, lo mismo en tu máquina con `local_multi.py`: N procesos
`uvicorn index:app` (:8041, :8042, …) con el mismo entorno y un balanceador
en :8040 que reparte **cada pedido** a la instancia siguiente (también dos
pedidos por la misma conexión), con streaming, corte a los 300 s (504
`FUNCTION_INVOCATION_TIMEOUT`), tope de 4,5 MB (413) y
`X-Balanceador-Instancia` en cada respuesta. Es el peor caso de Vercel: nada
de la memoria de una instancia la ve otra.

1. La simulación: `bash world/run_world.sh general`.
2. Los secretos, sin `--dominio` (el comercio local va por loopback, sin túnel):
   `$PY deploy/vercel/generar_secretos.py --carpeta .temp-db/vercel-local`.
3. Un `.temp-db/vercel-local/local.env` a partir de [`env.ejemplo`](env.ejemplo), con:

   ```sh
   GATEWAY_PUBLIC_URL=http://127.0.0.1:8040
   GATEWAY_COMERCIOS_JSON={"por_defecto":"merchant_1","comercios":[{"id":"merchant_1","nombre":"Simulación","a2a_url":"http://127.0.0.1:8001/a2a/merchant_agent","catalogo_url":"http://127.0.0.1:8011","merchant_ids":["merchant_1"]}]}
   GATEWAY_ESTADO=supabase
   SUPABASE_URL=…
   SUPABASE_SERVICE_ROLE_KEY=…
   GUARD_FILTRO=filtro:filtrar
   FILTRO_SOLO_HEURISTICAS=1
   GOOGLE_API_KEY=…
   PROXY_PRESUPUESTO_LLAMADA_S=270
   PROXY_ESPERA_CONTEXTO_S=60
   PROXY_ESPERA_DECISION_S=150
   ```

4. Las instancias y el balanceador:

   ```sh
   $PY deploy/vercel/local_multi.py --instancias 2 \
       --env .temp-db/vercel-local/vercel.env --env .temp-db/vercel-local/local.env
   ```

   Arrancan con un entorno **limpio** (lo esencial del sistema más los
   `--env`; el último gana; un valor vacío es "sin definir"), como en
   Vercel: si falta una variable, se ve acá. `--heredar-entorno` les pasa
   además el tuyo. Los valores nunca se imprimen, solo los nombres.
5. El smoke de las pausas, con las respuestas por otra instancia:

   ```sh
   export KAIAI_CLAVE="$(cat .temp-db/vercel-local/clave_integrador_demo.txt)"
   $PY proxy/scripts/smoke_v1_pausas.py --otra-instancia                     # aceptar
   $PY proxy/scripts/smoke_v1_pausas.py --otra-instancia --caso rechazar
   $PY proxy/scripts/smoke_v1_pausas.py --otra-instancia --caso omitir
   $PY proxy/scripts/smoke_v1_pausas.py --caso sin-porque
   $PY proxy/scripts/smoke_v1_pausas.py --caso sin-decidir
   $PY proxy/scripts/smoke_v1_pausas.py --caso matar-porque --gracia 30
   $PY proxy/scripts/smoke_v1_pausas.py --caso matar-oportunidad
   ```

| Caso | Qué exige |
|---|---|
| `aceptar` | La pregunta llega por GET a cualquier instancia; la respuesta y la decisión, por otra; la búsqueda trae `guard.parametros_autorizados` del cart de la oportunidad; el checkout de ese cart llega a la firma (`--esperado` para otro status) |
| `rechazar` | Sin parámetros nuevos: el checkout sigue lo pedido |
| `omitir`, `sin-porque` | Sigue el pedido literal (al omitir o al vencer la pregunta) |
| `sin-decidir` | La búsqueda termina `failed` "KaiAI: en pausa…", la compra `vencida` y el A2A siguiente da 409 `compra_vencida` |
| `matar-porque` | Con la pregunta abierta mata la instancia que retiene la búsqueda: la compra se sigue viendo en espera hasta `vence` + la gracia (`GATEWAY_GRACIA_ESPERA_S`, `--gracia`) y después sigue literal (`negociando`); una búsqueda nueva no se queda colgada |
| `matar-oportunidad` | Igual con la oportunidad abierta: pasada la gracia, `vencida` y 409 |

`--aprobar-doble` (si el checkout escala, por ejemplo con
`--escenario general_otra_categoria`): dos aprobaciones a la vez en dos
instancias dan una sola, que sirve una vez.

Para matar una instancia a mano a mitad de una prueba:
`local_multi.py --matar 1 --a-los 40`, o
`curl -X POST http://127.0.0.1:8040/__balanceador/matar/1`. Lo que estaba en
curso en ella termina en 500 `FUNCTION_INVOCATION_FAILED` y lo siguiente va a
las otras. `GET /__balanceador/estado` muestra cuáles siguen vivas. Las rutas
`/__balanceador/*` y los headers `X-Balanceador-*` son solo del balanceador
(no llegan al motor) y solo responden desde esta máquina.

### Prueba local sin Supabase

Para probar varias instancias sin tocar el Supabase del equipo,
`postgrest_local.py` sirve por HTTP en `127.0.0.1:54321` el PostgREST falso
de los tests (`proxy/tests/postgrest_falso.py`): las tablas `gw_*` de 003 y
004 en memoria, con sus índices únicos, claves foráneas y el trigger de
`gw_compras`. Todas las instancias le hablan a ese, como a la base. Al
cortarlo se pierde todo. Solo escucha en loopback (no tiene autenticación).

```sh
$PY deploy/vercel/postgrest_local.py                         # terminal 1
$PY deploy/vercel/generar_secretos.py --carpeta .temp-db/vercel-local
cp deploy/vercel/local_sin_supabase.env.ejemplo .temp-db/vercel-local/local.env
$PY deploy/vercel/local_multi.py --instancias 2 \
    --env .temp-db/vercel-local/vercel.env --env .temp-db/vercel-local/local.env   # terminal 2
curl -s http://127.0.0.1:8040/v1/public-key                  # el kid de vercel.env
curl -s http://127.0.0.1:54321/__tablas                      # cuántas filas hay en cada tabla
```

[`local_sin_supabase.env.ejemplo`](local_sin_supabase.env.ejemplo) trae
`SUPABASE_URL=http://127.0.0.1:54321` (http solo vale contra loopback), el
comercio de la simulación por loopback y el filtro solo con heurísticas.
Sin `GOOGLE_API_KEY` la intención queda apagada (pedido literal): para el
smoke de las pausas (paso 5) agregala. Con la simulación levantada (paso 1)
sirve el mismo smoke.

## Qué se sube y qué no

- **Sube:** `index.py`, `vercel.json`, `pyproject.toml` (y `uv.lock` o
  `requirements.txt` si están), `.python-version`, los paquetes del motor
  (`contracts`, `guard`, `filtro`, `intencion`, `decisiones`, `taxonomia`,
  `fabricante`, `proxy`) sin sus `tests/` ni `proxy/scripts/`, la taxonomía
  (`taxonomia/datos/…subconjunto.txt`) y las fichas del fabricante
  (`fixtures/fabricante/`).
- **No sube:** `.env` y `.env.*`, `.temp-db/`, `.logs/`, `*.pem`, `*.key`,
  `guard_api_token`, `.claude/`, `.mcp.json`, `world/`, `dashboard/`,
  `deploy/`, `docs/`, `results/`, el resto de `fixtures/`, los tests, las
  carpetas de venv y caché, y **cualquier archivo nuevo en la raíz** (lista
  blanca). Vercel por su cuenta solo ignora `.env.local` y `.env.*.local`: el
  `.env` de la raíz se subiría sin `.vercelignore`.
- Si un lote suma a la raíz algo que el build necesita, va con `!` en la
  lista blanca de `.vercelignore` y en `RAIZ_DEL_BUILD` del test.

`proxy/tests/test_despliegue_vercel.py` emula el `.vercelignore` (semántica
de `.gitignore` más lo que ignora Vercel) sobre `git ls-files` y sobre lo
que hay en disco, y falla si algo con forma de secreto (`.env*`, `*.pem`,
`*_key*`, `guard_api_token`) o de la simulación se subiría. Con
`[tool.kaiai.motor]` en `pyproject.toml` y con `index.py` comprueba además
que `excludeFiles` no saque nada que el motor importe en runtime.
