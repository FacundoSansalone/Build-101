"""API `/v1` del gateway: rutas, validación y errores (diseño del gateway, §1.5).

El integrador crea una compra con el pedido autorizado del usuario, consulta
su estado y contesta lo que el gateway le pregunta (el porqué, una
oportunidad, una aprobación). Además hay dos servicios sin estado:
`/v1/evaluar` (el Guard sobre un checkout, para quien no pasa por el
gateway) y `/v1/filtrar` (el filtro sobre contenido que el agente lee fuera
del gateway).

El historial (panel del cliente): `GET /v1/compras` con filtros (`desde`,
`hasta`, `referencia`, `alerta`) y cursor opaco, y en cada compra su
`resultado`, sus `alertas` e `intento_de_estafa`;
`GET /v1/compras/{id}/explicacion` (`proxy/explicacion.py`) y
`GET /v1/resumen`.

Este módulo no sabe nada del proxy: valida, llama al `NucleoGateway` y arma
las vistas. Las reglas que dependen del estado (dueño de la compra, estado
válido, comercio permitido, límite de compras) las aplica el núcleo con
`ErrorApi`, y acá solo se convierten en respuesta.

Tampoco chequea la clave: eso lo hace el middleware de autenticación, que
deja el `Integrador` en `request.state.integrador`. Si no está, 401 (falla
cerrado aunque el middleware no esté montado).

La validación es estricta a propósito: una clave desconocida, un requisito
que el Guard no sabe chequear o una ficha sin tope de gasto son un 400, nunca
un dato descartado en silencio. Una autorización que el Guard no puede
verificar entera no es una autorización.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Match, Route
from starlette.types import Receive, Scope, Send

from contracts import CheckoutVisto, Ficha, OfertasVistas, Requisito
from proxy import a2a, explicacion, openapi, vistas
from proxy.alertas import ALERTAS
from proxy.tipos_gateway import (
    ID_COMPRA_RE,
    ID_SEGURO_RE,
    ID_SLUG_RE,
    RUTAS_V1_PUBLICAS,
    ErrorApi,
    FiltrosListado,
    FotoCompra,
    Integrador,
    NucleoGateway,
    SolicitudCompra,
    SolicitudEvaluar,
    SolicitudFiltrar,
    es_id,
)

log = logging.getLogger("proxy.api_v1")

# -- límites -----------------------------------------------------------------
LARGO_PEDIDO = 2000
# El mismo tope que la respuesta del porqué en el dashboard
# (`compras.LARGO_MAXIMO_RESPUESTA`); se repite para no atar la API al
# registro de compras.
LARGO_RESPUESTA = 1000
LARGO_REFERENCIA = 200
MAX_REQUISITOS = 50
MAX_CARTS = 200
LIMITE_POR_DEFECTO = 50
LIMITE_MAXIMO = 200
# Niveles de objetos y listas en un cuerpo (el cuerpo es el primero). La API
# usa como mucho 6 (un cronograma `cubre`); pydantic no vuelve a serializar
# ~100 y `copy.deepcopy` revienta en ~300: una compra así validaba pero no se
# podía guardar ni mostrar.
MAX_PROFUNDIDAD_JSON = 32
# La ficha de una compra, en JSON compacto. Vive en memoria y en el almacén
# hasta 24 h y una clave puede tener 1000 compras activas: con el tope del
# cuerpo (1 MB) eran ~1 GB por integrador. 50 requisitos completos son pocos KB.
MAX_BYTES_FICHA = 32_768

CATEGORIA_RE = re.compile(r"[0-9]{1,12}")
MONEDA_RE = re.compile(r"[A-Z]{3}")
# Historial: fechas de los filtros, el cursor y la ventana del resumen.
FECHA_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
FECHA_HORA_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?"
)
CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")
VERSION_CURSOR = 1
DIAS_RESUMEN = 30
MAX_DIAS_RESUMEN = 366

ESTADOS_COMPRA = frozenset({
    "creada", "esperando_contexto", "negociando", "esperando_decision",
    "sin_oportunidad", "aceptada", "rechazada", "vencida", "bloqueada",
})
ORIGENES_CONTENIDO = ("web_search", "pagina", "herramienta", "otro")

_CLAVES_COMPRA = frozenset({"pedido", "comercio", "ficha", "intencion", "referencia"})
_CLAVES_FICHA = frozenset({"requisitos", "categoria_taxonomia"})
_CLAVES_FICHA_EVALUAR = _CLAVES_FICHA | {"pedido"}
_CLAVES_INTENCION = frozenset({"porque", "preguntar"})
_CLAVES_REQUISITO = frozenset(Requisito.model_fields)
_CLAVES_EVALUAR = frozenset({"ficha", "checkout", "ofertas", "comercio"})
_CLAVES_OFERTAS = frozenset({"carts", "offer_details"})
_CLAVES_FILTRAR = frozenset({"contenido", "origen"})
_CLAVES_RESPUESTA = frozenset({"respuesta", "omitir"})
_CLAVES_DECISION = frozenset({"oportunidad_id", "decision"})
_CLAVES_APROBACION = frozenset({"cart_id", "contexto_id"})

_SIN_CACHE = {"Cache-Control": "no-store"}
_MENSAJE_INTERNO = "Error interno del gateway."


# -- errores -----------------------------------------------------------------


def respuesta_error(e: ErrorApi) -> JSONResponse:
    """`{"error": {codigo, mensaje, detalle?}}` con el status del error."""
    headers = dict(_SIN_CACHE)
    if e.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(e.a_json(), status_code=e.status, headers=headers)


def _json(cuerpo: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(cuerpo, status_code=status, headers=_SIN_CACHE)


class _Problemas:
    """Junta los problemas de validación para devolverlos todos juntos en
    `detalle.campos` (el integrador corrige una vez, no de a uno)."""

    def __init__(self) -> None:
        self.campos: list[dict[str, str]] = []

    def agregar(self, campo: str, problema: str) -> None:
        self.campos.append({"campo": campo, "problema": problema})

    def claves(self, datos: dict[str, Any], permitidas: frozenset[str], prefijo: str) -> None:
        for clave in sorted(set(datos) - permitidas, key=str):
            self.agregar(f"{prefijo}{clave}"[:120], "clave desconocida")

    def lanzar(self) -> None:
        if self.campos:
            raise ErrorApi(
                400, "validacion", "La solicitud no es válida.",
                {"campos": self.campos[:50]},
            )


def _error_validacion(campo: str, problema: str) -> ErrorApi:
    return ErrorApi(
        400, "validacion", "La solicitud no es válida.",
        {"campos": [{"campo": campo, "problema": problema}]},
    )


def _objeto(body: Any, campo: str = "") -> dict[str, Any]:
    if not isinstance(body, dict):
        raise _error_validacion(campo or "(cuerpo)", "se espera un objeto JSON")
    return body


def _texto(
    problemas: _Problemas, datos: dict[str, Any], clave: str, campo: str, *,
    largo: int, obligatorio: bool,
) -> str | None:
    """Un str de 1 a `largo` caracteres, sin espacios en los extremos."""
    if clave not in datos or datos[clave] is None:
        if obligatorio:
            problemas.agregar(campo, "obligatorio")
        return None
    valor = datos[clave]
    if not isinstance(valor, str):
        problemas.agregar(campo, "se espera texto")
        return None
    valor = valor.strip()
    if not 1 <= len(valor) <= largo:
        problemas.agregar(campo, f"de 1 a {largo} caracteres")
        return None
    return valor


def _slug(problemas: _Problemas, datos: dict[str, Any], clave: str) -> str | None:
    valor = datos.get(clave)
    if valor is None:
        return None
    if not es_id(ID_SLUG_RE, valor):
        problemas.agregar(clave, "identificador inválido")
        return None
    return valor


# -- autenticación y cuerpo --------------------------------------------------


def integrador_de(request: Request) -> Integrador:
    """El integrador que dejó el middleware. Sin él, 401: la API no confía
    en que el middleware esté montado."""
    integrador = getattr(request.state, "integrador", None)
    if not isinstance(integrador, Integrador):
        raise ErrorApi(401, "no_autenticado", "Falta la clave de API.")
    return integrador


async def leer_json(request: Request, max_bytes: int) -> Any:
    """El cuerpo JSON de un POST: 415 sin `application/json`, 413 si supera
    el tope (se corta al leer, no se confía en `Content-Length`) y 400 si
    no es JSON estricto (sin NaN, Infinity ni números que desbordan), si
    está anidado a más de `MAX_PROFUNDIDAD_JSON` niveles o si trae texto que
    no es UTF-8 (un surrogate suelto)."""
    tipo = request.headers.get("content-type", "")
    if tipo.split(";", 1)[0].strip().lower() != "application/json":
        raise ErrorApi(415, "se_espera_json", "El cuerpo tiene que ser application/json.")
    largo = request.headers.get("content-length")
    if largo is not None and largo.isdigit() and int(largo) > max_bytes:
        raise ErrorApi(
            413, "cuerpo_demasiado_grande",
            f"El cuerpo supera el máximo de {max_bytes} bytes.",
        )
    try:
        crudo = await a2a.leer_acotado(request.stream(), max_bytes)
    except a2a.LimiteExcedido:
        raise ErrorApi(
            413, "cuerpo_demasiado_grande",
            f"El cuerpo supera el máximo de {max_bytes} bytes.",
        ) from None
    datos, motivo = a2a.parsear_json(crudo)
    if motivo is not None:
        raise ErrorApi(400, "json_invalido", "El cuerpo no es JSON válido.")
    if a2a.anidado_de_mas(datos, MAX_PROFUNDIDAD_JSON):
        raise ErrorApi(
            400, "json_invalido",
            f"El cuerpo tiene más de {MAX_PROFUNDIDAD_JSON} niveles de anidamiento.",
        )
    try:
        # Un escape de un surrogate suelto es JSON válido pero no texto UTF-8:
        # la compra se creaba y fallaba al responder (500). Se rechaza acá.
        a2a.dumps(datos)
    except UnicodeEncodeError:
        raise ErrorApi(
            400, "json_invalido", "El cuerpo tiene texto que no es UTF-8 válido."
        ) from None
    return datos


def _bytes_json(datos: Any) -> int:
    """Lo que pesa `datos` en JSON compacto (UTF-8). Viene de `leer_json`:
    siempre se puede volver a escribir (estricto y con la profundidad acotada)."""
    return len(json.dumps(datos, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


# -- ficha -------------------------------------------------------------------


def _soportados() -> tuple[frozenset[str], frozenset[str]]:
    """Lo que el Guard sabe chequear. Import perezoso: importar la API no
    carga el Guard (el proxy ya lo carga con el evaluador)."""
    from guard.chequeos import CAMPOS_SOPORTADOS, OPERADORES_SOPORTADOS

    return frozenset(CAMPOS_SOPORTADOS), frozenset(OPERADORES_SOPORTADOS)


def _entregas_validas(valor: Any) -> Any:
    from guard.normalizar import entregas_validas

    return entregas_validas(valor)


def _numero_positivo(valor: Any) -> bool:
    return (
        isinstance(valor, (int, float)) and not isinstance(valor, bool)
        and _en_rango(valor) and math.isfinite(valor) and valor > 0
    )


def _en_rango(valor: Any) -> bool:
    """¿Todos los números de `valor` (también dentro de listas y objetos, como
    un cronograma) entran en un float? Un entero de más de 308 dígitos no: el
    Guard (y `math.isfinite`) lo convierte y lanza `OverflowError`. Iterativo:
    la profundidad ya la acotó `leer_json`."""
    pila = [valor]
    while pila:
        actual = pila.pop()
        if isinstance(actual, int) and not isinstance(actual, bool):
            try:
                float(actual)
            except OverflowError:
                return False
        elif isinstance(actual, dict):
            pila.extend(actual.values())
        elif isinstance(actual, list):
            pila.extend(actual)
    return True


def _tiene_tope(requisitos: list[Requisito]) -> bool:
    """Precio máximo obligatorio > 0 y moneda obligatoria ISO: sin tope de
    gasto, la ficha no autoriza nada (y AP2 no tendría contra qué frenar)."""
    precio = any(
        r.campo == "precio" and r.operador == "maximo" and r.obligatorio
        and _numero_positivo(r.valor)
        for r in requisitos
    )
    moneda = any(
        r.campo == "moneda" and r.operador == "igual" and r.obligatorio
        and isinstance(r.valor, str) and MONEDA_RE.fullmatch(r.valor) is not None
        for r in requisitos
    )
    return precio and moneda


def _validar_ficha(
    pedido: str, datos: Any, *, prefijo: str, problemas: _Problemas,
    permitidas: frozenset[str] = _CLAVES_FICHA, solo_forma: bool = False,
) -> Ficha | None:
    """Arma la ficha juntando los problemas de forma en `problemas`.

    Devuelve None si hubo problemas de forma (el llamador lanza
    `validacion` con todos juntos). Los dos errores de fondo, requisito no
    soportado y ficha sin tope, se lanzan acá, después de la forma, salvo
    con `solo_forma` (el resto de la solicitud ya tiene errores de forma:
    primero se arregla eso y se devuelven todos juntos).
    """
    if not isinstance(datos, dict):
        problemas.agregar(prefijo.rstrip("."), "se espera un objeto")
        return None
    problemas.claves(datos, permitidas, prefijo)
    antes = len(problemas.campos)

    categoria = datos.get("categoria_taxonomia")
    if categoria is not None and not (
        isinstance(categoria, str) and CATEGORIA_RE.fullmatch(categoria)
    ):
        problemas.agregar(f"{prefijo}categoria_taxonomia", "solo dígitos, hasta 12")
        categoria = None

    crudos = datos.get("requisitos")
    if not isinstance(crudos, list) or not 1 <= len(crudos) <= MAX_REQUISITOS:
        problemas.agregar(f"{prefijo}requisitos", f"lista de 1 a {MAX_REQUISITOS} requisitos")
        return None

    campos_ok, operadores_ok = _soportados()
    requisitos: list[Requisito] = []
    no_soportados: list[dict[str, str]] = []
    for i, crudo in enumerate(crudos):
        campo = f"{prefijo}requisitos[{i}]"
        if not isinstance(crudo, dict):
            problemas.agregar(campo, "se espera un objeto")
            continue
        problemas.claves(crudo, _CLAVES_REQUISITO, f"{campo}.")
        nombre, operador = crudo.get("campo"), crudo.get("operador")
        if not isinstance(nombre, str) or not isinstance(operador, str):
            problemas.agregar(campo, "campo y operador son texto obligatorio")
            continue
        if nombre not in campos_ok:
            no_soportados.append({"campo": f"{campo}.campo", "problema": "el Guard no lo chequea"})
            continue
        if operador not in operadores_ok:
            no_soportados.append({"campo": f"{campo}.operador", "problema": "el Guard no lo chequea"})
            continue
        try:
            # Estricto: en una autorización, "obligatorio": "no" no es False.
            requisito = Requisito.model_validate(crudo, strict=True)
        except ValidationError as e:
            for err in e.errors(include_url=False, include_input=False, include_context=False):
                lugar = ".".join(str(p) for p in err.get("loc", ()))
                problemas.agregar(f"{campo}.{lugar}" if lugar else campo, str(err.get("msg", ""))[:200])
            continue
        if not _en_rango(requisito.valor):
            problemas.agregar(f"{campo}.valor", "número fuera de rango")
            continue
        if requisito.operador == "cubre" and _entregas_validas(requisito.valor) is None:
            # El Guard escalaría un cronograma ilegible; mejor avisarlo al crear.
            problemas.agregar(
                f"{campo}.valor", "cronograma inválido: lista de {cantidad, fecha}",
            )
            continue
        requisitos.append(requisito)

    if len(problemas.campos) > antes or solo_forma:
        return None
    if no_soportados:
        raise ErrorApi(
            400, "requisito_no_soportado",
            "Hay requisitos que el Guard no sabe verificar.",
            {"campos": no_soportados},
        )
    if not _tiene_tope(requisitos):
        raise ErrorApi(
            400, "ficha_sin_tope",
            "La ficha tiene que tener un precio máximo obligatorio y una moneda "
            "obligatoria (código ISO de 3 letras).",
        )
    return Ficha(
        pedido_original=pedido, requisitos=requisitos, confirmada=True,
        categoria_taxonomia=categoria,
    )


def validar_ficha(pedido: str, datos: Any) -> Ficha:
    """La ficha autorizada del integrador (ErrorApi 400 si no sirve)."""
    problemas = _Problemas()
    ficha = _validar_ficha(pedido, datos, prefijo="ficha.", problemas=problemas)
    problemas.lanzar()
    assert ficha is not None
    return ficha


# -- solicitudes -------------------------------------------------------------


def validar_solicitud_compra(body: Any) -> SolicitudCompra:
    datos = _objeto(body)
    problemas = _Problemas()
    problemas.claves(datos, _CLAVES_COMPRA, "")

    pedido = _texto(problemas, datos, "pedido", "pedido", largo=LARGO_PEDIDO, obligatorio=True)
    comercio = _slug(problemas, datos, "comercio")

    porque: str | None = None
    preguntar = False
    intencion = datos.get("intencion")
    if intencion is not None:
        if not isinstance(intencion, dict):
            problemas.agregar("intencion", "se espera un objeto")
        else:
            problemas.claves(intencion, _CLAVES_INTENCION, "intencion.")
            porque = _texto(
                problemas, intencion, "porque", "intencion.porque",
                largo=LARGO_RESPUESTA, obligatorio=False,
            )
            valor = intencion.get("preguntar", False)
            if not isinstance(valor, bool):
                problemas.agregar("intencion.preguntar", "se espera true o false")
            else:
                preguntar = valor

    referencia = _texto(
        problemas, datos, "referencia", "referencia", largo=LARGO_REFERENCIA, obligatorio=False,
    )
    if referencia is not None and not referencia.isprintable():
        problemas.agregar("referencia", "solo caracteres imprimibles")
        referencia = None

    ficha: Ficha | None = None
    if "ficha" not in datos:
        problemas.agregar("ficha", "obligatorio")
    elif _bytes_json(datos["ficha"]) > MAX_BYTES_FICHA:
        problemas.agregar("ficha", f"supera {MAX_BYTES_FICHA} bytes")
    else:
        # Con el pedido inválido igual se mira la forma de la ficha, para
        # devolver todos los problemas juntos.
        ficha = _validar_ficha(
            pedido or "-", datos["ficha"], prefijo="ficha.", problemas=problemas,
            solo_forma=bool(problemas.campos),
        )
    problemas.lanzar()
    assert pedido is not None and ficha is not None
    return SolicitudCompra(
        pedido=pedido, ficha=ficha, comercio_id=comercio, porque=porque,
        preguntar_porque=preguntar, referencia=referencia,
    )


def _validar_checkout(datos: Any) -> dict[str, Any]:
    """El data part `ap2.checkout` tal cual; tiene que validar como
    `CheckoutVisto` y traer un `cart_id` seguro."""
    error = ErrorApi(400, "checkout_invalido", "El checkout no es un ap2.checkout válido.")
    if not isinstance(datos, dict):
        raise error
    try:
        visto = CheckoutVisto.model_validate(datos)
    except ValidationError:
        raise error from None
    if not es_id(ID_SEGURO_RE, visto.cart_id):
        raise error
    return datos


def _validar_ofertas(datos: Any, problemas: _Problemas) -> OfertasVistas | None:
    if datos is None:
        return None
    if not isinstance(datos, dict):
        problemas.agregar("ofertas", "se espera un objeto")
        return None
    problemas.claves(datos, _CLAVES_OFERTAS, "ofertas.")
    carts = datos.get("carts")
    detalles = datos.get("offer_details", [])
    ok = True
    if not (
        isinstance(carts, list) and 1 <= len(carts) <= MAX_CARTS
        and all(isinstance(c, dict) for c in carts)
    ):
        problemas.agregar("ofertas.carts", f"lista de 1 a {MAX_CARTS} objetos")
        ok = False
    if not (
        isinstance(detalles, list) and len(detalles) <= MAX_CARTS
        and all(isinstance(d, dict) for d in detalles)
    ):
        problemas.agregar("ofertas.offer_details", f"lista de hasta {MAX_CARTS} objetos")
        ok = False
    # `comercio_id` no se acepta del integrador: lo completa el núcleo con el
    # `merchant.id` firmado.
    return OfertasVistas(carts=carts, offer_details=detalles) if ok else None


def validar_solicitud_evaluar(body: Any) -> SolicitudEvaluar:
    datos = _objeto(body)
    problemas = _Problemas()
    problemas.claves(datos, _CLAVES_EVALUAR, "")
    comercio = _slug(problemas, datos, "comercio")
    ofertas = _validar_ofertas(datos.get("ofertas"), problemas)

    ficha_cruda = datos.get("ficha")
    pedido: str | None = None
    if not isinstance(ficha_cruda, dict):
        problemas.agregar("ficha", "obligatorio (objeto)")
    else:
        pedido = _texto(
            problemas, ficha_cruda, "pedido", "ficha.pedido", largo=LARGO_PEDIDO, obligatorio=True,
        )
    if "checkout" not in datos:
        problemas.agregar("checkout", "obligatorio")

    ficha: Ficha | None = None
    if isinstance(ficha_cruda, dict):
        ficha = _validar_ficha(
            pedido or "-", ficha_cruda, prefijo="ficha.", problemas=problemas,
            permitidas=_CLAVES_FICHA_EVALUAR, solo_forma=bool(problemas.campos),
        )
    problemas.lanzar()
    checkout = _validar_checkout(datos["checkout"])
    assert ficha is not None
    return SolicitudEvaluar(ficha=ficha, checkout=checkout, ofertas=ofertas, comercio_id=comercio)


def validar_solicitud_filtrar(body: Any) -> SolicitudFiltrar:
    """`contenido` es cualquier JSON (texto, objeto, lista); `origen`, de
    dónde lo sacó el agente (por defecto "otro")."""
    datos = _objeto(body)
    problemas = _Problemas()
    problemas.claves(datos, _CLAVES_FILTRAR, "")
    if "contenido" not in datos:
        problemas.agregar("contenido", "obligatorio")
    origen = datos.get("origen", "otro")
    if origen not in ORIGENES_CONTENIDO:
        problemas.agregar("origen", "uno de: " + ", ".join(ORIGENES_CONTENIDO))
    problemas.lanzar()
    return SolicitudFiltrar(contenido=datos["contenido"], origen=origen)


def _validar_respuesta(body: Any) -> str | None:
    """`{"respuesta": "…"}` o `{"omitir": true}` (uno de los dos)."""
    datos = _objeto(body)
    problemas = _Problemas()
    problemas.claves(datos, _CLAVES_RESPUESTA, "")
    omitir = datos.get("omitir", False)
    tiene_respuesta = datos.get("respuesta") is not None
    if not isinstance(omitir, bool):
        problemas.agregar("omitir", "se espera true o false")
    elif omitir == tiene_respuesta:
        problemas.agregar("respuesta", 'mandá "respuesta" o "omitir": true (uno de los dos)')
    respuesta = None
    if tiene_respuesta and not omitir:
        respuesta = _texto(
            problemas, datos, "respuesta", "respuesta", largo=LARGO_RESPUESTA, obligatorio=True,
        )
    problemas.lanzar()
    return respuesta


def _validar_decision(body: Any) -> tuple[str, str]:
    datos = _objeto(body)
    problemas = _Problemas()
    problemas.claves(datos, _CLAVES_DECISION, "")
    oportunidad_id = datos.get("oportunidad_id")
    if not es_id(ID_SEGURO_RE, oportunidad_id):
        problemas.agregar("oportunidad_id", "identificador inválido u omitido")
    decision = datos.get("decision")
    if decision not in ("aceptar", "rechazar"):
        problemas.agregar("decision", 'uno de: "aceptar", "rechazar"')
    problemas.lanzar()
    return oportunidad_id, decision


def _validar_aprobacion(body: Any) -> tuple[str, str | None]:
    datos = _objeto(body)
    problemas = _Problemas()
    problemas.claves(datos, _CLAVES_APROBACION, "")
    cart_id = datos.get("cart_id")
    if not es_id(ID_SEGURO_RE, cart_id):
        problemas.agregar("cart_id", "identificador inválido u omitido")
    contexto_id = datos.get("contexto_id")
    if contexto_id is not None and not es_id(ID_SEGURO_RE, contexto_id):
        problemas.agregar("contexto_id", "identificador inválido")
    problemas.lanzar()
    return cart_id, contexto_id


def _limite(request: Request) -> int:
    crudo = request.query_params.get("limite")
    if crudo is None:
        return LIMITE_POR_DEFECTO
    if not (crudo.isascii() and crudo.isdigit()) or not 1 <= int(crudo) <= LIMITE_MAXIMO:
        raise _error_validacion("limite", f"entero de 1 a {LIMITE_MAXIMO}")
    return int(crudo)


# -- historial: filtros, cursor y ventana ----------------------------------------


def _fecha_param(request: Request, nombre: str, *, fin: bool = False) -> datetime | None:
    """Una fecha ISO 8601 de la query: `YYYY-MM-DD` (un día UTC entero: como
    `hasta` cuenta hasta el final de ese día) o fecha y hora (sin zona, UTC;
    un `+` sin codificar llega como espacio y se acepta igual)."""
    crudo = request.query_params.get(nombre)
    if crudo is None:
        return None
    texto = crudo.strip().replace(" ", "+")
    problema = _error_validacion(nombre, "fecha ISO 8601: YYYY-MM-DD o fecha y hora")
    try:
        if FECHA_RE.fullmatch(texto):
            dia = date.fromisoformat(texto)
            momento = datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc)
            return momento + timedelta(days=1) if fin else momento
        if FECHA_HORA_RE.fullmatch(texto):
            momento = datetime.fromisoformat(texto.replace("Z", "+00:00"))
            return momento if momento.tzinfo is not None else momento.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        raise problema from None
    raise problema


def _rango(desde: datetime | None, hasta: datetime | None) -> None:
    if desde is not None and hasta is not None and desde >= hasta:
        raise _error_validacion("desde", "tiene que ser anterior a hasta")


def codificar_cursor(cursor: tuple[datetime, str] | None) -> str | None:
    """El cursor opaco de `siguiente`: base64url (sin relleno) de un JSON con
    la última compra de la página. No se interpreta afuera."""
    if cursor is None:
        return None
    creada, compra_id = cursor
    crudo = json.dumps(
        {"v": VERSION_CURSOR, "c": creada.isoformat(), "i": compra_id}, separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(crudo).decode("ascii").rstrip("=")


def decodificar_cursor(texto: Any) -> tuple[datetime, str]:
    """El cursor de `siguiente`, validado (no es un filtro libre: la fecha y
    el id se vuelven a escribir antes de ir a la base). Si no sirve, 400."""
    problema = _error_validacion("cursor", "cursor inválido: usá el `siguiente` de la respuesta")
    if not isinstance(texto, str) or not CURSOR_RE.fullmatch(texto):
        raise problema
    try:
        crudo = base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))
        datos = json.loads(crudo)
        if not isinstance(datos, dict) or set(datos) != {"v", "c", "i"} or datos["v"] != VERSION_CURSOR:
            raise ValueError
        creada = datetime.fromisoformat(datos["c"]) if isinstance(datos["c"], str) else None
        if creada is None or creada.tzinfo is None or not es_id(ID_COMPRA_RE, datos["i"]):
            raise ValueError
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, RecursionError):
        raise problema from None
    return creada, datos["i"]


def _filtros_listado(request: Request) -> FiltrosListado:
    limite = _limite(request)
    estado = request.query_params.get("estado")
    if estado is not None and estado not in ESTADOS_COMPRA:
        raise _error_validacion("estado", "estado desconocido")
    alerta = request.query_params.get("alerta")
    if alerta is not None and alerta not in ALERTAS:
        raise _error_validacion("alerta", "una de: " + ", ".join(ALERTAS))
    problemas = _Problemas()
    referencia = _texto(
        problemas, dict(request.query_params), "referencia", "referencia",
        largo=LARGO_REFERENCIA, obligatorio=False,
    )
    if referencia is not None and not referencia.isprintable():
        problemas.agregar("referencia", "solo caracteres imprimibles")
    problemas.lanzar()
    desde = _fecha_param(request, "desde")
    hasta = _fecha_param(request, "hasta", fin=True)
    _rango(desde, hasta)
    crudo = request.query_params.get("cursor")
    cursor = decodificar_cursor(crudo) if crudo is not None else None
    return FiltrosListado(
        limite=limite, estado=estado, desde=desde, hasta=hasta, referencia=referencia,
        alerta=alerta, cursor=cursor,
    )


def _ventana_resumen(request: Request, ahora: datetime) -> tuple[datetime, datetime]:
    """[`desde`, `hasta`) del resumen: por defecto los últimos 30 días hasta
    el final de hoy (UTC), así dos pedidos del mismo día ven la misma
    ventana; con una sola punta, 30 días desde o hasta ella. Como mucho 366
    días."""
    desde = _fecha_param(request, "desde")
    hasta = _fecha_param(request, "hasta", fin=True)
    if hasta is None:
        fin_de_hoy = datetime(ahora.year, ahora.month, ahora.day, tzinfo=timezone.utc) + timedelta(days=1)
        hasta = fin_de_hoy if desde is None or desde < fin_de_hoy else desde + timedelta(days=DIAS_RESUMEN)
    if desde is None:
        desde = hasta - timedelta(days=DIAS_RESUMEN)
    _rango(desde, hasta)
    if hasta - desde > timedelta(days=MAX_DIAS_RESUMEN):
        raise _error_validacion("desde", f"el período es de hasta {MAX_DIAS_RESUMEN} días")
    return desde, hasta


def _compra_id(request: Request) -> str:
    """Un id con otro formato no puede existir: el mismo 404 que una compra
    de otro integrador (no se filtra nada)."""
    compra_id = request.path_params.get("compra_id")
    if not es_id(ID_COMPRA_RE, compra_id):
        raise ErrorApi(404, "compra_desconocida", "No existe esa compra.")
    return compra_id


# -- rutas -------------------------------------------------------------------

Handler = Callable[[Request], Awaitable[Response]]


def _protegido(nombre: str, handler: Handler) -> Handler:
    """Todo handler: ErrorApi → su respuesta; cualquier otra excepción → 500
    `interno` con mensaje fijo (el detalle queda en el log, nunca en la
    respuesta: puede traer texto del comercio)."""

    async def envuelto(request: Request) -> Response:
        try:
            return await handler(request)
        except ErrorApi as e:
            return respuesta_error(e)
        except Exception:
            log.exception("/v1 %s: error interno", nombre)
            return respuesta_error(ErrorApi(500, "interno", _MENSAJE_INTERNO))

    envuelto.__name__ = nombre
    return envuelto


class _RutaFinal:
    """Endpoint ASGI de las rutas finales de `/v1`. Es una clase (no una
    función) para que Starlette no le fije métodos: atiende cualquiera, también
    uno raro (`PROPFIND`), y así nada de `/v1` sale con el texto plano de
    Starlette."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        respuesta = await self.handler(Request(scope, receive))
        await respuesta(scope, receive, send)


