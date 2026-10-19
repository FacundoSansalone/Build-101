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
| [`vercel.json`](../../vercel.json) | La función `index.py`: `maxDuration: 300` (el máximo de Hobby) y `excludeFiles` (la simulación y lo que no es runtime) |
| [`.vercelignore`](../../.vercelignore) | Lo que **no** sube `vercel deploy`: lista blanca en la raíz (solo el motor) y lista negra adentro (secretos, simulación, tests). Lo verifica `proxy/tests/test_despliegue_vercel.py` |
| `env.ejemplo` | Todas las variables, obligatorias, opcionales y **prohibidas** |
| `comercios.ejemplo.json` | Formato de `GATEWAY_COMERCIOS_JSON` con la simulación detrás del túnel |
| `generar_secretos.py` | Clave del recibo, clave de integrador y los registros JSON, cada uno en su archivo. No imprime secretos |
| `verificar_supabase.py` | Solo lectura: ¿están las tablas y columnas de las migraciones 003 y 004? |
| `tunel_router.py` | Un solo puerto (:8090) para la simulación: `/a2a/*` al comercio, el resto al catálogo |
| `local_multi.py` | Vercel en tu máquina: N instancias de `index:app` y un balanceador por pedido en :8040 |
| [`proxy/scripts/smoke_v1_pausas.py`](../../proxy/scripts/smoke_v1_pausas.py) | Prueba de las pausas (porqué y oportunidad) con las respuestas por otras instancias |

Los comandos van desde la raíz del repo, con el venv de `world/ap2`
(`PY=world/ap2/.venv/Scripts/python` en Windows con Git Bash,
`world/ap2/.venv/bin/python` en Linux y macOS) y `PYTHONUTF8=1`.

## Antes de empezar

- **Lo que ponen los otros lotes:** `index.py` (raíz, `from proxy.vercel import app`),
  `proxy/vercel.py`, el `pyproject.toml` raíz con las dependencias del motor,
  `.python-version` (3.12) y `proxy/sql/004_serverless.sql`. Sin `index.py`,
  el deploy falla (`functions` de `vercel.json` no encuentra la función).
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
tiene un preset con nombre como FastAPI o Flask: si el build no encontrara la
entrada, `[tool.vercel] entrypoint = "index:app"` en `pyproject.toml` la fija
(la clave de `functions` sigue siendo `index.py`). Después del deploy, una
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
