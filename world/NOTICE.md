# Cambios respecto del repo oficial de AP2

`world/ap2/` es una copia de
[google-agentic-commerce/AP2](https://github.com/google-agentic-commerce/AP2)
(commit en `world/ap2/UPSTREAM_COMMIT`), bajo licencia Apache 2.0
(`world/ap2/LICENSE`). Se quitaron las carpetas que no usamos (docs, web-client,
samples de Go y Android, CI) y se hicieron estos cambios, todos marcados con
`HACKATHON (Pre-Sign Guard)` en el código:

| Archivo | Cambio |
|---|---|
| `code/samples/python/src/roles/merchant_agent/sub_agents/catalog_agent.py` | Los productos salen de un catálogo fijo por escenario (`demo_world`) en lugar de generarse con un LLM. Agrega el data part `demo.offer_details`. |
| `code/samples/python/src/roles/merchant_agent/agent_executor.py` | La búsqueda de productos se despacha sin el LLM que elige la herramienta. |
| `code/samples/python/src/roles/shopping_agent/remote_agents.py` | La URL del comercio se lee de `MERCHANT_AGENT_URL`, para poder poner el proxy del Guard en el medio. |
| `code/samples/python/src/roles/shopping_agent/tools.py` | Si `create_checkout` no recibe checkout y la tarea trae el artefacto `guard.veredicto`, el error muestra el motivo del Guard (bloqueado o retenido para revisión humana) en lugar del genérico. Si está bloqueado, lo devuelve en vez de tirar error (con el error, `RetryingLlmAgent` reintentaba y el agente inventaba un resumen para confirmar). |
| `code/samples/python/src/roles/shopping_agent/subagents/shopper/tools.py` | Etapa 2: si la respuesta a `find_products` trae el artefacto `guard.parametros_autorizados` (el usuario aceptó una alternativa en el dashboard del Guard), la herramienta devuelve los carts junto con esos parámetros. Sin el artefacto, devuelve lo mismo que el sample. Si el proxy cortó la conversación (`guard.veredicto` `bloqueado`), devuelve el motivo en vez de tirar error: con el error, `RetryingLlmAgent` reintentaba la búsqueda 5 veces y nuestro agente volvía a preguntar en cada una. |
| `code/samples/python/src/common/system_utils.py`, `common/function_call_resolver.py`, `roles/shopping_agent/agent.py` y sus 3 sub-agentes | El modelo se lee de `AP2_MODEL` (por defecto, `gemini-3.1-flash-lite-preview`, el del sample). En el deploy se usa uno estable: el preview satura (503) y todos los agentes comparten su cupo por minuto. |
| `code/samples/python/src/roles/shopping_agent/tools.py` (`initiate_payment_with_otp`) | Un código OTP ya rechazado (o más de 3 intentos) no se vuelve a mandar al comercio: se le pide al agente que pregunte al usuario. El modelo inventaba un código y lo reintentaba sin fin dentro del mismo turno. |

Código nuevo (no es de AP2): `code/samples/python/src/demo_world/`.

El protocolo, el SDK y el resto del flujo (checkout firmado por el comercio,
mandatos, pago, OTP) no se modificaron.
