"""OpenAPI 3.1 de la API `/v1` y su página de docs (diseño del gateway, §1.5).

El spec se arma a mano, en Python, a partir de una tabla de operaciones: sin
dependencias nuevas y sin generarlo desde el código (las rutas validan a
mano, no con modelos). `test_api_v1` controla que el spec liste exactamente
las rutas de `api_v1.rutas_v1`.

La página `/v1/docs` es HTML autocontenido generado desde el spec: sin
scripts ni recursos externos, y todo texto pasa por `html.escape` (el spec
incluye `GATEWAY_PUBLIC_URL`, que viene de la configuración).

Sin I/O.
"""

from __future__ import annotations

import html
import json
from typing import Any

# -- esquemas ----------------------------------------------------------------


def _ref(nombre: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{nombre}"}


def _nullable(esquema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [esquema, {"type": "null"}]}


_ID_SEGURO = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$"}
_ID_COMPRA = {"type": "string", "pattern": "^cmp-[0-9a-f]{16}$"}
_SLUG = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,39}$"}
_FECHA = {"type": "string", "format": "date-time"}
_MONEDA = {"type": "string", "pattern": "^[A-Z]{3}$"}
_ESTADOS = [
    "creada", "esperando_contexto", "negociando", "esperando_decision",
    "sin_oportunidad", "aceptada", "rechazada", "vencida", "bloqueada",
]
_CHEQUEO = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": ["cumplimiento", "evidencia", "integridad", "politica", "mandato"]},
        "resultado": {"type": "string", "enum": ["ok", "aviso", "escalar", "bloquea"]},
    },
}

