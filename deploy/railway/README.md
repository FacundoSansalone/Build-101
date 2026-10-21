# La demo siempre prendida en Railway

El motor de KaiAI está en Vercel (https://kaiai-motor.vercel.app), pero el
comercio simulado (`world/`) solo existía mientras alguien lo levantaba en su
máquina con `deploy/vercel/tunel_demo.sh`. Acá va **todo lo demás en un
servicio de Railway**, prendido siempre:

- lo que usa el motor de Vercel: el Merchant Agent (:8001), el proveedor de
  credenciales (:8002) y el procesador de pagos (:8003), que el comercio
  llama por `localhost`, y el catálogo público (:8011);
- la demo `/demo`: el agente de compras (:8000, ADK), el proxy local (:8010)
  y el panel (:8020), con contraseña.

Es **un solo servicio y un solo contenedor**, con todo adentro hablando por
`127.0.0.1` como en `deploy/start_all.sh`. Railway le da al servicio un solo
puerto público (`PORT`) con un dominio https, y ahí escucha solo el enrutador
(`deploy/vercel/tunel_router.py`):

```
motor en Vercel ──https──▶ <dominio>.up.railway.app/<TUNEL_PREFIJO>/a2a/*  ──▶ :8001 Merchant Agent
                           <dominio>.up.railway.app/<TUNEL_PREFIJO>/…      ──▶ :8011 catálogo
navegador ───────https──▶ <dominio>.up.railway.app/demo (y todo lo demás) ──▶ :8020 panel (pide contraseña)
Railway ─────────────────▶ <dominio>.up.railway.app/salud                  ──▶ 200 si comercio, catálogo y panel contestan

adentro, solo en 127.0.0.1: :8000 ADK · :8002 credenciales · :8003 pagos · :8010 proxy · :8020 panel
```

| Archivo | Qué es |
|---|---|
| [`Dockerfile`](Dockerfile) | La imagen: Python 3.12, `uv` (versión fija), `uv sync --package ap2-samples` en `world/ap2` (como `run_world.sh`), bash, curl, git y tini; usuario `app` (no root). El contexto es la raíz del repo |
| [`../../.dockerignore`](../../.dockerignore) | Lo que **nunca** entra a la imagen: `.env*`, `.temp-db/`, `.logs/`, `results/`, venvs, `.vercel/`, `.git`, `__pycache__`, claves… Lista blanca en la raíz (un archivo nuevo ahí no entra). Lo verifica `proxy/tests/test_despliegue_railway.py` |
| [`arrancar.sh`](arrancar.sh) | El entrypoint: valida las variables (sin mostrarlas), levanta `start_all.sh general`, espera el panel y ADK, comprueba que el panel pide contraseña y abre el enrutador en `$PORT`. Si un proceso muere, el contenedor termina y Railway lo reinicia. `--solo-validar` solo revisa las variables |
| [`railway.json`](railway.json) | Build con el Dockerfile, healthcheck en `/salud` (600 s: ADK tarda en arrancar), reinicio `ON_FAILURE`, siempre prendido (`sleepApplication: false`) y **una sola réplica** en US East (`us-east4-eqdc4a`, cerca de `iad1`, donde está el motor de Vercel) |
| [`apuntar_vercel.sh`](apuntar_vercel.sh) | Desde tu máquina: comprueba que la demo responde y apunta el motor de Vercel al dominio de Railway (`GATEWAY_COMERCIOS_JSON` con el mismo prefijo, `vercel env add` y `vercel deploy --prod`). Dice "Listo" solo si todo salió bien |

Los comandos van desde la raíz del repo, en Git Bash en Windows. La CLI de
Railway va con `npx` (no hace falta instalarla); en los ejemplos,
`R="npx @railway/cli"`.

## Antes de empezar

- **El motor en Vercel andando**, con los secretos de
  `deploy/vercel/generar_secretos.py` en `.temp-db/vercel/` (ver
  [`deploy/vercel/README.md`](../vercel/README.md)). De ahí sale el prefijo
  secreto (`TUNEL_PREFIJO.txt`) y es lo que `apuntar_vercel.sh` actualiza.
- **Node** (para `npx`), como para Vercel.
- **La clave de Gemini** (`GOOGLE_API_KEY`), la misma del `.env`.
- **Una cuenta de Railway**. El plan Hobby (US$5 por mes, con US$5 de uso
  incluidos) alcanza; el de prueba gratis también sirve para probar, pero
  puede apagar el servicio cuando se termina el crédito. Ver
  [Costo](#costo).

## Paso a paso

### 1. Cuenta, login y proyecto

```sh
R="npx @railway/cli"
$R login                         # lo hacés vos: abre el navegador (crea la cuenta si no tenés)
$R init --name kaiai-demo        # crea el proyecto y lo asocia a esta carpeta
$R add --service demo            # un servicio vacío (todavía no despliega nada)
$R service link demo             # esta carpeta queda asociada a ese servicio
```

Si el proyecto y el servicio ya existen (por ejemplo, desde otra máquina),
alcanza con `$R link` (interactivo: elegís proyecto, entorno y servicio).

### 2. Que Railway lea `deploy/railway/railway.json`

Railway busca `railway.json` solo en la raíz del repo, y la ruta de otro
archivo no se puede fijar desde el mismo archivo: va en el panel web. Antes
del primer deploy:

`$R open` → servicio **demo** → **Settings** → **Config-as-code** →
**Railway Config File**: `/deploy/railway/railway.json`.

Sin esto, Railway intenta adivinar cómo construir el repo (ve el
`pyproject.toml` de la raíz, que es del motor) y el deploy no sirve.

### 3. El dominio

```sh
$R domain --port 8080            # imprime https://<algo>.up.railway.app
```

Es el dominio fijo del servicio: no cambia entre deploys. `--port 8080` es el
puerto interno al que Railway le manda el tráfico: tiene que ser el mismo que
`PORT` (paso 4).

### 4. Las variables

Cada una en el servicio **demo** (`$R variable set …` o en el panel web,
**Variables**). Los secretos van por `--stdin`, leídos de su archivo: así no
quedan en la historia del shell. `--skip-deploys` evita un deploy por cada
variable.

| Variable | Obligatoria | De dónde sale |
|---|---|---|
| `GOOGLE_API_KEY` | sí | La clave de Gemini (la del `.env`). La usan los agentes del sample y la intención |
| `DASHBOARD_ACCESS_TOKEN` | sí | La contraseña del panel publicado (HTTP Basic, usuario cualquiera), **16 caracteres o más**. Generala (abajo) |
| `TUNEL_PREFIJO` | sí | El prefijo secreto de ruta: **el de `.temp-db/vercel/TUNEL_PREFIJO.txt`**, así el motor de Vercel usa el mismo |
| `DASHBOARD_PUBLIC_ORIGINS` | sí | `https://<el dominio del paso 3>`, sin barra ni ruta. Con eso el panel acepta ese `Host` y ese `Origin`, y pide la contraseña |
| `PORT` | recomendada | `8080` (el mismo del `--port` del paso 3). Sin ella, Railway pone la suya; no puede ser uno de los puertos de adentro (8000-8003, 8010, 8011, 8020, 8030) |
| `DEMO_PAUSA_AUTO_S` | no | `8` por defecto (plan gratuito de Gemini); `0` con una clave paga |
| `OPENAI_API_KEY` o `TYPESAFE_API_KEY` | no | El cliente de decisiones del filtro y del intake. Sin ninguna, el filtro va solo con heurísticas (`FILTRO_SOLO_HEURISTICAS=1`, el default de `start_all.sh`) y el intake con Gemini |
| `ESCENARIO` | no | El comportamiento del comercio al arrancar (`general`); después se cambia desde `/demo` |
| `DASHBOARD_EXTRA_HOSTS` | no | Solo si `/api/*` da 403 `host_no_permitido` (el borde de Railway manda el `Host` público y no hace falta) |

```sh
D=.temp-db/vercel
# La contraseña del panel: una vez, en un archivo ignorado por git.
(umask 077; world/ap2/.venv/Scripts/python -c "import secrets; print(secrets.token_urlsafe(24))" > .temp-db/railway_dashboard_token)
tr -d '\r\n' < .temp-db/railway_dashboard_token | $R variable set DASHBOARD_ACCESS_TOKEN --stdin --skip-deploys
tr -d '\r\n' < $D/TUNEL_PREFIJO.txt              | $R variable set TUNEL_PREFIJO --stdin --skip-deploys
grep -E '^GOOGLE_API_KEY=' .env | cut -d= -f2- | tr -d '\r\n"' | $R variable set GOOGLE_API_KEY --stdin --skip-deploys
$R variable set PORT=8080 --skip-deploys
$R variable set DASHBOARD_PUBLIC_ORIGINS=https://<dominio-del-paso-3> --skip-deploys
$R variable list                 # ojo: muestra los valores; no lo pegues en ningún lado
```

(En Linux o macOS, el Python es `world/ap2/.venv/bin/python`.) En el panel
web, `DASHBOARD_PUBLIC_ORIGINS` también se puede escribir como
`https://${{RAILWAY_PUBLIC_DOMAIN}}` (Railway lo completa con el dominio del
servicio).

### 5. El deploy

```sh
$R up --detach                   # sube esta carpeta y construye en Railway
$R logs --build                  # el build (la primera vez, unos minutos: uv sync)
$R logs                          # el arranque: "KaiAI en Railway" cuando está lista
```

`railway up` sube la carpeta sin lo que ignora `.gitignore` (el `.env`, la
`.temp-db/` y los `.logs/` no salen de tu máquina) y el build deja afuera,
además, todo lo que no está en la lista blanca de `.dockerignore`. Railway
espera a que `/salud` dé 200 (hasta 10 minutos) antes de pasarle el tráfico
al deploy nuevo; si no, lo da por fallido y el anterior sigue.

**O desde GitHub:** servicio → **Settings** → **Source** → **Connect Repo**,
rama `main`. Cada push a `main` despliega solo (con el mismo
`railway.json`). Sirve si el repo está en GitHub y querés que la demo siga a
`main`; si no, `railway up` cuando quieras.

### 6. Probar

```sh
DOM=https://<dominio>
CLAVE="$(cat .temp-db/railway_dashboard_token)"
curl -s "$DOM/salud"                                              # {"estado": "ok"}
curl -s -o /dev/null -w '%{http_code}\n' "$DOM/demo"               # 401 (sin contraseña)
curl -s -o /dev/null -w '%{http_code}\n' -u "kaiai:$CLAVE" "$DOM/demo"   # 200
curl -s -o /dev/null -w '%{http_code}\n' "$DOM/a2a/merchant_agent" # 401: sin el prefijo, al panel, nunca al comercio
curl -s -o /dev/null -w '%{http_code}\n' "$DOM/$(cat .temp-db/vercel/TUNEL_PREFIJO.txt)/products"   # 200: el catálogo
```

En el navegador: `https://<dominio>/demo`, usuario cualquiera (por ejemplo
`kaiai`) y la contraseña de `.temp-db/railway_dashboard_token`.

### 7. Apuntar el motor de Vercel a Railway

```sh
bash deploy/railway/apuntar_vercel.sh <dominio>.up.railway.app
```

Comprueba `/salud` y el catálogo con el prefijo (si el `TUNEL_PREFIJO` de
Railway no es el del archivo, lo dice), reescribe `GATEWAY_COMERCIOS_JSON`
con el dominio de Railway y el mismo prefijo
(`generar_secretos.py --apuntar`), la carga en Vercel y redespliega
producción. Cambia producción: corrélo vos. Requiere `npx vercel login` y
`npx vercel link` hechos. A mano, es lo mismo que:

```sh
PY=world/ap2/.venv/Scripts/python
$PY deploy/vercel/generar_secretos.py --apuntar <dominio>.up.railway.app
npx vercel env add GATEWAY_COMERCIOS_JSON production --force < .temp-db/vercel/GATEWAY_COMERCIOS_JSON.json
npx vercel deploy --prod
```

No uses `generar_secretos.py --dominio` para esto: genera un prefijo nuevo
(y con `--forzar` pisa también la clave del recibo y `vercel.env`).
Después, el smoke de [`deploy/vercel/README.md`](../vercel/README.md#7-smoke)
contra el motor (`smoke_v1_general.py --gateway https://kaiai-motor.vercel.app`)
ya pasa por Railway.

El dominio de Railway no cambia: esto se hace **una vez**. Si después corrés
`tunel_demo.sh`, el motor queda apuntando al túnel; para volver, de nuevo
`apuntar_vercel.sh`.

## Logs

```sh
$R logs                          # el arranque y el enrutador (el último deploy)
$R logs --build                  # el build
$R logs --http                   # los pedidos HTTP que llegaron
$R ssh -- tail -n 50 /app/.logs/proxy.log      # adentro: proxy, dashboard, world
$R ssh -- tail -n 50 /app/world/ap2/.logs/merchant_agent.log
```

Lo que va a `railway logs` es lo de `arrancar.sh`, `start_all.sh` y el
enrutador: ningún secreto (ni la clave de Gemini, ni la contraseña, ni el
prefijo: el enrutador lo muestra como `/<prefijo>`). El detalle de cada
proceso queda en `.logs/` adentro del contenedor, como en local; si un
proceso no arranca, `start_all.sh` muestra las últimas líneas.

## Costo

Railway cobra lo que se usa, por segundo: a la tarifa publicada (verificala en
railway.com/pricing), unos **US$10 por GB de RAM al mes** y **US$20 por vCPU
al mes**. La demo son 8 procesos de Python (los 4 agentes y el catálogo de
`world/`, ADK, el proxy, el panel y el enrutador) más los `uv run` que los
lanzan:

- **RAM:** alrededor de **1 GB** quieta (medido con `arrancar.sh` y todo
  levantado: 0,99 GB entre los 4 agentes, ~150 MB cada uno; ADK, 140 MB; el
  proxy, 110 MB; el panel, el catálogo y el enrutador, ~50 MB cada uno; y los
  `uv run`, ~17 MB cada uno). Algo más durante una compra.
- **CPU:** casi nada quieta (los procesos esperan pedidos); unos segundos de
  CPU por compra.

Prendida todo el mes, **unos US$10 a 15 en total**: el plan Hobby (US$5, con
US$5 de uso incluidos) más unos US$5-10 de uso (1 GB ≈ US$10 al mes, y la CPU
quieta, centavos). Las llamadas a Gemini no las cobra
Railway (son de la cuenta de Google). Para no pasarte, poné un tope en
**Workspace → Usage → Usage limits** (`$R usage`). Para apagarla del todo:
`$R down` (saca el deploy) o borrá el servicio.

## Seguridad

- **El comercio solo con el prefijo secreto.** Llama a un LLM en cada
  checkout: sin `/<TUNEL_PREFIJO>/`, el pedido va al panel (401), nunca al
  comercio ni al catálogo.
- **El panel, con contraseña** (HTTP Basic) y solo por https (la termina
  Railway). Publicado, `dashboard/server.py` no arranca sin ella, y
  `arrancar.sh` además comprueba que el panel conteste 401 sin contraseña
  antes de abrir la puerta. Le llega el `Host` público y lo compara con
  `DASHBOARD_PUBLIC_ORIGINS` (DNS rebinding), y el `Origin` de los POST.
- **ADK, el proxy local y los otros agentes no se publican:** escuchan en
  `127.0.0.1` (el panel también, `DASHBOARD_SOLO_LOOPBACK=1`). El único
  puerto del contenedor fuera de loopback es el del enrutador.
- **`/salud` no dice nada:** `{"estado": "ok"}` o `{"estado":
  "no_disponible"}`; qué falta va al log.
- **Los secretos solo en las variables del servicio:** la imagen no tiene
  ninguno (`.dockerignore`, y el Dockerfile no declara ninguno), y el log de
  arranque no los muestra.
- **Rotar:** la contraseña o la clave de Gemini, cambiando la variable
  (Railway redespliega). El prefijo: uno nuevo en
  `.temp-db/vercel/TUNEL_PREFIJO.txt`
  (`python -c "import secrets; print(secrets.token_urlsafe(18))"`), la
  variable `TUNEL_PREFIJO` en Railway y, cuando termine el deploy,
  `apuntar_vercel.sh`.

## A tener en cuenta

- **Una sola réplica.** El estado vive en memoria (sesiones de ADK, compras,
  aprobaciones, la clave del recibo de la demo, que se genera en cada
  arranque): si se reinicia, la demo arranca de cero, como en local. No
  subas `numReplicas`.
- **`/demo` es una sola conversación compartida**, como en Tenki: alcanza
  para una demo con una persona al mando.
- **La cuota de Gemini:** cada compra hace varias llamadas (4 agentes). Con
  el plan gratuito, dejá `DEMO_PAUSA_AUTO_S=8`.
- **Tiempos del borde de Railway:** un pedido puede durar hasta 15 minutos;
  hay reportes (no documentados) de que corta a los ~60 s un pedido que no
  mandó ningún byte. El SSE del panel manda un `: ping` cada 15 s (no le
  pasa); un `create_checkout` del comercio tarda bastante menos. Si el
  motor ve cortes en checkouts largos, es eso.
- **Región:** `us-east4-eqdc4a` (Virginia). Si Railway la rechaza (las
  regiones disponibles cambian por plan), sacá `multiRegionConfig` de
  `railway.json` y elegila en **Settings → Deploy → Regions**, con 1 réplica.
- **El gráfico del benchmark** del panel queda vacío: `results/` no entra a
  la imagen.
- **El gateway (:8030) no se abre acá:** las compras de integradores son del
  motor de Vercel. `arrancar.sh` saca `GATEWAY_CLAVES_ARCHIVO`.

## Probar la imagen en tu máquina

Con Docker Desktop prendido, desde la raíz, con un puerto de afuera que no
choque con una demo local. `arrancar.sh` exige `https` en
`DASHBOARD_PUBLIC_ORIGINS`: para probar, un origen https cualquiera y el
`Host` a mano en cada `curl` (como lo manda el borde de Railway).

```sh
docker build -f deploy/railway/Dockerfile -t kaiai-demo .
docker run --rm --name kaiai-demo -p 8099:8080 -e PORT=8080 -e GOOGLE_API_KEY=dummy \
  -e DASHBOARD_ACCESS_TOKEN=clave-de-prueba-0001 -e TUNEL_PREFIJO=prefijo-de-prueba \
  -e DASHBOARD_PUBLIC_ORIGINS=https://demo.example kaiai-demo
# en otra terminal, cuando el log dice "KaiAI en Railway":
H='Host: demo.example'
curl -s -H "$H" http://localhost:8099/salud                                   # {"estado": "ok"}
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" http://localhost:8099/demo   # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" -u x:clave-de-prueba-0001 http://localhost:8099/demo   # 200
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" http://localhost:8099/prefijo-de-prueba/products   # 200
docker stop kaiai-demo
```

Con `GOOGLE_API_KEY=dummy` arranca todo (el LLM no se usa hasta que hay una
compra). Sin Docker, el mismo `arrancar.sh` corre en tu máquina (Git Bash),
con el venv de `world/ap2` armado, `ENRUTADOR_HOST=127.0.0.1` y un `PORT`
libre: así se probó (las mismas pruebas de arriba, más el SSE del panel, el
`Host` y el `Origin` de `/api/*`, la caída de un proceso y el `SIGTERM`).