def _rutas_finales(rutas: list[Route]) -> list[Route]:
    """Lo que ninguna ruta de `/v1` atendió, con el `ErrorApi` documentado:
    405 `metodo_no_permitido` (con `Allow`) si la ruta existe con otro método,
    404 `no_encontrado` si no existe. Van al final y fuera del spec."""

    async def no_encontrado(request: Request) -> Response:
        permitidos = sorted({
            metodo
            for ruta in rutas
            if ruta.matches(request.scope)[0] is Match.PARTIAL
            for metodo in ruta.methods or ()
        })
        if request.scope.get("path") not in RUTAS_V1_PUBLICAS:
            # Sin clave, 401 como en el middleware: no se dice qué rutas existen.
            integrador_de(request)
        if not permitidos:
            raise ErrorApi(404, "no_encontrado", "No existe.")
        respuesta = respuesta_error(
            ErrorApi(405, "metodo_no_permitido", "La ruta no acepta ese método.")
        )
        respuesta.headers["Allow"] = ", ".join(permitidos)
        return respuesta

    final = _RutaFinal(_protegido("no_encontrado", no_encontrado))
    return [
        Route("/v1", final, include_in_schema=False),
        # `{resto:path}` también toma `/v1/` y `/v1/compras/`: 404 en JSON en
        # lugar de la redirección de Starlette por la barra final.
        Route("/v1/{resto:path}", final, include_in_schema=False),
    ]