ESQUEMAS: dict[str, dict[str, Any]] = {
    "Error": {
        "type": "object",
        "required": ["error"],
        "description": "Todo error de `/v1` tiene esta forma, también una ruta que no "
        "existe (404 `no_encontrado`) o un método que la ruta no acepta (405 "
        "`metodo_no_permitido`, con los que acepta en `Allow`).",
        "properties": {
            "error": {
                "type": "object",
                "required": ["codigo", "mensaje"],
                "properties": {
                    "codigo": {"type": "string"},
                    "mensaje": {"type": "string"},
                    "detalle": {
                        "type": "object",
                        "description": "En `validacion` y `requisito_no_soportado`: "
                        "`campos`, lista de `{campo, problema}`. En `estado`: `estado`.",
                    },
                },
            }
        },
    },
    "Entrega": {
        "type": "object",
        "required": ["cantidad", "fecha"],
        "properties": {
            "cantidad": {"type": "integer", "minimum": 1},
            "fecha": {"type": "string", "format": "date"},
        },
    },
    "Requisito": {
        "type": "object",
        "required": ["campo", "operador", "valor", "obligatorio"],
        "additionalProperties": False,
        "description": "Un parámetro autorizado. `campo` y `operador` tienen que ser "
        "de los que el Guard sabe chequear (si no, 400 `requisito_no_soportado`).",
        "properties": {
            "campo": {"type": "string", "examples": ["precio", "moneda", "cantidad", "entregas", "atributo"]},
            "operador": {"type": "string", "enum": ["igual", "maximo", "minimo", "incluye", "cubre"]},
            "valor": {"description": "Número, texto, bool o lista de `Entrega` (con `cubre`)."},
            "obligatorio": {"type": "boolean"},
            "atributo": _nullable({"type": "string"}),
            "unidad": _nullable({"type": "string"}),
            "origen": _nullable({"type": "string", "enum": ["dicho", "inferido", "aceptado", "politica"]}),
        },
    },
    "FichaEntrada": {
        "type": "object",
        "required": ["requisitos"],
        "additionalProperties": False,
        "description": "Tiene que incluir `precio`/`maximo`/obligatorio (> 0) y "
        "`moneda`/`igual`/obligatorio (ISO 4217); si no, 400 `ficha_sin_tope`. "
        "En `POST /v1/compras`, hasta 32 KB en JSON compacto.",
        "properties": {
            "requisitos": {"type": "array", "minItems": 1, "maxItems": 50, "items": _ref("Requisito")},
            "categoria_taxonomia": _nullable({"type": "string", "pattern": "^[0-9]{1,12}$"}),
        },
    },
    "Ficha": {
        "type": "object",
        "description": "La ficha autorizada vigente (cambia solo si el usuario acepta "
        "una oportunidad). Los valores aceptados de una oportunidad se filtran como en "
        "`VistaOportunidad`.",
        "properties": {
            "pedido_original": {"type": "string"},
            "requisitos": {"type": "array", "items": _ref("Requisito")},
            "criterio": {"type": "string"},
            "confirmada": {"type": "boolean"},
            "categoria_taxonomia": _nullable({"type": "string"}),
        },
    },
    "SolicitudCompra": {
        "type": "object",
        "required": ["pedido", "ficha"],
        "additionalProperties": False,
        "properties": {
            "pedido": {"type": "string", "minLength": 1, "maxLength": 2000},
            "comercio": {**_SLUG, "description": "Por defecto, el comercio por defecto del gateway."},
            "ficha": _ref("FichaEntrada"),
            "intencion": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "porque": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "preguntar": {"type": "boolean", "default": False},
                },
            },
            "referencia": {"type": "string", "minLength": 1, "maxLength": 200},
        },
    },
    "VistaVeredicto": {
        "type": "object",
        "description": "Versión reducida: sin texto del comercio. Firmá solo si "
        "`llega_a_firma` es true.",
        "properties": {
            "status": {"type": "string", "enum": ["aprobado", "con_avisos", "escalar", "bloqueado"]},
            "motivo": {"type": "string"},
            "cart_id": _nullable(_ID_SEGURO),
            "llega_a_firma": {"type": "boolean"},
            "chequeos": {"type": "array", "items": _CHEQUEO},
            "recibo": _nullable({
                "type": "object",
                "description": "JWS ES256 opaco. Su payload puede traer valores declarados "
                "por el comercio: guardalo y verificalo, no se lo pases a un LLM.",
                "properties": {
                    "id": {"type": "string"}, "jws": {"type": "string"}, "kid": {"type": "string"},
                },
            }),
            "contexto_id": _nullable(_ID_SEGURO),
            "ts": _FECHA,
            "evaluador": {"type": "string"},
        },
    },
    "VistaOportunidad": {
        "type": "object",
        "properties": {
            "id": _nullable(_ID_SEGURO),
            "cart_id": _nullable(_ID_SEGURO),
            "sku": _nullable(_ID_SEGURO),
            "titulo": {"type": "string", "maxLength": 200, "description": "Título público o vacío."},
            "precio": _nullable({"type": "number"}),
            "moneda": _nullable(_MONEDA),
            "por_que": {"type": "string"},
            "cambios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "campo": {"type": "string"},
                        "pedido": {},
                        "ofrecido": {},
                        "omitido": {
                            "type": "boolean",
                            "description": "Había un valor con texto libre y no se muestra.",
                        },
                    },
                },
            },
            "requisitos_propuestos": {"type": "array", "items": _ref("Requisito")},
        },
    },
    "Pendiente": {
        "type": "object",
        "description": "Lo que la compra espera del integrador (como mucho uno).",
        "required": ["tipo"],
        "properties": {
            "tipo": {"type": "string", "enum": ["porque", "oportunidad", "aprobacion"]},
            "pregunta": {"type": "string"},
            "vence": _nullable(_FECHA),
            "oportunidad": _ref("VistaOportunidad"),
            "contexto_id": _nullable(_ID_SEGURO),
            "cart_id": _nullable(_ID_SEGURO),
            "monto": _nullable({"type": "number"}),
            "centavos": _nullable({"type": "integer"}),
            "moneda": _nullable(_MONEDA),
            "motivo": {"type": "string"},
            "chequeos": {"type": "array", "items": _CHEQUEO},
        },
    },
    "VistaAprobacion": {
        "type": "object",
        "properties": {
            "id": _nullable(_ID_SEGURO),
            "contexto_id": _nullable(_ID_SEGURO),
            "cart_id": _nullable(_ID_SEGURO),
            "centavos": _nullable({"type": "integer"}),
            "monto": _nullable({"type": "number"}),
            "moneda": _nullable(_MONEDA),
            "creada": _nullable(_FECHA),
            "estado": _nullable({"type": "string", "enum": ["pendiente", "usada"]}),
            "usada": _nullable(_FECHA),
            "task_id": _nullable(_ID_SEGURO),
        },
    },
    "VistaCompra": {
        "type": "object",
        "properties": {
            "compra_id": _ID_COMPRA,
            "referencia": _nullable({"type": "string"}),
            "comercio": _nullable(_SLUG),
            "a2a_url": _nullable({"type": "string", "format": "uri"}),
            "estado": {"type": "string", "enum": _ESTADOS},
            "creada": _FECHA,
            "actualizada": _FECHA,
            "pedido": {"type": "string"},
            "contextos": {"type": "array", "items": _ID_SEGURO},
            "ficha": _nullable(_ref("Ficha")),
            "intencion_activa": {"type": "boolean"},
            "pendiente": _nullable(_ref("Pendiente")),
            "veredictos": {"type": "array", "items": _ref("VistaVeredicto")},
            "aprobaciones": {"type": "array", "items": _ref("VistaAprobacion")},
        },
    },
    "ListaCompras": {
        "type": "object",
        "properties": {"compras": {"type": "array", "items": _ref("VistaCompra")}},
    },
    "SolicitudRespuesta": {
        "type": "object",
        "additionalProperties": False,
        "description": 'Uno de los dos: `{"respuesta": "…"}` o `{"omitir": true}`.',
        "properties": {
            "respuesta": {"type": "string", "minLength": 1, "maxLength": 1000},
            "omitir": {"type": "boolean"},
        },
    },
    "SolicitudDecision": {
        "type": "object",
        "required": ["oportunidad_id", "decision"],
        "additionalProperties": False,
        "properties": {
            "oportunidad_id": _ID_SEGURO,
            "decision": {"type": "string", "enum": ["aceptar", "rechazar"]},
        },
    },
    "SolicitudAprobacion": {
        "type": "object",
        "required": ["cart_id"],
        "additionalProperties": False,
        "properties": {"cart_id": _ID_SEGURO, "contexto_id": _ID_SEGURO},
    },
    "SolicitudEvaluar": {
        "type": "object",
        "required": ["ficha", "checkout"],
        "additionalProperties": False,
        "properties": {
            "ficha": {
                "type": "object",
                "required": ["pedido", "requisitos"],
                "additionalProperties": False,
                "properties": {
                    "pedido": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "requisitos": {"type": "array", "minItems": 1, "maxItems": 50, "items": _ref("Requisito")},
                    "categoria_taxonomia": _nullable({"type": "string", "pattern": "^[0-9]{1,12}$"}),
                },
            },
            "checkout": {
                "type": "object",
                "required": ["checkout_jwt", "cart_id"],
                "description": "El data part `ap2.checkout` tal cual.",
                "properties": {
                    "checkout_jwt": {"type": "string"},
                    "checkout_hash": {"type": "string"},
                    "cart_id": _ID_SEGURO,
                    "amount": {"type": "number"},
                    "currency": {"type": "string"},
                },
            },
            "ofertas": {
                "type": "object",
                "required": ["carts"],
                "additionalProperties": False,
                "description": "Opcional. Sin ofertas se evalúa el cart firmado.",
                "properties": {
                    "carts": {"type": "array", "minItems": 1, "maxItems": 200, "items": {"type": "object"}},
                    "offer_details": {"type": "array", "maxItems": 200, "items": {"type": "object"}},
                },
            },
            "comercio": _SLUG,
        },
    },
    "SolicitudVacia": {"type": "object", "additionalProperties": False, "maxProperties": 0},
    "ResultadoEvaluar": {
        "allOf": [
            _ref("VistaVeredicto"),
            {"type": "object", "properties": {"comercio_firmado": _nullable(_ID_SEGURO)}},
        ],
    },
    "SolicitudFiltrar": {
        "type": "object",
        "required": ["contenido"],
        "additionalProperties": False,
        "properties": {
            "contenido": {"description": "Texto o cualquier JSON."},
            "origen": {
                "type": "string", "enum": ["web_search", "pagina", "herramienta", "otro"],
                "default": "otro",
            },
        },
    },
    "ResultadoFiltrar": {
        "type": "object",
        "description": "Nunca devuelve el contenido recibido. Ante cualquier falla del "
        "filtro, `bloqueado`.",
        "properties": {
            "pasa": {"type": "boolean"},
            "status": {"type": "string", "enum": ["pasa", "bloqueado"]},
            "motivo": {"type": "string"},
            "senales": {"type": "array", "items": {"type": "string"}},
        },
    },
    "ListaComercios": {
        "type": "object",
        "properties": {
            "comercios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _SLUG, "nombre": {"type": "string"},
                        "a2a_url": {"type": "string", "format": "uri"},
                    },
                },
            }
        },
    },
    "EntregaWebhook": {
        "type": "object",
        "properties": {
            "id": {"type": "string"}, "integrador_id": {"type": "string"},
            "tipo": {"type": "string"}, "compra_id": _nullable({"type": "string"}),
            "creada": _FECHA, "intentos": {"type": "integer"},
            "estado": {"type": "string", "enum": ["pendiente", "entregada", "fallida", "descartada"]},
            "status_http": _nullable({"type": "integer"}), "error": _nullable({"type": "string"}),
            "destino_host": _nullable({"type": "string"}), "actualizada": _FECHA,
        },
    },
    "ListaEntregas": {
        "type": "object",
        "properties": {"entregas": {"type": "array", "items": _ref("EntregaWebhook")}},
    },
    "ClavePublica": {
        "type": "object",
        "description": "Clave pública (JWK) para verificar los recibos JWS del Guard.",
        "properties": {
            "kid": {"type": "string"}, "jwk": {"type": "object"},
            "keys": {"type": "array", "items": {"type": "object"}},
        },
    },
}

