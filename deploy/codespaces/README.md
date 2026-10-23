# La demo a pedido en GitHub Codespaces

El motor de KaiAI está en Vercel (https://kaiai-motor.vercel.app), siempre
prendido. El comercio simulado (`world/`) y la demo `/demo` solo hacen falta
para **probarlo o mostrarlo**: en la vida real los comercios son externos.
Acá van a un **Codespace**, una máquina de GitHub con este repo adentro que
prendés cuando querés probar y que se apaga sola. No necesita tu máquina y,
con el cupo gratis de GitHub, no cuesta nada.

Adentro corre lo mismo que el contenedor de Railway
([`../railway/arrancar.sh`](../railway/arrancar.sh)): `start_all.sh general`
con todo en `127.0.0.1`, y como única puerta el enrutador de
[`../vercel/tunel_router.py`](../vercel/tunel_router.py) en el puerto 8080,
que GitHub publica por `https`:

```
navegador ──▶ https://<codespace>-8080.app.github.dev/demo                  ──▶ :8020 panel (pide contraseña)
motor ──────▶ https://<codespace>-8080.app.github.dev/<TUNEL_PREFIJO>/a2a/* ──▶ :8001 Merchant Agent
              https://<codespace>-8080.app.github.dev/<TUNEL_PREFIJO>/…     ──▶ :8011 catálogo
              https://<codespace>-8080.app.github.dev/salud                 ──▶ 200 si todo contesta

adentro, solo en 127.0.0.1: :8000 ADK · :8002 credenciales · :8003 pagos · :8010 proxy · :8020 panel
```

| Archivo | Qué es |
|---|---|
| [`../../.devcontainer/devcontainer.json`](../../.devcontainer/devcontainer.json) | La definición del Codespace: Python 3.12 (Debian trixie, como Railway), 2 núcleos y 8 GB, solo el puerto 8080, los tres secretos que pide y los scripts de abajo |
| [`instalar.sh`](instalar.sh) | Al **crear** el Codespace (una vez): uv con la misma versión que el Dockerfile de Railway y el venv de `world/ap2` |
| [`prender.sh`](prender.sh) | Cada vez que **prende**: valida los secretos, levanta la demo en segundo plano (log en `.logs/codespaces.log`) y deja público el 8080. A mano, si ya anda, muestra las URLs |
| [`../../.devcontainer/kaiai-arranque/`](../../.devcontainer/kaiai-arranque/) | Una feature local con un vigía que arranca con el contenedor (como el `sshd` de su feature). El `postStartCommand` (`prender.sh --avisar`) valida y le avisa, y el vigía corre `prender.sh`. Hace falta porque lo que deja corriendo el `postStartCommand`, GitHub lo mata al terminar esa etapa (lo medimos: aun con `setsid` y `nohup`) |
| [`cargar_secretos.sh`](cargar_secretos.sh) | Desde tu máquina, una vez: carga los tres secretos en GitHub |
| [`apuntar_vercel.sh`](apuntar_vercel.sh) | Desde tu máquina, una vez por Codespace: apunta el motor de Vercel al comercio del Codespace. Con `--solo-publicar`, solo deja público el 8080 y comprueba la demo |

Los comandos de tu máquina van desde la raíz del repo, en Git Bash en
Windows, con la [CLI de GitHub](https://cli.github.com) (`gh`).

## La primera vez

### 1. `gh` con permiso de Codespaces

```sh
gh auth login -s codespace       # o, si ya tenés sesión: gh auth refresh -s codespace
```

### 2. Los secretos

```sh
bash deploy/codespaces/cargar_secretos.sh
```

Carga tres **secretos de Codespaces de tu usuario**, habilitados solo para
este repo (no van al repo y no los ve nadie más):

| Secreto | De dónde sale |
|---|---|
| `GOOGLE_API_KEY` | La clave de Gemini del `.env` |
| `DASHBOARD_ACCESS_TOKEN` | La contraseña de `/demo`. La genera una vez en `.temp-db/codespaces_dashboard_token` (ignorado por git) y la reusa |
| `TUNEL_PREFIJO` | El de `.temp-db/vercel/TUNEL_PREFIJO.txt`: el mismo que usa el motor de Vercel |

Los valores le llegan a `gh` por la entrada estándar: nunca en la línea de
comandos ni en la pantalla. También se pueden cargar a mano en
github.com → Settings → Codespaces → Secrets (y la página de creación del
Codespace los pide si faltan).

### 3. Crear el Codespace

En github.com, en el repo: **Code → Codespaces → Create codespace on main**.
O desde tu máquina:

```sh
gh codespace create -R FacundoSansalone/Build-101 -b main -m basicLinux32gb --idle-timeout 60m
```

La primera vez tarda unos minutos (instala el venv). Después prende la demo
sola; en uno o dos minutos más está lista. Se puede seguir con
`tail -f .logs/codespaces.log` en la terminal del Codespace.

### 4. El tiempo sin uso

En github.com/settings/codespaces → **Default idle timeout**, poné **60
minutos** o más (máximo 240). Ojo: **el tráfico a la demo no cuenta como
uso**, solo escribir en el editor o en la terminal. Con 30 minutos (el valor
por defecto), el Codespace se puede apagar en medio de una prueba si no
tocás el editor. Además, se apaga siempre a las 12 horas de prendido.

### 5. Apuntar el motor de Vercel

Con el Codespace prendido y la demo lista:

```sh
bash deploy/codespaces/apuntar_vercel.sh            # usa tu único Codespace de este repo
bash deploy/codespaces/apuntar_vercel.sh <nombre>   # si tenés más de uno (gh codespace list)
```

Deja público el 8080, comprueba que la demo responde (salud y catálogo con
el prefijo) y recién ahí carga `GATEWAY_COMERCIOS_JSON` en Vercel y
redespliega producción. Dice "Listo" solo si todo salió bien. Requiere
`npx vercel login` y `npx vercel link` hechos
([`../vercel/README.md`](../vercel/README.md)).

Apuntar Vercel es **una vez por Codespace**: el dominio no cambia al apagarlo
y prenderlo. Solo cambia si se crea otro (por ejemplo, porque GitHub borró
el anterior después de 30 días sin usarlo); ahí se vuelve a correr.

El puerto público es otra cosa. `prender.sh` intenta dejarlo público en cada
prendida, pero desde adentro del Codespace `gh` puede no tener permiso, y
GitHub puede volver a dejarlo privado al reiniciar. Si la URL pide iniciar
sesión en GitHub, desde tu máquina:

```sh
bash deploy/codespaces/apuntar_vercel.sh --solo-publicar   # el 8080 público y la comprobación, sin tocar Vercel
```

## El uso de todos los días

- **Prender:** github.com/codespaces → tu Codespace → **Open in browser** (o
  `gh codespace code`). La demo prende sola.
- **Probar:** `https://<codespace>-8080.app.github.dev/demo`, usuario
  cualquiera y la contraseña de `.temp-db/codespaces_dashboard_token`. Cada
  compra por el motor de Vercel queda en
  https://kaiai-motor.vercel.app/panel.
- **Ver las URLs:** en la terminal del Codespace,
  `bash deploy/codespaces/prender.sh`.
- **Apagar:** github.com/codespaces → **Stop codespace**, o
  `gh codespace stop`. Si no, se apaga solo cuando pasa el tiempo sin uso.

Con el Codespace apagado, el motor de Vercel no llega al comercio y las
compras de prueba se bloquean (falla cerrado), como siempre.

## Costo

GitHub da gratis por mes, en una cuenta personal, **120 horas-núcleo** (unas
**60 horas** prendido con 2 núcleos) y **15 GB** de disco. Apagado no gasta
horas, solo disco. Con el límite de gasto en US$0 (el valor por defecto),
GitHub nunca cobra: si se termina el cupo, el Codespace no prende hasta el
mes siguiente.

## Logs

- En el Codespace: `tail -f .logs/codespaces.log` (el arranque),
  `.logs/*.log` (cada proceso, como en `start_all.sh`) y
  `/tmp/kaiai-vigia.log` (el vigía: cuándo recibió el aviso).
- Desde tu máquina: `gh codespace ssh -c <nombre> -- tail -n 40 .logs/codespaces.log`.

La demo escribe en archivos y no en la terminal: lo que se escribe en una
terminal cuenta como uso, y el Codespace nunca se apagaría solo.

## Seguridad

- Público hay un solo puerto, el 8080, y ahí escucha solo el enrutador:
  - `/demo` y el resto del panel piden la contraseña (HTTP Basic);
  - el comercio y el catálogo, solo con el prefijo secreto;
  - `/salud` da 200 o 503, sin decir qué falta.
- ADK, el proxy, los otros agentes y el panel escuchan en `127.0.0.1`, y
  GitHub no los reenvía (`otherPortsAttributes: ignore`).
- Los secretos son tuyos, solo para este repo. No están en el repo ni en el
  log.

## Si algo falla

- **"KaiAI no arranca: revisá estos secretos de Codespaces"** (en el
  registro de creación o en `.logs/codespaces.log`): cargalos (paso 2) y
  reiniciá el Codespace (Stop y Start). GitHub los pasa al prender.
- **La URL pide iniciar sesión en GitHub:** el 8080 quedó privado. Desde tu
  máquina, `bash deploy/codespaces/apuntar_vercel.sh --solo-publicar`, o en
  el Codespace: pestaña **Puertos** → clic derecho en el 8080 → **Port
  Visibility → Public**. Mientras esté privado, las compras por el motor se
  bloquean (falla cerrado).
- **"No encontré el vigía que prende la demo"**: el contenedor es de antes de
  la feature `kaiai-arranque`. **Rebuild Container** (paleta de comandos) o
  `gh codespace rebuild -c <nombre>`. Mientras tanto,
  `bash deploy/codespaces/prender.sh` en la terminal la prende igual.
- **La demo se cayó** (un proceso terminó y `arrancar.sh` apagó todo): en la
  terminal del Codespace, `bash deploy/codespaces/prender.sh`.
- **El panel contesta 403 `host_no_permitido`:** el reenvío de GitHub cambia
  el `Host` por `localhost:8080` y el `Origin` propio por
  `https://localhost:8080`. `prender.sh` ya suma ese origen; si GitHub
  cambiara la forma, se ajusta ahí.
- **`/demo` es una sola conversación compartida**, como en Railway y Tenki.
