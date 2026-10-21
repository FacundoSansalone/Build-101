"""Claves de API de los integradores y autenticación del gateway.

- **Formato:** `psg_<kid>_<secreto>`. El `kid` (12 hex) es público y sirve
  para buscar la clave sin recorrer todas; el secreto sale de
  `secrets.token_urlsafe(32)` y puede traer `_`, así que el parseo parte solo
  en los dos primeros `_`.
- **Qué se guarda:** solo `sha256(clave_completa)`. El archivo de claves
  (`GATEWAY_CLAVES_ARCHIVO`) no tiene nada que sirva para autenticarse si se
  filtra. Como el hash cubre la clave entera, que coincida implica que el
  `kid` también.
- **Tiempo constante:** la comparación es `hmac.compare_digest` y un `kid`
  inexistente se compara contra un hash ficticio fijo, para que el tiempo de
  respuesta no diga qué `kid` existen.
- **Middleware (`AutenticacionGateway`):** el más externo. Reconoce el oyente
  por el puerto local de la conexión (o, con `todo_publico=True`, trata todo
  como público: en Vercel no hay puerto) y aplica las reglas por ruta del
  diseño: en el oyente público solo existen `/a2a/*` y `/v1/*`, y piden
  clave; en el local nada cambia para la demo (`/a2a/*` anónimo sigue
  andando) salvo que una clave `psg_` presente tiene que ser válida. Las
  `rutas_abiertas` (rutas exactas: la consola web, `proxy/consola/`) pasan
  sin clave en los dos oyentes: son archivos estáticos sin estado.
- **Claves de solo lectura** (`"permisos": ["lectura"]` en la clave; sin el
  campo, la clave es completa como siempre): para el panel web, que corre
  en el navegador. Son del mismo integrador y ven lo mismo, pero solo pueden
  lo que está en la lista blanca `RUTAS_LECTURA` (GET en `/v1`); el resto de
  `/v1` y todo `/a2a/*` da 403 `solo_lectura` acá, antes de llegar a la app.
  El permiso sale de la clave autenticada, nunca de algo que mande el
  cliente.

Nunca se loguea la clave ni el header `Authorization`. Middleware ASGI puro
(como `ControlLocal`) para no interferir con el SSE.

CLI: `python -m proxy.claves nueva --integrador acme [--lectura]` y
`python -m proxy.claves secreto-webhook`.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from proxy.tipos_gateway import (
    ID_SLUG_RE,
    KID_RE,
    PREFIJO_CLAVE,
    RUTAS_V1_PUBLICAS,
    ConfigInvalida,
    ConfigWebhook,
    ErrorApi,
    Integrador,
    Oyente,
    es_id,
)

logger = logging.getLogger("proxy.claves")

# El secreto es base64url (lo que da token_urlsafe). El tope evita hashear
# headers enormes y deja margen para secretos más largos que los de hoy.
_SECRETO_RE = re.compile(r"[A-Za-z0-9_-]{16,256}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ESQUEMA_BEARER = "bearer"
# Algo con forma de clave nuestra en cualquier parte del header, como venga
# (otro esquema, sin esquema, en mayúsculas). Pide el kid entero para no
# confundirlo con un token ajeno que por azar trae "psg_".
_PARECE_CLAVE_RE = re.compile(re.escape(PREFIJO_CLAVE) + r"[0-9a-f]{12}_", re.IGNORECASE)
MIN_SECRETO_WEBHOOK = 32
PREFIJO_SECRETO_WEBHOOK = "whsec_"

# Hash contra el que se compara cuando el kid no existe: mismo costo que una
# clave real, así el tiempo no distingue "kid inexistente" de "secreto
# incorrecto". No corresponde a ninguna clave (es el sha256 de un texto fijo
# que no tiene el formato psg_).
_HASH_FICTICIO = hashlib.sha256(b"pre-sign-guard:kid-inexistente").hexdigest()

_CLAVES_ARCHIVO = frozenset({"integradores"})
_CLAVES_INTEGRADOR = frozenset(
    {"id", "nombre", "claves", "comercios", "webhook", "max_compras_activas"}
)
_CLAVES_CLAVE = frozenset({"kid", "sha256", "creada", "activa", "permisos"})
_CLAVES_WEBHOOK = frozenset({"url", "secreto_env"})

# Rutas por oyente (diseño §1.2). `/guard` sin barra también cuenta: en el
# público no tiene que existir nada de la API de control.
_PREFIJO_GUARD = "/guard"
_PREFIJO_V1 = "/v1"
_PREFIJO_A2A = "/a2a"

# -- permisos -----------------------------------------------------------------
PERMISO_LECTURA = "lectura"
PERMISOS_CONOCIDOS: frozenset[str] = frozenset({PERMISO_LECTURA})

# Lo único que puede una clave de lectura: (método, patrón de ruta), con
# `{x}` = un segmento cualquiera (el handler valida el id y da el mismo error
# que a la clave completa). Es una lista blanca: una ruta nueva de `/v1`
# queda prohibida para lectura hasta que alguien la sume acá. Solo GET; las
# rutas públicas (`RUTAS_V1_PUBLICAS`) pasan sin clave como siempre.
RUTAS_LECTURA: tuple[tuple[str, str], ...] = (
    ("GET", "/v1/compras"),
    ("GET", "/v1/compras/{compra_id}"),
    ("GET", "/v1/compras/{compra_id}/explicacion"),
    ("GET", "/v1/resumen"),
    ("GET", "/v1/comercios"),
    ("GET", "/v1/webhooks/entregas"),
)


def _regla(metodo: str, patron: str) -> tuple[str, re.Pattern[str]]:
    segmentos = [
        "[^/]+" if s.startswith("{") and s.endswith("}") else re.escape(s)
        for s in patron.split("/")
    ]
    return metodo, re.compile("/".join(segmentos))


_REGLAS_POR_PERMISO: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    PERMISO_LECTURA: tuple(_regla(m, p) for m, p in RUTAS_LECTURA),
}


# -- formato de la clave ------------------------------------------------------
def hash_clave(clave: str) -> str:
    """sha256 hex de la clave completa (lo único que se guarda)."""
    return hashlib.sha256(clave.encode("utf-8")).hexdigest()


def generar_clave() -> tuple[str, str, str]:
    """Clave nueva: (clave_completa, kid, sha256_hex). La clave en claro se
    muestra una vez y no se guarda en ningún lado."""
    kid = secrets.token_hex(6)
    clave = f"{PREFIJO_CLAVE}{kid}_{secrets.token_urlsafe(32)}"
    return clave, kid, hash_clave(clave)


def parsear_kid(clave: str) -> str | None:
    """El `kid` si `clave` tiene el formato `psg_<kid>_<secreto>`; si no, None.

    Parte solo en el primer `_` después del prefijo: el secreto puede traer
    `_` (base64url)."""
    if not isinstance(clave, str) or not clave.startswith(PREFIJO_CLAVE):
        return None
    kid, sep, secreto = clave[len(PREFIJO_CLAVE):].partition("_")
    if not sep or not es_id(KID_RE, kid) or _SECRETO_RE.fullmatch(secreto) is None:
        return None
    return kid


def extraer_bearer(autorizacion: str | None) -> str | None:
    """La clave si el header es `Bearer psg_…`; si no, None.

    El esquema no distingue mayúsculas (RFC 9110). Lo que no es `Bearer psg_`
    no es nuestro: el middleware decide qué hacer con eso según el oyente. Una
    clave `psg_` mal formada se devuelve igual, para que el rechazo sea
    `clave_invalida` y no un pase como anónimo."""
    if not isinstance(autorizacion, str):
        return None
    esquema, _, resto = autorizacion.strip().partition(" ")
    if esquema.lower() != _ESQUEMA_BEARER:
        return None
    token = resto.strip()
    return token if token.startswith(PREFIJO_CLAVE) else None


# -- registro -----------------------------------------------------------------
@dataclass(frozen=True)
class _ClaveGuardada:
    sha256: str
    activa: bool
    integrador_id: str
    permisos: frozenset[str] | None = None  # None = clave completa (sin "permisos")


@dataclass(frozen=True)
class ClaveAutenticada:
    """Una clave que autenticó: de qué integrador es y qué puede.

    `permisos` None es una clave completa (como todas las de antes); si no,
    la clave solo puede las rutas de esos permisos (hoy, `lectura`)."""

    integrador: Integrador
    permisos: frozenset[str] | None = None

    @property
    def solo_lectura(self) -> bool:
        return self.permisos is not None

    def permite(self, metodo: str, path: str) -> bool:
        """Si esta clave puede `metodo` sobre `path`. Una completa puede todo
        (lo demás lo decide la app); una con permisos, solo su lista blanca."""
        if self.permisos is None:
            return True
        return any(
            metodo == m and regla.fullmatch(path) is not None
            for permiso in self.permisos
            for m, regla in _REGLAS_POR_PERMISO.get(permiso, ())
        )


class RegistroIntegradores:
    """Integradores y sus claves (hasheadas), cargados al arrancar.

    Las claves se recargan solo al reiniciar: rotar o revocar es editar el
    archivo y reiniciar el proxy."""

    def __init__(
        self, integradores: Iterable[Integrador], claves: Mapping[str, _ClaveGuardada]
    ) -> None:
        self._integradores: dict[str, Integrador] = {i.id: i for i in integradores}
        self._claves: dict[str, _ClaveGuardada] = dict(claves)

    @classmethod
    def vacio(cls) -> RegistroIntegradores:
        """Sin integradores: no hay oyente público y `/v1` protegido da 401."""
        return cls((), {})

    @classmethod
    def desde_archivo(cls, path: Path, env: Mapping[str, str]) -> RegistroIntegradores:
        """Lee `GATEWAY_CLAVES_ARCHIVO`. Cualquier problema es `ConfigInvalida`
        (el proxy no arranca): una configuración a medias podría dejar pasar a
        quien no corresponde o dejar afuera a todos sin aviso."""
        path = Path(path)
        try:
            texto = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigInvalida(f"no se pudo leer el archivo de claves {path}: {e.strerror}") from e
        try:
            datos = json.loads(texto)
        except json.JSONDecodeError as e:
            raise ConfigInvalida(f"el archivo de claves {path} no es JSON válido (línea {e.lineno})") from e
        _avisar_permisos(path)
        return cls.desde_dict(datos, env)

    @classmethod
    def desde_dict(cls, datos: dict[str, Any], env: Mapping[str, str]) -> RegistroIntegradores:
        """Valida el formato de §1.8 y arma el registro.

        Que los comercios existan no se chequea acá (no conoce el registro de
        comercios): lo cruza `__main__`."""
        if not isinstance(datos, dict):
            raise ConfigInvalida("el archivo de claves tiene que ser un objeto JSON")
        _sin_claves_desconocidas(datos, _CLAVES_ARCHIVO, "el archivo de claves")
        lista = datos.get("integradores")
        if not isinstance(lista, list):
            raise ConfigInvalida("falta la lista 'integradores'")

        integradores: list[Integrador] = []
        claves: dict[str, _ClaveGuardada] = {}
        for n, item in enumerate(lista):
            integrador, suyas = _integrador_desde_dict(item, n, env)
            if any(i.id == integrador.id for i in integradores):
                raise ConfigInvalida(f"integrador repetido: {integrador.id}")
            for kid, guardada in suyas.items():
                if kid in claves:
                    # Único en todo el archivo: el kid es la llave de búsqueda.
                    raise ConfigInvalida(f"kid repetido: {kid}")
                claves[kid] = guardada
            integradores.append(integrador)
        return cls(integradores, claves)

    def hay_claves(self) -> bool:
        """Hay al menos una clave activa (condición para el oyente público)."""
        return any(c.activa for c in self._claves.values())

    def autenticar(self, clave: str | None) -> Integrador | None:
        """El integrador dueño de `clave`, o None (sin mirar los permisos: el
        middleware usa `autenticar_clave`)."""
        autenticada = self.autenticar_clave(clave)
        return autenticada.integrador if autenticada is not None else None

    def autenticar_clave(self, clave: str | None) -> ClaveAutenticada | None:
        """El integrador dueño de `clave` y sus permisos, o None. Siempre
        calcula un hash y lo compara contra uno guardado (o el ficticio), sea
        cual sea el motivo del rechazo y sea completa o de lectura."""
        kid = parsear_kid(clave) if isinstance(clave, str) else None
        recibido = hash_clave(clave) if isinstance(clave, str) else _HASH_FICTICIO
        guardada = self._claves.get(kid) if kid is not None else None
        esperado = guardada.sha256 if guardada is not None else _HASH_FICTICIO
        coincide = hmac.compare_digest(recibido.encode("ascii"), esperado.encode("ascii"))
        if kid is None or guardada is None or not coincide or not guardada.activa:
            return None
        integrador = self._integradores.get(guardada.integrador_id)
        if integrador is None:
            return None
        return ClaveAutenticada(integrador=integrador, permisos=guardada.permisos)

    def get(self, integrador_id: str) -> Integrador | None:
        return self._integradores.get(integrador_id)

    def todos(self) -> list[Integrador]:
        return list(self._integradores.values())


def _sin_claves_desconocidas(datos: dict[str, Any], permitidas: frozenset[str], donde: str) -> None:
    desconocidas = sorted(str(k) for k in datos if k not in permitidas)
    if desconocidas:
        # Una clave mal escrita ("activo", "webhok") se ignoraría en silencio
        # y cambiaría lo que el operador cree que configuró.
        raise ConfigInvalida(f"{donde}: claves desconocidas {desconocidas}")


def _integrador_desde_dict(
    item: Any, n: int, env: Mapping[str, str]
) -> tuple[Integrador, dict[str, _ClaveGuardada]]:
    donde = f"integradores[{n}]"
    if not isinstance(item, dict):
        raise ConfigInvalida(f"{donde} tiene que ser un objeto")
    _sin_claves_desconocidas(item, _CLAVES_INTEGRADOR, donde)

    integrador_id = item.get("id")
    if not es_id(ID_SLUG_RE, integrador_id):
        raise ConfigInvalida(f"{donde}: 'id' inválido (minúsculas, dígitos, '_' o '-', hasta 40)")
    donde = f"integrador {integrador_id}"

    nombre = item.get("nombre", integrador_id)
    if not isinstance(nombre, str) or not nombre.strip():
        raise ConfigInvalida(f"{donde}: 'nombre' tiene que ser un texto no vacío")

    comercios_raw = item.get("comercios", [])
    if not isinstance(comercios_raw, list) or not all(es_id(ID_SLUG_RE, c) for c in comercios_raw):
        raise ConfigInvalida(f"{donde}: 'comercios' tiene que ser una lista de ids de comercio")
    comercios = tuple(dict.fromkeys(comercios_raw))  # sin repetidos, en orden

    maximo = item.get("max_compras_activas", 1000)
    if isinstance(maximo, bool) or not isinstance(maximo, int) or maximo <= 0:
        raise ConfigInvalida(f"{donde}: 'max_compras_activas' tiene que ser un entero > 0")

    webhook = _webhook_desde_dict(item.get("webhook"), donde, env)

    claves_raw = item.get("claves", [])
    if not isinstance(claves_raw, list):
        raise ConfigInvalida(f"{donde}: 'claves' tiene que ser una lista")
    claves: dict[str, _ClaveGuardada] = {}
    for m, c in enumerate(claves_raw):
        kid, guardada = _clave_desde_dict(c, f"{donde}, claves[{m}]", integrador_id)
        if kid in claves:
            raise ConfigInvalida(f"kid repetido: {kid}")
        claves[kid] = guardada

    integrador = Integrador(
        id=integrador_id,
        nombre=nombre.strip(),
        comercios=comercios,
        webhook=webhook,
        max_compras_activas=maximo,
    )
    return integrador, claves


def _clave_desde_dict(c: Any, donde: str, integrador_id: str) -> tuple[str, _ClaveGuardada]:
    if not isinstance(c, dict):
        raise ConfigInvalida(f"{donde} tiene que ser un objeto")
    _sin_claves_desconocidas(c, _CLAVES_CLAVE, donde)
    kid = c.get("kid")
    if not es_id(KID_RE, kid):
        raise ConfigInvalida(f"{donde}: 'kid' tiene que ser 12 caracteres hex en minúscula")
    sha = c.get("sha256")
    if not isinstance(sha, str) or _SHA256_RE.fullmatch(sha) is None:
        raise ConfigInvalida(f"{donde}: 'sha256' tiene que ser 64 caracteres hex en minúscula")
    creada = c.get("creada")
    if creada is not None and not isinstance(creada, str):
        raise ConfigInvalida(f"{donde}: 'creada' tiene que ser texto")
    activa = c.get("activa", True)
    if not isinstance(activa, bool):
        raise ConfigInvalida(f"{donde}: 'activa' tiene que ser true o false")
    permisos = _permisos_desde_dict(c, donde)
    return kid, _ClaveGuardada(
        sha256=sha, activa=activa, integrador_id=integrador_id, permisos=permisos
    )


def _permisos_desde_dict(c: dict[str, Any], donde: str) -> frozenset[str] | None:
    """Sin `permisos`, None (clave completa: los archivos de antes siguen
    igual). Con el campo, una lista no vacía de permisos conocidos: un valor
    mal escrito o una lista vacía no se interpretan (¿ninguno o todos?)."""
    if "permisos" not in c:
        return None
    permisos = c["permisos"]
    validos = isinstance(permisos, list) and bool(permisos) and all(
        isinstance(p, str) and p in PERMISOS_CONOCIDOS for p in permisos
    )
    if not validos:
        raise ConfigInvalida(
            f"{donde}: 'permisos' tiene que ser una lista con valores de "
            f"{sorted(PERMISOS_CONOCIDOS)} (sin el campo, la clave es completa)"
        )
    return frozenset(permisos)


def _webhook_desde_dict(w: Any, donde: str, env: Mapping[str, str]) -> ConfigWebhook | None:
    """El secreto no va en el archivo: se nombra la variable de entorno que lo
    tiene (`secreto_env`). La URL la valida `__main__` con la regla de SSRF de
    los webhooks; acá solo el tipo."""
    if w is None:
        return None
    if not isinstance(w, dict):
        raise ConfigInvalida(f"{donde}: 'webhook' tiene que ser un objeto")
    _sin_claves_desconocidas(w, _CLAVES_WEBHOOK, f"{donde}, webhook")
    url = w.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigInvalida(f"{donde}: 'webhook.url' tiene que ser un texto no vacío")
    nombre_env = w.get("secreto_env")
    if not isinstance(nombre_env, str) or not nombre_env:
        raise ConfigInvalida(f"{donde}: falta 'webhook.secreto_env'")
    secreto = env.get(nombre_env)
    if not secreto:
        raise ConfigInvalida(f"{donde}: la variable {nombre_env} no está en el entorno")
    if len(secreto) < MIN_SECRETO_WEBHOOK:
        # El valor nunca se muestra; solo el nombre de la variable.
        raise ConfigInvalida(
            f"{donde}: el secreto de {nombre_env} tiene menos de {MIN_SECRETO_WEBHOOK} caracteres"
        )
    return ConfigWebhook(url=url.strip(), secreto=secreto.encode("utf-8"))


def _avisar_permisos(path: Path) -> None:
    """En POSIX, avisa si el grupo u otros pueden leer el archivo. No frena el
    arranque: el archivo solo tiene hashes, pero también los ids, comercios y
    URLs de los integradores."""
    if os.name != "posix":
        return
    try:
        modo = path.stat().st_mode
    except OSError:
        return
    if modo & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "El archivo de claves %s lo pueden leer otros usuarios (modo %o); "
            "se recomienda chmod 600.", path, stat.S_IMODE(modo),
        )


# -- middleware ---------------------------------------------------------------
def oyente_de(scope: Scope, puertos_publicos: frozenset[int]) -> Oyente:
    """"publico" si la conexión entró por un puerto del oyente público. Sin
    `scope["server"]` (o sin puerto) es "local": así anda hoy la demo y los
    tests que no dicen puerto."""
    server = scope.get("server")
    if not server or len(server) < 2 or server[1] is None:
        return "local"
    return "publico" if server[1] in puertos_publicos else "local"


def _bajo(path: str, prefijo: str) -> bool:
    return path == prefijo or path.startswith(prefijo + "/")


def _error(status: int, codigo: str, mensaje: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(ErrorApi(status, codigo, mensaje).a_json(), status_code=status, headers=headers)


def _no_autenticado() -> JSONResponse:
    return _error(
        401, "no_autenticado", "Falta la clave de API (Authorization: Bearer psg_…).",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _clave_invalida() -> JSONResponse:
    return _error(
        401, "clave_invalida", "La clave de API no es válida.",
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def _no_encontrado() -> JSONResponse:
    return _error(404, "no_encontrado", "No existe.")


def _solo_lectura() -> JSONResponse:
    # La forma de /v1 también en /a2a, como el 401 de la clave (RFC 6750:
    # la clave vale pero no alcanza).
    return _error(
        403, "solo_lectura",
        "La clave es de solo lectura: sirve para consultar (GET en /v1), "
        "no para comprar, contestar ni aprobar.",
        headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
    )


class AutenticacionGateway:
    """Reglas por ruta y oyente (diseño §1.2). Deja en `scope["state"]`:

    - `oyente`: "local" | "publico";
    - `integrador`: el `Integrador` autenticado, o None si el pedido es
      anónimo (en el oyente local fuera de `/v1` protegido, o una de las
      `rutas_abiertas`).

    El oyente sale del puerto local de la conexión (`puertos_publicos`). Con
    `todo_publico=True` todo pedido es del oyente público, sin mirar el
    puerto: en un despliegue serverless (Vercel) no hay puerto propio ni
    oyente local, y lo que llega viene de afuera.

    `rutas_abiertas`: rutas exactas que pasan sin clave en cualquier oyente
    (la consola web: archivos estáticos, sin estado; ver `proxy.consola`).
    Nunca abre nada bajo `/guard`, `/v1` ni `/a2a`.

    Una clave con permisos (de solo lectura) que autenticó solo pasa por su
    lista blanca (`RUTAS_LECTURA`); en el resto de `/v1` y en `/a2a/*`, 403
    `solo_lectura` sin llegar a la app (en el local tampoco se degrada a
    anónimo). Primero se autentica: una clave que no vale es 401 aunque sea
    de lectura.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        registro: RegistroIntegradores,
        puertos_publicos: Iterable[int] = (),
        todo_publico: bool = False,
        rutas_abiertas: Iterable[str] = (),
    ) -> None:
        abiertas = frozenset(rutas_abiertas)
        protegidas = [
            r for r in abiertas
            if any(_bajo(r, p) for p in (_PREFIJO_GUARD, _PREFIJO_V1, _PREFIJO_A2A))
        ]
        if protegidas:
            raise ValueError(f"rutas_abiertas no puede abrir rutas protegidas: {sorted(protegidas)}")
        self.app = app
        self.registro = registro
        self.puertos_publicos = frozenset(puertos_publicos)
        self.todo_publico = bool(todo_publico)
        self.rutas_abiertas = abiertas

    def oyente(self, scope: Scope) -> Oyente:
        """"publico" siempre con `todo_publico`; si no, por el puerto."""
        if self.todo_publico:
            return "publico"
        return oyente_de(scope, self.puertos_publicos)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        oyente = self.oyente(scope)
        estado = scope.setdefault("state", {})
        estado["oyente"] = oyente
        estado["integrador"] = None

        resultado = self._resolver(scope, oyente)
        if isinstance(resultado, JSONResponse):
            await resultado(scope, receive, send)
            return
        estado["integrador"] = resultado
        await self.app(scope, receive, send)

    def _resolver(self, scope: Scope, oyente: Oyente) -> Integrador | None | JSONResponse:
        """El integrador (o None si pasa como anónimo), o la respuesta de
        rechazo."""
        path: str = scope["path"]
        publico = oyente == "publico"

        if _bajo(path, _PREFIJO_GUARD):
            # En el local la protege ControlLocal, igual que hoy.
            return _no_encontrado() if publico else None
        if path in RUTAS_V1_PUBLICAS or path in self.rutas_abiertas:
            return None
        if _bajo(path, _PREFIJO_V1):
            return self._con_permiso(scope, self._exigir_clave(scope))
        if _bajo(path, _PREFIJO_A2A):
            if publico:
                return self._con_permiso(scope, self._exigir_clave(scope))
            return self._con_permiso(scope, self._clave_opcional(scope))
        # El público es una lista cerrada: lo que no es /a2a ni /v1 no existe.
        return _no_encontrado() if publico else None

    @staticmethod
    def _con_permiso(
        scope: Scope, resultado: ClaveAutenticada | None | JSONResponse
    ) -> Integrador | None | JSONResponse:
        """El integrador si la clave puede este método y ruta; si no, 403. Lo
        que decide es el permiso guardado de la clave que autenticó: ningún
        header ni parámetro del pedido lo cambia."""
        if not isinstance(resultado, ClaveAutenticada):
            return resultado
        if not resultado.permite(scope["method"], scope["path"]):
            return _solo_lectura()
        return resultado.integrador

    def _claves_presentes(self, scope: Scope) -> tuple[list[str], str | None]:
        """(todos los valores de Authorization, la clave psg_ si hay una sola)."""
        valores = Headers(scope=scope).getlist("authorization")
        clave = extraer_bearer(valores[0]) if len(valores) == 1 else None
        return valores, clave

    def _ambiguo(self, valores: list[str]) -> bool:
        """Más de un `Authorization` con alguna clave nuestra entre ellos. Se
        rechaza: si pasara como anónimo, el reenvío al comercio llevaría la
        clave del integrador (en el tráfico anónimo `authorization` no se
        saca)."""
        return len(valores) > 1 and any(
            extraer_bearer(v) is not None or _parece_clave(v) for v in valores
        )

    def _exigir_clave(self, scope: Scope) -> ClaveAutenticada | JSONResponse:
        valores, clave = self._claves_presentes(scope)
        if self._ambiguo(valores):
            return _clave_invalida()
        if clave is None:
            return _clave_invalida() if any(map(_parece_clave, valores)) else _no_autenticado()
        autenticada = self.registro.autenticar_clave(clave)
        return autenticada if autenticada is not None else _clave_invalida()

    def _clave_opcional(self, scope: Scope) -> ClaveAutenticada | None | JSONResponse:
        """Oyente local, `/a2a/*`: sin clave nuestra es anónimo (la demo); con
        `Bearer psg_…` tiene que ser válida, nunca se degrada a anónimo.

        Tampoco una clave mal presentada (`Token psg_…`, sin esquema, con un
        tabulador): pasaría como anónimo y el reenvío al comercio la llevaría
        tal cual (en el tráfico anónimo `authorization` no se saca)."""
        valores, clave = self._claves_presentes(scope)
        if self._ambiguo(valores):
            return _clave_invalida()
        if clave is None:
            return _clave_invalida() if any(map(_parece_clave, valores)) else None
        autenticada = self.registro.autenticar_clave(clave)
        return autenticada if autenticada is not None else _clave_invalida()


