# Onboarding: Dev 3 (`proxy/` y `dashboard/`)

> Si sos un Claude Code leyendo esto: tu dev es el dueño de `proxy/` y
> `dashboard/`. Leé también [`CLAUDE.md`](../../CLAUDE.md),
> [ARQUITECTURA.md](../ARQUITECTURA.md) y [DEMO.md](../DEMO.md).

## Tu misión en una frase

Meter el Guard en el medio del flujo AP2 real sin tocar ningún objeto firmado,
y hacer que el jurado vea, en vivo y lado a lado, que AP2 aprobó todo y aun así
el Guard frenó la compra.

## Qué ya existe

| Qué | Dónde |
|---|---|
| El Shopping Agent ya acepta un proxy | `MERCHANT_AGENT_URL` en [`remote_agents.py`](../../world/ap2/code/samples/python/src/roles/shopping_agent/remote_agents.py) |
| El comercio real (A2A) | `:8001`, ruta `/a2a/merchant_agent` ([`merchant_agent/__main__.py`](../../world/ap2/code/samples/python/src/roles/merchant_agent/__main__.py)) |
| Agent Card del comercio | [`merchant_agent/agent.json`](../../world/ap2/code/samples/python/src/roles/merchant_agent/agent.json) |
| Cómo responde el comercio a `find_products` | [`catalog_agent.py`](../../world/ap2/code/samples/python/src/roles/merchant_agent/sub_agents/catalog_agent.py) |
| Cómo responde a `create_checkout` (JWT firmado) | `create_checkout()` en [`merchant_agent/tools.py`](../../world/ap2/code/samples/python/src/roles/merchant_agent/tools.py) |
| Servidor A2A del sample (útil para armar el proxy) | [`common/server.py`](../../world/ap2/code/samples/python/src/common/server.py) |
| Cliente A2A del sample | [`common/payment_remote_a2a_client.py`](../../world/ap2/code/samples/python/src/common/payment_remote_a2a_client.py) |
| Datos para trabajar sin levantar nada | [`fixtures/`](../../fixtures/) |
| Resultados del benchmark (para el gráfico) | `world/ap2/results/benchmark-*.json` (se generan al correrlo) |
| Formato de los resultados | `run()` en [`demo_world/benchmark.py`](../../world/ap2/code/samples/python/src/demo_world/benchmark.py): `meta` + `scenarios.<nombre>.{trials, summary}` |
| Guion y momento clave | [DEMO.md](../DEMO.md) |

`proxy/` y `dashboard/` hoy solo tienen un README.

## Tus tareas

1. **Paso 0 con el equipo**: contratos acordados, sobre todo `Evento`,
   `Veredicto` y `Recibo`.
