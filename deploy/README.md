# Deploy de la demo

La demo entera (los 5 procesos de `world/`, el proxy del Guard y el dashboard)
corre en **una sola máquina** y habla por `127.0.0.1` igual que en local. Lo
único que se publica es el dashboard (:8020). La compra se hace en
**`/demo`**.

Opcionalmente, el mismo deploy publica el **gateway de producción** (:8030)
para integradores externos. Solo con un archivo de claves: ver
[Gateway (opcional)](#gateway-opcional).

| Script | Qué hace |
|---|---|
| `start_all.sh [escenario]` | Levanta todo en esta máquina con el agente pasando por el proxy, espera a que responda y, si un proceso muere, apaga todo. Logs en `.logs/`. Escenario por defecto `general` (el comercio general: cualquier pedido, cualquier día). Funciona en Linux, macOS y Windows con Git Bash. |
| `tenki.sh [escenario]` | Crea una VM en Tenki Sandbox, sube el código y el `.env`, instala dependencias, publica el :8020 (y el :8030 si hay archivo de claves) y corre `start_all.sh` adentro. |
| `railway/` | La demo entera **siempre prendida** en un servicio de Railway: un contenedor con `start_all.sh` adentro y el enrutador del túnel como única puerta (el comercio con prefijo secreto para el motor de Vercel, el resto al panel con contraseña). Ver [Railway](#railway-siempre-prendida). |
| `vercel/` | El **motor solo** (gateway, filtro, intención y Guard) en Vercel, con el estado en Supabase; la simulación queda detrás de un túnel desde tu máquina o en Railway. Ver [Vercel](#vercel-solo-el-motor). |

## Local

```sh
bash deploy/start_all.sh                    # comercio general → http://localhost:8020/demo
bash deploy/start_all.sh general_inyeccion  # otro comportamiento del comercio
```

Por defecto manda la ficha que confirma el usuario en `/demo`
(`PROXY_FICHA_DEMO=0`) y las fechas son las de hoy (sin `DEMO_HOY`). Los
escenarios fijos (`notebook_honesto`, `computadoras`, `B`…) quedan para el
benchmark, los tests y las grabaciones. Para reproducir una grabación con su
ficha y sus fechas fijas:

```sh
DEMO_HOY=2026-10-18 PROXY_FICHA_DEMO=1 bash deploy/start_all.sh computadoras
```

Para probar el gateway por `/v1` sin LLM de nuestro lado, con la demo
levantada (usa el comercio :8001 y el catálogo :8011; arma su propio gateway
con una clave temporal, sin abrir puertos):

```sh
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/bin/python -m proxy.scripts.smoke_v1_general \
    --pedido "un monitor de 27 pulgadas" --tope 400 --categoria 305
```

## Tenki Sandbox

Tenki Sandbox da una microVM Linux con una URL `https://` por puerto
publicado. Con una sesión *sticky*, la VM queda prendida hasta que se la
apaga, así que no hay que separar los procesos ni tocar `world/`.

```sh
# una vez: instalar la CLI de Tenki (https://tenki.cloud/docs/sandbox/quickstart)
tenki login                              # o TENKI_API_KEY=tk_...
bash deploy/tenki.sh                     # imprime la URL: https://…/demo
```

**El panel publicado pide contraseña** (HTTP Basic: el navegador la pide una
vez; usuario cualquiera, por ejemplo `kaiai`). `tenki.sh` usa
`DASHBOARD_ACCESS_TOKEN` (del entorno o del `.env`) o, si no hay, genera una
en `.temp-db/tenki_dashboard_token` (ignorado por git) y la reusa en cada
redeploy; al terminar imprime de dónde sacarla. Viaja a la VM en
`.temp-db/deploy.env` (modo 600), no por la línea de comandos. Sin
contraseña, el panel publicado no arranca: Host, Origin y Sec-Fetch-Site solo
frenan navegadores, y con `curl` alguien con la URL podría apagar el Guard o
aprobar compras (el panel les agrega el token del proxy).

- **Redeploy** (código nuevo, misma VM y misma URL):
  `TENKI_SESSION=<id> bash deploy/tenki.sh`. La clave del Guard
  (`.temp-db/`) se conserva, y la contraseña del panel también.
- **Cambiar la contraseña:** borrá `.temp-db/tenki_dashboard_token` (o
  definí otra `DASHBOARD_ACCESS_TOKEN`) y volvé a correr el redeploy.
- **Pausar fuera de la demo:** `tenki sandbox pause --session <id>`. Se
  conservan la memoria y el disco, pero se cortan las conexiones. Al
  reanudar (`resume`), volvé a correr `tenki.sh` con `TENKI_SESSION`.
- **Logs:** `tenki sandbox exec --session <id> -c 'tail -n 50 ~/app/.logs/*.log'`.
- **Apagar del todo:** `tenki sandbox terminate --session <id>`.

**Costo** (tarifa de Tenki, se cobra por segundo): 2 vCPU + 4 GiB ≈
$0.17/h ≈ $4/día. Los $50 de crédito de alta alcanzan para ~12 días
prendida todo el tiempo. Se puede cambiar con `TENKI_CPU` y
`TENKI_MEMORY_MB`.

**Lo que la doc de Tenki no fija** y el script resuelve de forma tolerante:

- El formato en que `create` imprime el id de la sesión. Si no lo
  encuentra, pasalo a mano con `TENKI_SESSION`.
- El formato en que `expose` imprime la URL. Si no la encuentra, pasala con
  `PREVIEW_URL`.
- Si la CLI falla al consultar la VM mientras espera el arranque (red,
  rate limit): reintenta, y a las 6 fallas seguidas corta con el error de la
  CLI (no lo confunde con "start_all.sh terminó": el comando remoto contesta
  con una marca explícita).
- Si el túnel le cambia el `Host` al panel. Si `/api/*` da 403
  `host_no_permitido`, agregá el host que aparece en
  `.logs/dashboard.log` con `DASHBOARD_EXTRA_HOSTS`.

## Railway (siempre prendida)

La demo entera en **un servicio de Railway**, prendida siempre, sin depender
de ninguna máquina: lo que usa el motor de Vercel (el comercio, el procesador
de pagos, el proveedor de credenciales y el catálogo) y la demo `/demo` (el
agente de compras, el proxy y el panel). Es un solo contenedor
([`railway/Dockerfile`](railway/Dockerfile)) que corre `start_all.sh general`
con todo en `127.0.0.1`, como acá; Railway publica un solo puerto (`PORT`)
y ahí escucha solo el enrutador de `vercel/tunel_router.py`
([`railway/arrancar.sh`](railway/arrancar.sh)):

| Ruta | Va a |
|---|---|
| `/<TUNEL_PREFIJO>/a2a/*` | el Merchant Agent (:8001): lo que usa el motor de Vercel |
| `/<TUNEL_PREFIJO>/…` | el catálogo público (:8011) |
| `/salud` | 200 si el comercio, el catálogo y el panel contestan (el healthcheck de Railway); no dice qué falta |
| todo lo demás | el panel (:8020, `/demo`), con contraseña (`DASHBOARD_ACCESS_TOKEN`, HTTP Basic) |

La dev-ui de ADK, el proxy local, los otros agentes y el panel escuchan solo
en `127.0.0.1` (el panel con `DASHBOARD_SOLO_LOOPBACK=1`). Las variables
obligatorias son `GOOGLE_API_KEY`, `DASHBOARD_ACCESS_TOKEN` (16 o más),
`TUNEL_PREFIJO` (el de `.temp-db/vercel/TUNEL_PREFIJO.txt`, el mismo que usa
el motor) y `DASHBOARD_PUBLIC_ORIGINS` (`https://<dominio de Railway>`);
`arrancar.sh` las valida sin mostrarlas. Lo que nunca entra a la imagen lo
fija [`.dockerignore`](../.dockerignore) y lo verifica
`proxy/tests/test_despliegue_railway.py`.

```sh
npx @railway/cli login && npx @railway/cli init --name kaiai-demo   # una vez (ver railway/README.md)
npx @railway/cli up --detach                                          # cada deploy
bash deploy/railway/apuntar_vercel.sh <servicio>.up.railway.app       # una vez: el motor de Vercel → Railway
```

Todo el paso a paso (el archivo de configuración en el panel de Railway, el
dominio, las variables y de dónde sale cada una, el deploy, las pruebas, los
logs y el costo, ~1 GB de RAM, unos US$10-15 al mes) está en
[`railway/README.md`](railway/README.md).

## Variables

| Variable | Default | Para qué |
|---|---|---|
| `DASHBOARD_PUBLIC_ORIGINS` | — | Origen público del panel (`https://dominio`). Lo agrega a los `Origin` y `Host` que acepta `/api/*`. Si está, el panel escucha en `0.0.0.0`. `tenki.sh` lo completa solo. |
| `DASHBOARD_EXTRA_HOSTS` | — | `Host` extra (si el túnel lo reescribe). |
| `DASHBOARD_ACCESS_TOKEN` | — | Contraseña del panel publicado (HTTP Basic, usuario cualquiera, al menos 16 caracteres). Con `DASHBOARD_PUBLIC_ORIGINS`, `DASHBOARD_EXTRA_HOSTS` o el panel escuchando en la red es **obligatoria**: sin ella no arranca. `tenki.sh` genera una si no hay. En local no se usa. |
| `DASHBOARD_PERMITIR_SIN_CLAVE` | `0` | `1` para publicar el panel sin contraseña igual (no recomendado). |
| `DASHBOARD_SOLO_LOOPBACK` | `0` | `1`: el panel publicado escucha igual en `127.0.0.1`, detrás de un enrutador en la misma máquina. Lo fija `railway/arrancar.sh`. |
| `GUARD_FILTRO` | `filtro:filtrar` | Filtro del proxy. |
| `FILTRO_SOLO_HEURISTICAS` | `1` | Con el modelo, el filtro corta el desafío OTP del comercio honesto ([DEMO.md](../docs/DEMO.md)). |
| `DEMO_PAUSA_AUTO_S` | `8` | Espacia las respuestas automáticas de `/demo` (plan gratuito de Gemini). `0` con una clave paga. |
| `DEMO_HOY` | — | Sin fijar: hoy de verdad (el comercio general arma las fechas desde hoy). Solo para reproducir una grabación de un escenario fijo, que tiene fechas fijas ("el lunes" es el 2026-10-19): `DEMO_HOY=2026-10-18`, junto con `PROXY_FICHA_DEMO=1`. |
| `PROXY_FICHA_DEMO` | `0` | `0`: manda la ficha que confirmó el usuario. `1`: la del escenario fijo (grabaciones, benchmark por el proxy, `smoke_checkout.py`). |
| `PROXY_MAX_SUSCRIPTORES_SSE` | `200` | Suscriptores de `/guard/events` a la vez. Cada pestaña del panel abre una: con el default del proxy (20), la pestaña 21 se queda sin eventos. |

El `.env` de la raíz viaja a la VM (necesita `GOOGLE_API_KEY`; las
`SUPABASE_*` son opcionales) y puede pisar cualquiera de estas variables,
**menos** `SUPABASE_ACCESS_TOKEN` (token personal con poder sobre toda la
cuenta de Supabase: solo lo usa `vercel/aplicar_migraciones.py`, en tu
máquina) y las `VERCEL_*` y `TENKI_*`, que `tenki.sh` saca antes de subirlo.
El código sube sin lo ignorado por git y sin los archivos versionados que
borraste del working tree.

Del gateway (todas opcionales; sin `GATEWAY_CLAVES_ARCHIVO` no cambia nada):

| Variable | Default | Para qué |
|---|---|---|
| `GATEWAY_CLAVES_ARCHIVO` | — | Archivo de integradores (claves, comercios y webhook). Con él se levanta y se publica el gateway. En `tenki.sh` es una ruta de **tu** máquina: el script lo sube |
| `GATEWAY_COMERCIOS_ARCHIVO` | — | Registro de comercios. Sin él, el comercio de la demo. `tenki.sh` lo sube solo junto con las claves |
| `GATEWAY_PORT` | `8030` | Puerto del gateway |
| `GATEWAY_PUBLIC_URL` | `http://localhost:8030` | URL pública. Si no es `http://localhost…` ni `http://127.0.0.1…`, `start_all.sh` hace escuchar el gateway en `0.0.0.0`. `tenki.sh` la completa sola |
| `GATEWAY_PREVIEW_URL` | — | Solo `tenki.sh`: la URL pública del :8030, si `expose` no la imprime |
| `GATEWAY_ESTADO` | `memoria` | `supabase` para que las compras sobrevivan a un reinicio (exige la migración `proxy/sql/003_gateway.sql` aplicada a mano) |
| *(los que nombra `secreto_env`)* | — | Secretos de los webhooks de los integradores. Van en el `.env` |

El resto de las `GATEWAY_*` está en
[`proxy/README.md`](../proxy/README.md#variables-del-gateway).

## Gateway (opcional)

El gateway es el proxy del Guard abierto a integradores externos (su API está
en [`docs/INTEGRACION.md`](../docs/INTEGRACION.md)). Corre en el mismo
proceso que el proxy de la demo, en un segundo puerto (:8030), y **solo se
abre si hay al menos una clave activa**.

**1. Armar el archivo de claves** (en tu máquina, fuera de git, por ejemplo
en `.temp-db/`, que está ignorado):

```sh
world/ap2/.venv/bin/python -m proxy.claves nueva --integrador acme
# imprime la clave (una sola vez: entregásela al integrador por un canal
# seguro) y un objeto {"kid", "sha256", "creada", "activa"} para el archivo
```

```json
{"integradores": [{"id": "acme", "nombre": "ACME", "claves": [<el objeto>],
                   "comercios": ["merchant_1"]}]}
```

Con webhook, sumá `"webhook": {"url": "https://…", "secreto_env": "WEBHOOK_SECRETO_ACME"}`
y poné el secreto en el `.env` (`python -m proxy.claves secreto-webhook`
genera uno). El formato completo está en
[`proxy/README.md`](../proxy/README.md#integradores-gateway_claves_archivo).

**2. Local:**

```sh
GATEWAY_CLAVES_ARCHIVO=.temp-db/gateway_claves.json bash deploy/start_all.sh
# → Gateway: http://localhost:8030 (docs: /v1/docs)
```

`start_all.sh` suma el 8030 al chequeo de puertos, le pasa al proxy el
`GATEWAY_PORT` y la `GATEWAY_PUBLIC_URL` que muestra y, después del proxy,
espera `/v1/openapi.json` en ese puerto **solo si el proxy abrió el oyente
público** (`/guard/state` → `gateway.publico`: hace falta al menos una clave
activa). Con todas las claves revocadas o con un archivo que no existe,
avisa y sigue sin gateway (la demo no se cae).

**3. Tenki:** con la variable en el entorno o en el `.env`, `tenki.sh` hace el
resto:

```sh
GATEWAY_CLAVES_ARCHIVO=.temp-db/gateway_claves.json bash deploy/tenki.sh
#   Gateway:        https://…/v1/docs
```

- Sube el archivo de claves a `~/app/.temp-db/gateway_claves.json` (modo
  600) y, si está, el de comercios a `.temp-db/gateway_comercios.json`.
- Publica el :8030 por `https://` (Tenki termina el TLS; el script se niega a
  publicarlo por `http`).
- Escribe en la VM `.temp-db/deploy.env` con las rutas de la VM y la URL
  publicada (`GATEWAY_CLAVES_ARCHIVO`, `GATEWAY_COMERCIOS_ARCHIVO`,
  `GATEWAY_PORT`, `GATEWAY_PUBLIC_URL`). `start_all.sh` lo carga **después**
  del `.env` y le gana: el `.env` subido puede traer rutas de tu máquina.
- Sin `GATEWAY_CLAVES_ARCHIVO`, escribe un `deploy.env` con
  `GATEWAY_CLAVES_ARCHIVO=` y `GATEWAY_COMERCIOS_ARCHIVO=` vacías (pisan las
  rutas de tu máquina que traiga el `.env` subido) y no publica nada más que
  el panel. Un redeploy sin la variable apaga el gateway.
- El panel publicado no muestra las compras de los integradores: el proxy
  las saca de los GET de `/guard/*` que llegan sin su token (el servidor del
  panel los reenvía sin él).

Para rotar una clave o cambiar comercios: editá el archivo y volvé a correr
`tenki.sh` con `TENKI_SESSION` (el proxy relee la configuración solo al
arrancar).

## Vercel (solo el motor)

Otra forma de publicar el gateway: **solo el motor** en Vercel (plan Hobby:
300 s por pedido, varias instancias sin memoria compartida), con el estado en
Supabase (migraciones 003 y 004). La simulación (`world/`, `dashboard/`)
sigue en tu máquina y el motor le habla por un túnel (`vercel/tunel_router.py`
y ngrok), o queda fija en [Railway](#railway-siempre-prendida); los agentes de compras son externos y entran como integradores por
`/v1` y `/a2a/m/{comercio}`. No reemplaza a `start_all.sh` ni a `tenki.sh`:
la demo de `/demo` sigue igual.

Todo el paso a paso (login, Supabase, secretos, variables, túnel, deploy y
smoke) y la prueba local con varias instancias (`vercel/local_multi.py`)
están en [`vercel/README.md`](vercel/README.md). Lo que no se sube lo fija
[`.vercelignore`](../.vercelignore) y lo verifica
`proxy/tests/test_despliegue_vercel.py`.

## A tener en cuenta

- **La URL es pública, con contraseña.** Cualquiera que tenga las dos gasta
  la cuota de Gemini (4 agentes llaman al LLM en cada compra), cambia el
  comercio y puede aprobar compras retenidas. Compartilas solo con quien las
  necesite. La contraseña viaja en cada pedido: publicá el panel solo por
  `https://` (Tenki termina el TLS).
- **`/demo` es una sola conversación compartida.** Todos los que abren la URL
  ven el mismo chat y el mismo comercio. Alcanza para una demo con una
  persona al mando, no para varios usuarios a la vez.
- El estado vive en memoria (sesiones de ADK, compras, aprobaciones): si se
  reinicia, la demo arranca de cero, como en local. Las compras de los
  integradores se pueden guardar en Supabase con `GATEWAY_ESTADO=supabase`.
- **El gateway sí exige clave**, a diferencia del panel: todo `/a2a/*` y
  `/v1/*` del :8030 pide `Authorization: Bearer psg_…` (salvo
  `/v1/openapi.json`, `/v1/docs` y `/v1/public-key`), y `/guard/*` no existe
  ahí. Igual, cada integrador puede gastar la cuota de LLM del operador: no
  hay cuotas por clave.
- **Si `GATEWAY_CLAVES_ARCHIVO` apunta a un archivo que no existe,**
  `start_all.sh` avisa "sigo sin gateway" y saca la variable antes de
  arrancar el proxy (lanzado a mano, `python -m proxy` no arranca: el
  operador pidió el gateway y no hay con qué autenticar). Si el archivo
  existe pero no tiene ninguna clave activa, el proxy arranca sin el :8030 y
  `start_all.sh` sigue sin gateway.
- **Webhooks:** los pendientes se pierden si se reinicia la VM o se pausa
  la sesión; los integradores reconcilian con `GET /v1/compras/{id}`.