# -- operaciones -------------------------------------------------------------

# (método, ruta, resumen, descripción, cuerpo, {status: (descripción, esquema)},
#  errores, pública)
_Respuestas = dict[str, tuple[str, str | None]]
OPERACIONES: list[tuple[str, str, str, str, str | None, _Respuestas, list[str], bool]] = [
    ("post", "/v1/compras", "Crear una compra",
     "Crea una compra con el pedido y la ficha que autorizó el usuario. La ficha queda "
     "fija: solo la cambia el usuario al aceptar una oportunidad. Usá `a2a_url` y el "
     "header `X-Compra-Id` en el cliente A2A del agente.",
     "SolicitudCompra", {"201": ("Compra creada", "VistaCompra")},
     ["400", "401", "403", "413", "415", "429", "503"], False),
    ("get", "/v1/compras", "Listar compras",
     "Las compras del integrador, la más reciente primero. Parámetros: `limite` (1 a "
     "200, 50 por defecto) y `estado`.",
     None, {"200": ("Compras", "ListaCompras")}, ["400", "401", "503"], False),
    ("get", "/v1/compras/{compra_id}", "Ver una compra",
     "Estado, ficha vigente, lo pendiente, veredictos y aprobaciones. Sirve para "
     "reconciliar si se perdió un webhook.",
     None, {"200": ("Compra", "VistaCompra")}, ["401", "404", "503"], False),
    ("post", "/v1/compras/{compra_id}/respuesta", "Contestar el porqué",
     "Solo con la compra en `esperando_contexto`. El silencio no es autorización: si "
     "vence, se sigue el pedido literal.",
     "SolicitudRespuesta", {"200": ("Compra", "VistaCompra")},
     ["400", "401", "404", "409", "413", "415"], False),
    ("post", "/v1/compras/{compra_id}/decision", "Decidir una oportunidad",
     "Solo con la compra en `esperando_decision` y la oportunidad vigente. La decide "
     "un humano, nunca un LLM.",
     "SolicitudDecision", {"200": ("Compra", "VistaCompra")},
     ["400", "401", "404", "409", "413", "415"], False),
    ("post", "/v1/compras/{compra_id}/aprobaciones", "Aprobar un checkout escalado",
     "Aprueba el checkout retenido por `escalar` (un solo uso; vale para ese "
     "contexto, cart y total firmado). Nunca hace pasar un `bloqueado`. Lo aprueba un "
     "humano; después el agente reintenta `create_checkout`.",
     "SolicitudAprobacion",
     {"201": ("Aprobación creada", "VistaAprobacion"), "200": ("Ya existía", "VistaAprobacion")},
     ["400", "401", "404", "409", "413", "415", "503"], False),
    ("post", "/v1/evaluar", "Evaluar un checkout (sin estado)",
     "El Guard sobre un `ap2.checkout`, para quien no pasa por el gateway. Firmá solo "
     "si la respuesta es 200 y `llega_a_firma` es true.",
     "SolicitudEvaluar", {"200": ("Veredicto", "ResultadoEvaluar")},
     ["400", "401", "403", "413", "415"], False),
    ("post", "/v1/filtrar", "Filtrar contenido externo",
     "El filtro de seguridad sobre lo que el agente lee fuera del gateway (búsquedas, "
     "páginas). Es defensa en profundidad: no garantiza detectar todo.",
     "SolicitudFiltrar", {"200": ("Resultado", "ResultadoFiltrar")},
     ["400", "401", "413", "415"], False),
    ("get", "/v1/comercios", "Comercios permitidos",
     "Los comercios que el integrador puede usar, con su URL A2A.",
     None, {"200": ("Comercios", "ListaComercios")}, ["401"], False),
    ("get", "/v1/webhooks/entregas", "Entregas de webhooks",
     "Las últimas entregas (en memoria: no sobreviven a un reinicio). Parámetro "
     "`limite` (1 a 200).",
     None, {"200": ("Entregas", "ListaEntregas")}, ["400", "401"], False),
    ("post", "/v1/webhooks/prueba", "Probar el webhook",
     "Manda un `ping` firmado en un solo intento. Cuerpo: `{}`.",
     "SolicitudVacia", {"200": ("Entrega", "EntregaWebhook")},
     ["400", "401", "409", "413", "415"], False),
    ("get", "/v1/public-key", "Clave pública de los recibos",
     "Para verificar el JWS de los recibos. Pública.",
     None, {"200": ("Clave", "ClavePublica")}, ["503"], True),
    ("get", "/v1/openapi.json", "Esta especificación", "OpenAPI 3.1. Pública.",
     None, {"200": ("OpenAPI", None)}, [], True),
    ("get", "/v1/docs", "Documentación", "Esta especificación en HTML. Pública.",
     None, {"200": ("HTML", None)}, [], True),
]

