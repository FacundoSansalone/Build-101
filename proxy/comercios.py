"""Registro de comercios del gateway: a qué Merchant Agent va cada ruta.

Hoy el proxy habla con un solo comercio (`MERCHANT_UPSTREAM_URL`). El gateway
de producción atiende varios, cada uno en `/a2a/m/{id}`, pero **sin URL
libre**: el upstream sale siempre de este registro, que se carga de
`GATEWAY_COMERCIOS_ARCHIVO` al arrancar (o, sin archivo, es el comercio de
siempre). Así nadie puede usar el gateway para hablar con un servidor que el
operador no aprobó.

Cada comercio declara los `merchant.id` con los que firma sus checkouts. Sin
esa lista no se puede atar lo firmado a la ruta: un comercio podría devolver
un checkout firmado por otro y el Guard lo evaluaría con el catálogo y la
política equivocados. Por eso es obligatoria para todo comercio que no sea el
por defecto (el por defecto puede no tenerla, como hoy).

El módulo no lee el entorno ni hace I/O salvo `desde_archivo`; la integración
le pasa los valores.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contracts import Politica
from proxy.config import es_loopback
from proxy.tipos_gateway import (
    ID_SEGURO_RE,
    ID_SLUG_RE,
    PREFIJO_A2A_COMERCIO,
    Comercio,
    ConfigInvalida,
    Integrador,
    es_id,
)

_CLAVES_RAIZ = frozenset({"por_defecto", "comercios"})
_CLAVES_COMERCIO = frozenset(
    {"id", "nombre", "a2a_url", "catalogo_url", "merchant_ids", "respaldo_fixture"}
)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def validar_url_upstream(url: str) -> str:
    """Valida la URL de un upstream (Merchant Agent o catálogo) del archivo.

    `http` solo vale contra loopback: fuera de la máquina, el tráfico al
    comercio lleva mandatos y checkouts firmados y tiene que ir cifrado.
    Userinfo (`user:pass@`) se rechaza porque las credenciales terminarían en
    logs y en `/guard/state`; fragment y query, porque el proxy arma URLs
    pegando `/{resto}` (Agent Card) y quedarían mal. Devuelve la URL sin la
    `/` final, que es como la usa el resto del proxy.

    Los mensajes nombran a lo sumo el host, nunca la URL entera: la
    configuración puede venir de una variable de entorno
    (`GATEWAY_COMERCIOS_JSON`) que termina en el log, y el userinfo o la
    query pueden traer credenciales.
    """
    if not isinstance(url, str) or not url.strip():
        raise ConfigInvalida("la URL del comercio tiene que ser un texto no vacío")
    url = url.strip()
    try:
        partes = urlsplit(url)
        host = partes.hostname
        partes.port  # noqa: B018 — valida el puerto (ValueError si no es número)
    except ValueError:
        raise ConfigInvalida("una URL del comercio no es válida (host o puerto)") from None
    if partes.scheme not in ("http", "https"):
        donde = f"de {host!r}" if host else "del comercio"
        raise ConfigInvalida(f"la URL {donde} tiene que ser http(s)")
    if not host:
        raise ConfigInvalida("una URL del comercio no tiene host")
    if partes.username is not None or partes.password is not None or "@" in partes.netloc:
        raise ConfigInvalida(f"la URL de {host!r} no puede llevar usuario ni clave")
    if partes.fragment or "#" in url:
        raise ConfigInvalida(f"la URL de {host!r} no puede llevar fragment")
    if partes.query or "?" in url:
        raise ConfigInvalida(f"la URL de {host!r} no puede llevar query")
    if partes.scheme == "http" and not es_loopback(host):
        raise ConfigInvalida(
            f"la URL de {host!r} usa http fuera de loopback: tiene que ser https"
        )
    return url.rstrip("/")


def base_url(url: str) -> str:
    """`scheme://host[:port]` de una URL (sin path ni userinfo).

    La usa la integración para armar la URL pública de `/a2a/m/{id}` en el
    oyente local a partir de `PROXY_PUBLIC_URL`, que apunta a la ruta vieja.
    """
    try:
        partes = urlsplit(url.strip())
        host = partes.hostname
        puerto = partes.port
    except (AttributeError, ValueError) as exc:
        raise ConfigInvalida(f"URL inválida: {url!r}") from exc
    if not partes.scheme or not host:
        raise ConfigInvalida(f"URL sin esquema o sin host: {url!r}")
    # hostname viene sin corchetes y en minúscula: se vuelven a poner para IPv6.
    host_url = f"[{host}]" if ":" in host else host
    return f"{partes.scheme}://{host_url}" + (f":{puerto}" if puerto is not None else "")


def url_publica_comercio(base: str, comercio_id: str) -> str:
    """URL pública de un comercio: `{base}/a2a/m/{id}`.

    El id se valida acá también porque la URL termina en la Agent Card y en
    las respuestas de `/v1`: un id fuera del slug no tiene que llegar nunca.
    """
    if not es_id(ID_SLUG_RE, comercio_id):
        raise ValueError(f"id de comercio inválido: {comercio_id!r}")
    return f"{base.rstrip('/')}{PREFIJO_A2A_COMERCIO}{comercio_id}"


# ---------------------------------------------------------------------------
# merchant.id firmado y política
# ---------------------------------------------------------------------------


def merchant_id_coincide(comercio: Comercio, firmado: str | None) -> bool:
    """¿El `merchant.id` firmado en el checkout es de este comercio?

    Sin lista (`merchant_ids == ()`, el comercio por defecto de siempre) no
    hay chequeo extra: el Guard sigue con su política, como hoy. Con lista,
    un checkout sin `merchant.id` firmado no coincide (fail-closed).
    """
    if not comercio.merchant_ids:
        return True
    return isinstance(firmado, str) and firmado in comercio.merchant_ids


def politica_de(comercios: Iterable[Comercio]) -> Politica | None:
    """Política del Guard con los `merchant.id` de estos comercios.

    Unión en orden y sin repetidos. Si ninguno declara `merchant_ids` queda
    vacía y se devuelve None: el Guard usa su `Politica()` por defecto, como
    hoy (una lista vacía bloquearía todo checkout).
    """
    ids: list[str] = []
    for comercio in comercios:
        for merchant_id in comercio.merchant_ids:
            if merchant_id not in ids:
                ids.append(merchant_id)
    return Politica(comercios_aprobados=ids) if ids else None


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------


def comercio_por_defecto(
    *, id: str, a2a_url: str, catalogo_url: str | None, respaldo_fixture: bool
) -> Comercio:
    """El comercio de siempre, armado desde la configuración actual.

    No pasa por `validar_url_upstream`: sin archivo de comercios todo tiene
    que andar igual que hoy, y hoy `MERCHANT_UPSTREAM_URL` y `CATALOGO_URL`
    no tienen esa regla (los tests usan hosts falsos por http). Sin
    `merchant_ids`: no se agrega ningún chequeo nuevo.
    """
    if not es_id(ID_SLUG_RE, id):
        raise ConfigInvalida(f"id de comercio por defecto inválido: {id!r}")
    return Comercio(
        id=id,
        nombre=id,
        a2a_url=a2a_url,
        catalogo_url=catalogo_url or None,
        merchant_ids=(),
        respaldo_fixture=bool(respaldo_fixture),
    )


def _comercio_desde_dict(datos: Any, por_defecto: str) -> Comercio:
    if not isinstance(datos, dict):
        raise ConfigInvalida("cada comercio tiene que ser un objeto")
    desconocidas = set(datos) - _CLAVES_COMERCIO
    if desconocidas:
        # Una clave mal escrita (`merchant_id`) dejaría sin chequeo lo firmado.
        raise ConfigInvalida(f"claves desconocidas en un comercio: {sorted(desconocidas)}")

    comercio_id = datos.get("id")
    if not es_id(ID_SLUG_RE, comercio_id):
        raise ConfigInvalida(f"id de comercio inválido: {comercio_id!r}")

    nombre = datos.get("nombre", comercio_id)
    if not isinstance(nombre, str) or not nombre.strip():
        raise ConfigInvalida(f"comercio {comercio_id}: nombre inválido")

    if "a2a_url" not in datos:
        raise ConfigInvalida(f"comercio {comercio_id}: falta a2a_url")
    a2a_url = validar_url_upstream(datos["a2a_url"])

    catalogo = datos.get("catalogo_url")
    catalogo_url = None if catalogo is None else validar_url_upstream(catalogo)

    crudos = datos.get("merchant_ids", [])
    if not isinstance(crudos, list):
        raise ConfigInvalida(f"comercio {comercio_id}: merchant_ids tiene que ser una lista")
    merchant_ids: list[str] = []
    for merchant_id in crudos:
        if not es_id(ID_SEGURO_RE, merchant_id):
            raise ConfigInvalida(
                f"comercio {comercio_id}: merchant_id inválido: {merchant_id!r}"
            )
        if merchant_id not in merchant_ids:
            merchant_ids.append(merchant_id)
    if not merchant_ids and comercio_id != por_defecto:
        raise ConfigInvalida(
            f"comercio {comercio_id}: merchant_ids es obligatorio (sin él no se "
            "puede atar el checkout firmado a la ruta)"
        )

    respaldo = datos.get("respaldo_fixture", False)
    if not isinstance(respaldo, bool):
        raise ConfigInvalida(f"comercio {comercio_id}: respaldo_fixture tiene que ser bool")

    return Comercio(
        id=comercio_id,
        nombre=nombre.strip(),
        a2a_url=a2a_url,
        catalogo_url=catalogo_url,
        merchant_ids=tuple(merchant_ids),
        respaldo_fixture=respaldo,
    )


class RegistroComercios:
    """Comercios que el gateway acepta, por id de ruta. Inmutable."""

    def __init__(self, comercios: Iterable[Comercio], por_defecto: str) -> None:
        self._comercios: dict[str, Comercio] = {}
        for comercio in comercios:
            if comercio.id in self._comercios:
                raise ConfigInvalida(f"id de comercio repetido: {comercio.id!r}")
            self._comercios[comercio.id] = comercio
        if por_defecto not in self._comercios:
            raise ConfigInvalida(f"por_defecto {por_defecto!r} no está en la lista")
        self._por_defecto = self._comercios[por_defecto]

    @classmethod
    def desde_archivo(cls, path: Path) -> RegistroComercios:
        """Lee `GATEWAY_COMERCIOS_ARCHIVO`. Cualquier falla es `ConfigInvalida`
        (el proxy no arranca): sin registro no se sabe a dónde reenviar."""
        try:
            texto = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigInvalida(f"no se pudo leer el archivo de comercios {path}: {exc}") from exc
        try:
            datos = json.loads(texto)
        except json.JSONDecodeError as exc:
            raise ConfigInvalida(f"el archivo de comercios {path} no es JSON válido: {exc}") from exc
        return cls.desde_dict(datos)

    @classmethod
    def desde_dict(cls, datos: dict[str, Any]) -> RegistroComercios:
        if not isinstance(datos, dict):
            raise ConfigInvalida("el archivo de comercios tiene que ser un objeto")
        desconocidas = set(datos) - _CLAVES_RAIZ
        if desconocidas:
            raise ConfigInvalida(f"claves desconocidas en el archivo de comercios: {sorted(desconocidas)}")
        por_defecto = datos.get("por_defecto")
        if not es_id(ID_SLUG_RE, por_defecto):
            raise ConfigInvalida(f"por_defecto inválido: {por_defecto!r}")
        lista = datos.get("comercios")
        if not isinstance(lista, list) or not lista:
            raise ConfigInvalida("comercios tiene que ser una lista no vacía")
        return cls((_comercio_desde_dict(c, por_defecto) for c in lista), por_defecto)

    @classmethod
    def solo(cls, por_defecto: Comercio) -> RegistroComercios:
        """Sin archivo: un único comercio, el de siempre."""
        return cls([por_defecto], por_defecto.id)

    @property
    def por_defecto(self) -> Comercio:
        return self._por_defecto

    def get(self, comercio_id: str | None) -> Comercio | None:
        """None → el por defecto (ruta vieja). Un id que no es slug (`../x`)
        o que no está registrado → None: la ruta da 404, nunca otro upstream."""
        if comercio_id is None:
            return self._por_defecto
        if not es_id(ID_SLUG_RE, comercio_id):
            return None
        return self._comercios.get(comercio_id)

    def todos(self) -> list[Comercio]:
        return list(self._comercios.values())

    def permitidos(self, integrador: Integrador) -> list[Comercio]:
        """Comercios que puede usar un integrador, en el orden de su config.

        Lista vacía = solo el por defecto. Los ids que no existen se ignoran
        acá (al arrancar, `ids_inexistentes` ya hizo fallar al proxy): si no
        queda ninguno, el integrador no puede usar ninguno.
        """
        if not integrador.comercios:
            return [self._por_defecto]
        permitidos: list[Comercio] = []
        for comercio_id in integrador.comercios:
            comercio = self.get(comercio_id)
            if comercio is not None and comercio not in permitidos:
                permitidos.append(comercio)
        return permitidos

    def ids_inexistentes(self, integradores: Iterable[Integrador]) -> list[str]:
        """Ids de comercio que nombran los integradores y no están registrados
        (ordenados y sin repetidos). `__main__` no arranca si hay alguno."""
        faltan = {
            comercio_id
            for integrador in integradores
            for comercio_id in integrador.comercios
            if self.get(comercio_id) is None
        }
        return sorted(faltan)