2. **Proxy transparente en `:8010`.** Reenvía todo a `:8001` sin cambios.
   **Primero verificá lo de la Agent Card** (ver
   [ARQUITECTURA.md](../ARQUITECTURA.md#cómo-se-mete-el-proxy-sin-tocar-ap2)):
   si la card que sirve el proxy dice `url: ...:8001`, el cliente probablemente
   se saltee el proxy.
   *Terminado:* la compra completa en la UI funciona igual con
   `MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent`, y los logs
   del proxy muestran `find_products` y `create_checkout`.
3. **Proxy con Guard (stub).** Al recibir las ofertas, guardarlas por
   `context_id`; al ver `create_checkout(cart_id)`, llamar a `evaluar()`
   (stub hasta que Dev 2 entregue) y, según el veredicto, reenviar el checkout
   **intacto** o cortar la tarea con el motivo.
   *Terminado:* con el stub devolviendo `bloqueado`, la UI no llega a la firma.
4. **Interruptor Guard sí/no** (variable de entorno, endpoint o flag del
   dashboard).
   *Terminado:* se puede mostrar B con y sin Guard sin reiniciar nada.
   **Punto de control 3** cuando se enchufa el Guard real de Dev 2.
5. **Eventos al dashboard** (`Evento`).
6. **Dashboard**: flujo en vivo, veredicto con sus chequeos, recibo, **los
   mandatos reales** (fragmentos de JSON con firma: `ap2.cart`,
   `checkout_jwt`, mandatos) con la verificación de AP2 en OK, y el gráfico
   del benchmark ("de X% a Y%").
   *Terminado:* el momento clave de [DEMO.md](../DEMO.md#el-momento-clave-escenario-b-lado-a-lado)
   se puede mostrar entero.
7. **Presentación** (pitch de 4 minutos, ver [DEMO.md](../DEMO.md#5-pitch-en-4-minutos)).

## De qué dependés y quién depende de vos

- **Dependés de:** los contratos; `evaluar()` de Dev 2 (mientras tanto,
  stub); el flujo levantado de Dev 1 (mientras tanto, fixtures); los
  resultados del benchmark para el gráfico.
- **Dependen de vos:** la demo entera. Dev 1 te puede ayudar con la
  integración del flujo AP2 una vez que cierre lo suyo.

## Cómo probar tu parte sin depender de los otros

- **Guard stub** con la firma acordada:

  ```python
  def evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None, checkout_hash=None):
      # el último cart es el del atacante en A, B y B2
      ultimo = ofertas_vistas.carts[-1]["cart_id"]
      return Veredicto(status="bloqueado" if cart_id_elegido == ultimo else "aprobado", ...)
  ```

- **Sin el flujo levantado:** el dashboard se alimenta de `fixtures/` y de un
  JSON de resultados del benchmark generado sin LLM:

  ```sh
  cd world/ap2 && uv run --no-sync --package ap2-samples python -m demo_world.benchmark \
    --source direct --decider target --trials 2
  ```

- **Merchant falso:** un servidor mínimo en otro puerto que responda los
  `ap2.cart` y `demo.offer_details` de un fixture. Sirve para probar el proxy
  sin levantar AP2. Lo más fiel igual es el comercio real, que se levanta con
  `bash world/run_world.sh` (pide `GOOGLE_API_KEY`). La búsqueda de productos
  del comercio no usa LLM, pero `create_checkout` pasa por el LLM del sample
  para elegir la herramienta.
- **Benchmark a través del proxy**, sin UI: `--merchant-url
  http://localhost:8010/a2a/merchant_agent`. El benchmark manda el escenario
  en el data part `demo_scenario`: el proxy lo tiene que reenviar tal cual.

## Primer prompt sugerido para Claude Code

```text
Soy Dev 3 del repo Pre-Sign Guard y soy dueño de proxy/ y dashboard/. Leé
CLAUDE.md, docs/ARQUITECTURA.md, docs/DEMO.md y
docs/onboarding/dev3-proxy-dashboard.md.

Quiero empezar por un proxy A2A transparente en el puerto 8010 que reenvíe
todo al Merchant Agent en http://localhost:8001/a2a/merchant_agent sin
modificar nada. Antes de escribir código:
1. Leé world/ap2/code/samples/python/src/common/payment_remote_a2a_client.py
   y common/server.py y explicame cómo el cliente resuelve la Agent Card y a
   qué URL manda las llamadas. Quiero saber si el proxy tiene que servir su
   propia Agent Card con url :8010.
2. Proponeme un plan para el proxy transparente y para cómo voy a detectar
   find_products (respuesta con ap2.cart) y create_checkout (respuesta con
   ap2.checkout) sin modificar esos objetos.

Reglas: no toques world/ ni contracts/. El proxy nunca modifica el
checkout_jwt ni los mandatos. Si hace falta cambiar un contrato, pará y
avisame.
```

## Errores a evitar

- **Modificar el `checkout_jwt` o los mandatos** (aunque sea para "limpiar"
  la inyección del título): rompe la firma y AP2 rechaza el pago.
- **Que el proxy se saltee en silencio** porque la Agent Card apunta a
  `:8001`. Siempre confirmar en los logs que el tráfico pasa por `:8010`.
- **Perder el data part `demo_scenario`** al reenviar: el benchmark lo usa
  para elegir el escenario.
- **Meter lógica del Guard en el proxy.** El proxy transporta, registra y
  aplica el veredicto; decide `guard/`.
- **Un dashboard que no muestra los mandatos reales.** Sin la firma y el OK
  de AP2 en pantalla, el momento clave pierde la mitad del impacto.
- **Depender del flujo en vivo para la demo.** Tener siempre a mano los
  resultados y fixtures guardados.