ERRORES: dict[str, str] = {
    "400": "json_invalido, validacion, ficha_sin_tope, requisito_no_soportado, checkout_invalido",
    "401": "no_autenticado, clave_invalida",
    "403": "comercio_no_permitido",
    "404": "compra_desconocida, no_encontrado",
    "409": "estado, oportunidad_distinta, sin_escalar, cart_distinto, sin_webhook",
    "413": "cuerpo_demasiado_grande",
    "415": "se_espera_json",
    "429": "demasiadas_compras",
    "503": "estado_no_disponible, sin_clave_recibo",
    "500": "interno",
}


def _operacion(
    resumen: str, descripcion: str, cuerpo: str | None, respuestas: _Respuestas,
    errores: list[str], publica: bool, ruta: str,
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "summary": resumen,
        "description": descripcion,
        "operationId": resumen.lower().replace(" ", "_"),
        "responses": {},
    }
    if publica:
        op["security"] = []
    if "{compra_id}" in ruta:
        op["parameters"] = [{
            "name": "compra_id", "in": "path", "required": True, "schema": _ID_COMPRA,
        }]
    if cuerpo is not None:
        op["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _ref(cuerpo)}},
        }
    for status, (texto, esquema) in respuestas.items():
        contenido: dict[str, Any] = {}
        if esquema is not None:
            contenido = {"content": {"application/json": {"schema": _ref(esquema)}}}
        op["responses"][status] = {"description": texto, **contenido}
    for status in [*errores, "500"]:
        op["responses"][status] = {
            "description": f"Códigos: {ERRORES[status]}",
            "content": {"application/json": {"schema": _ref("Error")}},
        }
    return op