def rutas_v1(
    nucleo: NucleoGateway, *, max_cuerpo_bytes: int = 1_048_576,
    url_publica: str = "http://localhost:8030",
) -> list[Route]:
    """Las rutas de §1.5 contra el núcleo. El spec y la página de docs se
    arman una vez: no dependen del pedido."""
    spec = openapi.especificacion(url_publica)
    docs = openapi.pagina_docs(spec)

    def vista(foto: FotoCompra) -> dict[str, Any]:
        a2a_url = nucleo.url_a2a(foto.comercio_id) if foto.comercio_id else None
        return vistas.vista_compra(foto, a2a_url=a2a_url)

    def resumen_de(foto: FotoCompra, vista_compra: dict[str, Any], ahora: datetime) -> dict[str, Any]:
        return explicacion.resumen_de_compra(
            vista_compra, ahora=ahora, ttl_s=nucleo.ttl_compra_s, corte=foto.corte,
            bloqueo_proxy=foto.bloqueo_proxy,
        )

    def vista_listada(foto: FotoCompra, ahora: datetime) -> dict[str, Any]:
        """La `VistaCompra` con lo del historial: `resultado`, `alertas` e
        `intento_de_estafa` (de los veredictos guardados, sin eventos). Sin el
        `chequeos_detalle` de los veredictos: 200 compras con 20 veredictos
        cada una no entran en una respuesta (el detalle está en la compra y en
        su explicación)."""
        datos = vista(foto)
        resumen = resumen_de(foto, datos, ahora)
        datos["veredictos"] = [
            {k: v for k, v in veredicto.items() if k != "chequeos_detalle"}
            for veredicto in datos["veredictos"]
        ]
        return {**datos, **resumen}

    async def crear_compra(request: Request) -> Response:
        integrador = integrador_de(request)
        solicitud = validar_solicitud_compra(await leer_json(request, max_cuerpo_bytes))
        foto = await nucleo.crear_compra(integrador, solicitud)
        return _json(vista(foto), 201)

    async def listar_compras(request: Request) -> Response:
        integrador = integrador_de(request)
        filtros = _filtros_listado(request)
        pagina = await nucleo.listar_compras(integrador, filtros)
        ahora = datetime.now(timezone.utc)
        return _json({
            "compras": [vista_listada(f, ahora) for f in pagina.fotos[:filtros.limite]],
            "siguiente": codificar_cursor(pagina.siguiente),
        })

    async def explicar(request: Request) -> Response:
        integrador = integrador_de(request)
        foto, eventos = await nucleo.explicacion_compra(integrador, _compra_id(request))
        return _json(explicacion.explicar(
            vista(foto), eventos, ahora=datetime.now(timezone.utc), ttl_s=nucleo.ttl_compra_s,
            corte=foto.corte, bloqueo_proxy=foto.bloqueo_proxy,
        ))

    async def resumen(request: Request) -> Response:
        integrador = integrador_de(request)
        ahora = datetime.now(timezone.utc)
        desde, hasta = _ventana_resumen(request, ahora)
        fotos, completo = await nucleo.resumen_compras(integrador, desde, hasta)
        entradas = []
        for foto in fotos:
            datos = vistas.vista_compra(foto, a2a_url=None)
            monto, moneda = explicacion.monto_de_compra(datos, foto.bloqueo_proxy)
            entradas.append({**resumen_de(foto, datos, ahora), "monto": monto, "moneda": moneda})
        return _json({**explicacion.resumir(entradas, desde=desde, hasta=hasta), "completo": completo})

    async def obtener_compra(request: Request) -> Response:
        integrador = integrador_de(request)
        foto = await nucleo.obtener_compra(integrador, _compra_id(request))
        return _json(vista(foto))

    async def responder(request: Request) -> Response:
        integrador = integrador_de(request)
        compra_id = _compra_id(request)
        respuesta = _validar_respuesta(await leer_json(request, max_cuerpo_bytes))
        foto = await nucleo.responder_porque(integrador, compra_id, respuesta)
        return _json(vista(foto))

    async def decidir(request: Request) -> Response:
        integrador = integrador_de(request)
        compra_id = _compra_id(request)
        oportunidad_id, decision = _validar_decision(await leer_json(request, max_cuerpo_bytes))
        foto = await nucleo.decidir_oportunidad(
            integrador, compra_id, oportunidad_id, decision,  # type: ignore[arg-type]
        )
        return _json(vista(foto))

    async def aprobar(request: Request) -> Response:
        integrador = integrador_de(request)
        compra_id = _compra_id(request)
        cart_id, contexto_id = _validar_aprobacion(await leer_json(request, max_cuerpo_bytes))
        datos, creada = await nucleo.aprobar(integrador, compra_id, cart_id, contexto_id)
        return _json(vistas.vista_aprobacion(datos), 201 if creada else 200)

    async def evaluar(request: Request) -> Response:
        integrador = integrador_de(request)
        solicitud = validar_solicitud_evaluar(await leer_json(request, max_cuerpo_bytes))
        return _json(await nucleo.evaluar(integrador, solicitud))

    async def filtrar(request: Request) -> Response:
        integrador = integrador_de(request)
        solicitud = validar_solicitud_filtrar(await leer_json(request, max_cuerpo_bytes))
        return _json(await nucleo.filtrar(integrador, solicitud))

    async def comercios(request: Request) -> Response:
        integrador = integrador_de(request)
        return _json({
            "comercios": [
                {"id": c.id, "nombre": c.nombre, "a2a_url": nucleo.url_a2a(c.id)}
                for c in nucleo.comercios_de(integrador)
            ]
        })

    async def entregas(request: Request) -> Response:
        integrador = integrador_de(request)
        entregas = nucleo.entregas_webhook(integrador, _limite(request))
        if inspect.isawaitable(entregas):
            # Con varias instancias el núcleo las lee del almacén.
            entregas = await entregas
        return _json({"entregas": entregas})

    async def probar_webhook(request: Request) -> Response:
        integrador = integrador_de(request)
        datos = _objeto(await leer_json(request, max_cuerpo_bytes))
        problemas = _Problemas()
        problemas.claves(datos, frozenset(), "")
        problemas.lanzar()
        return _json(await nucleo.probar_webhook(integrador))

    async def clave_publica(request: Request) -> Response:
        clave = nucleo.clave_publica()
        if clave is None:
            raise ErrorApi(
                503, "sin_clave_recibo",
                "El gateway no tiene una clave de recibos publicada.",
            )
        return _json(clave)

    async def especificacion(request: Request) -> Response:
        return JSONResponse(spec)

    async def pagina(request: Request) -> Response:
        return HTMLResponse(
            docs,
            headers={
                # Autocontenida: sin scripts ni recursos externos.
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    tabla: list[tuple[str, str, Handler]] = [
        ("/v1/compras", "POST", crear_compra),
        ("/v1/compras", "GET", listar_compras),
        ("/v1/compras/{compra_id}", "GET", obtener_compra),
        ("/v1/compras/{compra_id}/explicacion", "GET", explicar),
        ("/v1/resumen", "GET", resumen),
        ("/v1/compras/{compra_id}/respuesta", "POST", responder),
        ("/v1/compras/{compra_id}/decision", "POST", decidir),
        ("/v1/compras/{compra_id}/aprobaciones", "POST", aprobar),
        ("/v1/evaluar", "POST", evaluar),
        ("/v1/filtrar", "POST", filtrar),
        ("/v1/comercios", "GET", comercios),
        ("/v1/webhooks/entregas", "GET", entregas),
        ("/v1/webhooks/prueba", "POST", probar_webhook),
        ("/v1/public-key", "GET", clave_publica),
        ("/v1/openapi.json", "GET", especificacion),
        ("/v1/docs", "GET", pagina),
    ]
    rutas = [
        Route(path, _protegido(handler.__name__, handler), methods=[metodo])
        for path, metodo, handler in tabla
    ]
    return [*rutas, *_rutas_finales(rutas)]