def _parece_clave(valor: str) -> bool:
    return _PARECE_CLAVE_RE.search(valor) is not None


# -- CLI ----------------------------------------------------------------------
def _cmd_nueva(args: argparse.Namespace) -> int:
    if not es_id(ID_SLUG_RE, args.integrador):
        print(f"id de integrador inválido: {args.integrador!r}", file=sys.stderr)
        return 2
    clave, kid, sha = generar_clave()
    objeto: dict[str, Any] = {
        "kid": kid, "sha256": sha, "creada": date.today().isoformat(), "activa": True,
    }
    if args.lectura:
        objeto["permisos"] = [PERMISO_LECTURA]
    tipo = "de solo lectura (GET en /v1; no sirve para comprar)" if args.lectura else "nueva"
    # stdout: la clave y el objeto, una línea cada uno (se puede parsear).
    # stderr: las indicaciones para el operador.
    print(
        f"Clave {tipo} para el integrador {args.integrador}. Se muestra una sola vez: "
        "entregala por un canal seguro. En el archivo de claves va solo el objeto, "
        f"dentro de 'claves' del integrador {args.integrador}.",
        file=sys.stderr,
    )
    print(clave)
    print(json.dumps(objeto, ensure_ascii=False))
    return 0


def _cmd_secreto_webhook(_args: argparse.Namespace) -> int:
    print(
        "Secreto de webhook: ponelo en una variable de entorno y nombrala en "
        "'webhook.secreto_env'. El integrador lo usa para verificar la firma.",
        file=sys.stderr,
    )
    print(PREFIJO_SECRETO_WEBHOOK + secrets.token_urlsafe(32))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m proxy.claves", description="Claves de API y secretos de webhook del gateway."
    )
    sub = parser.add_subparsers(dest="comando", required=True)
    nueva = sub.add_parser("nueva", help="genera una clave de API para un integrador")
    nueva.add_argument("--integrador", required=True, help="id del integrador (como en el archivo)")
    nueva.add_argument("--lectura", action="store_true",
                       help='clave de solo lectura ("permisos": ["lectura"]): GET en /v1, para el panel')
    nueva.set_defaults(func=_cmd_nueva)
    secreto = sub.add_parser("secreto-webhook", help="genera un secreto whsec_ para firmar webhooks")
    secreto.set_defaults(func=_cmd_secreto_webhook)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