def especificacion(url_publica: str) -> dict[str, Any]:
    """OpenAPI 3.1 con `servers` en la URL pública y `bearerAuth` global
    (las rutas públicas lo anulan con `security: []`)."""
    paths: dict[str, dict[str, Any]] = {}
    for metodo, ruta, resumen, descripcion, cuerpo, respuestas, errores, publica in OPERACIONES:
        paths.setdefault(ruta, {})[metodo] = _operacion(
            resumen, descripcion, cuerpo, respuestas, errores, publica, ruta,
        )
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "KaiAI API",
            "version": "1.0.0",
            "description": (
                "Gateway del lado del comprador: verifica cada compra de tu agente "
                "contra lo que autorizó el usuario antes de la firma. Autenticación: "
                "`Authorization: Bearer psg_…`. Nada del texto del vendedor sale por "
                "esta API."
            ),
        },
        "servers": [{"url": url_publica.rstrip("/")}],
        "security": [{"bearerAuth": []}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http", "scheme": "bearer",
                    "description": "Clave de API `psg_<kid>_<secreto>`.",
                }
            },
            "schemas": ESQUEMAS,
        },
        "paths": paths,
    }


# -- página de docs ----------------------------------------------------------

_ESTILO = """
:root { color-scheme: light dark; --fondo: #fbfbfa; --texto: #1d1d1b; --suave: #5e5e5a;
  --borde: #deddd8; --caja: #f1f0ec; --acento: #2f5d8a; }
@media (prefers-color-scheme: dark) { :root { --fondo: #171716; --texto: #ecebe7;
  --suave: #a5a49f; --borde: #34332f; --caja: #22221f; --acento: #8fb6dd; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--fondo); color: var(--texto);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 920px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 1.6rem; margin: 0 0 8px; }
h2 { font-size: 1.15rem; margin: 40px 0 12px; border-bottom: 1px solid var(--borde); padding-bottom: 6px; }
h3 { font-size: 1rem; margin: 0; display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
p, li { color: var(--suave); }
code, pre { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
pre { background: var(--caja); border: 1px solid var(--borde); border-radius: 6px;
  padding: 12px; overflow-x: auto; }
section.op { border: 1px solid var(--borde); border-radius: 8px; padding: 14px 16px; margin: 12px 0; }
.metodo { font: 600 12px ui-monospace, monospace; color: var(--acento); text-transform: uppercase; }
.ruta { font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
.etiqueta { font-size: 12px; color: var(--suave); }
details summary { cursor: pointer; color: var(--acento); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--borde); vertical-align: top; }
"""


