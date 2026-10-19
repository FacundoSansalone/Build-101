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

- **Redeploy** (código nuevo, misma VM y misma URL):
  `TENKI_SESSION=<id> bash deploy/tenki.sh`. La clave del Guard
  (`.temp-db/`) se conserva.
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
- Si el túnel le cambia el `Host` al panel. Si `/api/*` da 403
  `host_no_permitido`, agregá el host que aparece en
  `.logs/dashboard.log` con `DASHBOARD_EXTRA_HOSTS`.

## Variables

| Variable | Default | Para qué |
|---|---|---|
| `DASHBOARD_PUBLIC_ORIGINS` | — | Origen público del panel (`https://dominio`). Lo agrega a los `Origin` y `Host` que acepta `/api/*`. Si está, el panel escucha en `0.0.0.0`. `tenki.sh` lo completa solo. |
| `DASHBOARD_EXTRA_HOSTS` | — | `Host` extra (si el túnel lo reescribe). |
| `GUARD_FILTRO` | `filtro:filtrar` | Filtro del proxy. |
| `FILTRO_SOLO_HEURISTICAS` | `1` | Con el modelo, el filtro corta el desafío OTP del comercio honesto ([DEMO.md](../docs/DEMO.md)). |
| `DEMO_PAUSA_AUTO_S` | `8` | Espacia las respuestas automáticas de `/demo` (plan gratuito de Gemini). `0` con una clave paga. |
| `DEMO_HOY` | — | Sin fijar: hoy de verdad (el comercio general arma las fechas desde hoy). Solo para reproducir una grabación de un escenario fijo, que tiene fechas fijas ("el lunes" es el 2026-10-19): `DEMO_HOY=2026-10-18`, junto con `PROXY_FICHA_DEMO=1`. |
| `PROXY_FICHA_DEMO` | `0` | `0`: manda la ficha que confirmó el usuario. `1`: la del escenario fijo (grabaciones, benchmark por el proxy, `smoke_checkout.py`). |
| `PROXY_MAX_SUSCRIPTORES_SSE` | `200` | Suscriptores de `/guard/events` a la vez. Cada pestaña del panel abre una: con el default del proxy (20), la pestaña 21 se queda sin eventos. |

El `.env` de la raíz viaja a la VM tal cual (necesita `GOOGLE_API_KEY`; las
`SUPABASE_*` son opcionales) y puede pisar cualquiera de estas variables.

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

## A tener en cuenta

- **La URL es pública y no tiene contraseña.** Cualquiera que la tenga gasta
  la cuota de Gemini (4 agentes llaman al LLM en cada compra). Compartila solo
  con quien la necesite.
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
