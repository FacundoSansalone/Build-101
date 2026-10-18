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
| `code/samples/python/src/roles/shopping_agent/tools.py` | Si `create_checkout` no recibe checkout y la tarea trae el artefacto `guard.veredicto`, el error muestra el motivo del Guard (bloqueado o retenido para revisión humana) en lugar del genérico. |
| `code/samples/python/src/roles/shopping_agent/subagents/shopper/tools.py` | Etapa 2: si la respuesta a `find_products` trae el artefacto `guard.parametros_autorizados` (el usuario aceptó una alternativa en el dashboard del Guard), la herramienta devuelve los carts junto con esos parámetros. Sin el artefacto, devuelve lo mismo que el sample. |

Código nuevo (no es de AP2): `code/samples/python/src/demo_world/`.

El protocolo, el SDK y el resto del flujo (checkout firmado por el comercio,
mandatos, pago, OTP) no se modificaron.
