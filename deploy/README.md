# Deploy de la demo

La demo entera (los 5 procesos de `world/`, el proxy del Guard y el dashboard)
corre en **una sola máquina** y habla por `127.0.0.1` igual que en local. Lo
único que se publica es el dashboard (:8020). La compra se hace en
**`/demo`**.

| Script | Qué hace |
|---|---|
| `start_all.sh [escenario]` | Levanta todo en esta máquina con el agente pasando por el proxy, espera a que responda y, si un proceso muere, apaga todo. Logs en `.logs/`. |
| `tenki.sh [escenario]` | Crea una VM en Tenki Sandbox, sube el código y el `.env`, instala dependencias, publica el :8020 y corre `start_all.sh` adentro. |

## Local

```sh
bash deploy/start_all.sh                 # → http://localhost:8020/demo
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

El `.env` de la raíz viaja a la VM tal cual (necesita `GOOGLE_API_KEY`; las
`SUPABASE_*` son opcionales) y puede pisar cualquiera de estas variables.

## A tener en cuenta

- **La URL es pública y no tiene contraseña.** Cualquiera que la tenga gasta
  la cuota de Gemini (4 agentes llaman al LLM en cada compra). Compartila solo
  con quien la necesite.
- **`/demo` es una sola conversación compartida.** Todos los que abren la URL
  ven el mismo chat y el mismo comercio. Alcanza para una demo con una
  persona al mando, no para varios usuarios a la vez.
- El estado vive en memoria (sesiones de ADK, compras, aprobaciones): si se
  reinicia, la demo arranca de cero, como en local.