def _e(texto: Any) -> str:
    return html.escape(str(texto), quote=True)


def pagina_docs(spec: dict[str, Any]) -> str:
    """HTML autocontenido desde el spec: cada operación con su cuerpo y sus
    respuestas, los errores y los esquemas. Sin scripts; todo escapado."""
    info = spec.get("info", {})
    servidores = [s.get("url", "") for s in spec.get("servers", [])]
    partes: list[str] = [
        "<!doctype html>",
        '<html lang="es"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(info.get('title', 'API'))}</title>",
        f"<style>{_ESTILO}</style></head><body><main>",
        f"<h1>{_e(info.get('title', 'API'))}</h1>",
        f"<p>{_e(info.get('description', ''))}</p>",
        "<p>Servidor: " + ", ".join(f"<code>{_e(u)}</code>" for u in servidores) + "</p>",
        "<h2>Operaciones</h2>",
    ]
    for ruta, metodos in spec.get("paths", {}).items():
        for metodo, op in metodos.items():
            publica = op.get("security") == []
            partes.append('<section class="op">')
            partes.append(
                f'<h3><span class="metodo">{_e(metodo)}</span>'
                f'<span class="ruta">{_e(ruta)}</span>'
                f'<span class="etiqueta">{"pública" if publica else "requiere clave"}</span></h3>'
            )
            partes.append(f"<p><strong>{_e(op.get('summary', ''))}.</strong> {_e(op.get('description', ''))}</p>")
            cuerpo = op.get("requestBody", {}).get("content", {}).get("application/json", {})
            if cuerpo:
                partes.append(f"<p>Cuerpo: <code>{_e(_nombre_ref(cuerpo.get('schema', {})))}</code></p>")
            filas = []
            for status, resp in op.get("responses", {}).items():
                esquema = resp.get("content", {}).get("application/json", {}).get("schema")
                nombre = _nombre_ref(esquema) if esquema else ""
                filas.append(
                    f"<tr><td><code>{_e(status)}</code></td><td>{_e(resp.get('description', ''))}</td>"
                    f"<td><code>{_e(nombre)}</code></td></tr>"
                )
            partes.append("<table><tr><th>HTTP</th><th>Qué</th><th>Esquema</th></tr>" + "".join(filas) + "</table>")
            partes.append("</section>")
    partes.append("<h2>Esquemas</h2>")
    for nombre, esquema in spec.get("components", {}).get("schemas", {}).items():
        partes.append(
            f"<details><summary><code>{_e(nombre)}</code></summary>"
            f"<pre>{_e(json.dumps(esquema, ensure_ascii=False, indent=2))}</pre></details>"
        )
    partes.append("</main></body></html>")
    return "\n".join(partes)


def _nombre_ref(esquema: dict[str, Any]) -> str:
    ref = esquema.get("$ref", "") if isinstance(esquema, dict) else ""
    return ref.rsplit("/", 1)[-1] if ref else ""
