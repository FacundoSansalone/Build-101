"""PostgREST falso con tablas en memoria (`httpx.MockTransport`).

Imita lo que usa `EstadoSupabase` (proxy/estado.py) de PostgREST y de las
tablas de `proxy/sql/003_gateway.sql` + `004_serverless.sql`:

- filtros `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `is`, `in`, `not.<op>` y
  `or=(…)` (con valores entre comillas), `order`, `limit` y `select`;
- `Prefer`: `return=representation|minimal`, `resolution=merge-duplicates|
  ignore-duplicates` (con `on_conflict`) y `count=exact` (`Content-Range`);
- claves primarias, índices únicos (también parciales) y claves foráneas, con
  el 409 y el código de Postgres que da PostgREST (`23505`, `23503`); los
  `check` de 004 (`23514`); columnas desconocidas (`42703`); `orden` de
  `gw_eventos` como `generated always as identity` (`428C9` si se manda);
- el trigger `gw_compras_proteger` (escrito acá aparte del de `estado.py`,
  para que el test compare dos implementaciones de la misma regla).

Cada pedido se procesa entero sin ceder el loop, como una sentencia en la
base. Varios `EstadoSupabase` sobre la misma `BaseFalsa` son varias
instancias del proxy contra la misma base (lote C: tests multi-instancia).
`interceptar` deja meter una respuesta propia (una caída, una carrera).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

TS, INT, BOOL, JSON, TEXT = "ts", "int", "bool", "json", "text"
TERMINALES = ("bloqueada", "vencida")


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Unica:
    columnas: tuple[str, ...]
    donde: Callable[[dict[str, Any]], bool] | None = None   # índice parcial


@dataclass
class Tabla:
    nombre: str
    tipos: dict[str, str]                                   # todas las columnas
    pk: tuple[str, ...]
    unicas: list[Unica] = field(default_factory=list)
    fks: list[tuple[str, str, str]] = field(default_factory=list)   # (col, tabla, col_ref)
    defaults: dict[str, Callable[[], Any]] = field(default_factory=dict)
    identity: str | None = None
    checks: list[Callable[[dict[str, Any]], bool]] = field(default_factory=list)
    antes_de_actualizar: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = None
    filas: list[dict[str, Any]] = field(default_factory=list)
    siguiente: int = 1


def _proteger_compra(vieja: dict[str, Any], nueva: dict[str, Any]) -> dict[str, Any] | None:
    """`gw_compras_proteger` (004): return null descarta la escritura."""
    if _a_fecha(nueva["actualizada"]) < _a_fecha(vieja["actualizada"]):
        return None
    if vieja["estado"] in TERMINALES and nueva["estado"] != vieja["estado"]:
        return None
    return nueva


def tablas_gateway() -> dict[str, Tabla]:
    """Las tablas de 003 + 004, con sus restricciones."""
    compras = Tabla(
        "gw_compras",
        {"id": TEXT, "integrador_id": TEXT, "comercio_id": TEXT, "referencia": TEXT,
         "estado": TEXT, "creada": TS, "actualizada": TS, "datos": JSON, "espera_id": TEXT},
        pk=("id",),
        antes_de_actualizar=_proteger_compra,
    )
    contextos = Tabla(
        "gw_contextos",
        {"contexto_id": TEXT, "compra_id": TEXT, "comercio_id": TEXT, "creado": TS},
        pk=("contexto_id",),
        fks=[("compra_id", "gw_compras", "id")],
        defaults={"creado": _ahora_iso},
    )
    ofertas = Tabla(
        "gw_ofertas",
        {"contexto_id": TEXT, "comercio_id": TEXT, "carts": JSON, "offer_details": JSON,
         "escenario": TEXT, "registradas": TS},
        pk=("contexto_id",),
    )
    veredictos = Tabla(
        "gw_ultimos_veredictos",
        {"contexto_id": TEXT, "cart_id": TEXT, "status": TEXT, "centavos": INT,
         "moneda": TEXT, "ts": TS},
        pk=("contexto_id", "cart_id"),
    )
    aprobaciones = Tabla(
        "gw_aprobaciones",
        {"id": TEXT, "contexto_id": TEXT, "cart_id": TEXT, "centavos": INT, "moneda": TEXT,
         "estado": TEXT, "creada": TS, "usada": TS, "task_id": TEXT, "integrador_id": TEXT,
         "compra_id": TEXT},
        pk=("id",),
        unicas=[Unica(("contexto_id", "cart_id", "centavos", "moneda"),
                      lambda f: f["estado"] == "pendiente")],
    )
    entregas = Tabla(
        "gw_entregas_webhook",
        {"id": TEXT, "integrador_id": TEXT, "tipo": TEXT, "compra_id": TEXT, "intentos": INT,
         "estado": TEXT, "status_http": INT, "error": TEXT, "destino_host": TEXT,
         "creada": TS, "actualizada": TS, "cuerpo": TEXT, "proximo_intento": TS,
         "tomada_hasta": TS},
        pk=("id",),
    )
    esperas = Tabla(
        "gw_esperas",
        {"id": TEXT, "compra_id": TEXT, "tipo": TEXT, "estado": TEXT, "oportunidad_id": TEXT,
         "vence": TS, "respuesta": JSON, "creada": TS, "resuelta": TS},
        pk=("id",),
        unicas=[Unica(("compra_id",), lambda f: f["estado"] == "abierta")],
        fks=[("compra_id", "gw_compras", "id")],
        checks=[
            lambda f: f["tipo"] in ("porque", "decision"),
            lambda f: f["estado"] in ("abierta", "respondida", "vencida"),
            lambda f: (f["tipo"] == "decision") == (f["oportunidad_id"] is not None),
            lambda f: (f["estado"] == "abierta") == (f["resuelta"] is None),
        ],
    )
    eventos = Tabla(
        "gw_eventos",
        {"orden": INT, "id": TEXT, "ts": TS, "integrador_id": TEXT, "compra_id": TEXT,
         "contexto_id": TEXT, "escenario": TEXT, "guard_activo": BOOL, "tipo": TEXT,
         "datos": JSON},
        pk=("orden",),
        unicas=[Unica(("id",))],
        identity="orden",
        defaults={"datos": dict},
    )
    return {t.nombre: t for t in (compras, contextos, ofertas, veredictos, aprobaciones,
                                   entregas, esperas, eventos)}


# -- valores --------------------------------------------------------------------


def _a_fecha(valor: Any) -> datetime:
    fecha = datetime.fromisoformat(valor) if isinstance(valor, str) else valor
    return fecha if fecha.tzinfo is not None else fecha.replace(tzinfo=timezone.utc)


def _normalizar(tipo: str, valor: Any) -> Any:
    """Lo que guarda y devuelve Postgres: fechas en UTC con `+00:00`."""
    if valor is None:
        return None
    if tipo == TS:
        return _a_fecha(valor).astimezone(timezone.utc).isoformat()
    if tipo == JSON:
        return json.loads(json.dumps(valor))
    return valor


def _coercer(tipo: str, texto: str) -> Any:
    if len(texto) >= 2 and texto[0] == texto[-1] == '"':
        texto = texto[1:-1]
    if tipo == TS:
        return _a_fecha(texto)
    if tipo == INT:
        return int(texto)
    if tipo == BOOL:
        return {"true": True, "false": False}[texto]
    return texto


def _comparable(tipo: str, valor: Any) -> Any:
    if valor is None:
        return None
    return _a_fecha(valor) if tipo == TS else valor


class ErrorPg(Exception):
    def __init__(self, status: int, codigo: str) -> None:
        super().__init__(codigo)
        self.status = status
        self.codigo = codigo


# -- filtros ----------------------------------------------------------------------


def _partir(texto: str) -> list[str]:
    """Separa por comas que no están entre comillas ni paréntesis."""
    partes, actual, nivel, comillas = [], "", 0, False
    for c in texto:
        if c == '"':
            comillas = not comillas
        elif not comillas and c == "(":
            nivel += 1
        elif not comillas and c == ")":
            nivel -= 1
        if c == "," and nivel == 0 and not comillas:
            partes.append(actual)
            actual = ""
        else:
            actual += c
    partes.append(actual)
    return partes


def _condicion(tabla: Tabla, columna: str, expresion: str) -> Callable[[dict[str, Any]], bool]:
    if columna not in tabla.tipos:
        raise ErrorPg(400, "42703")
    tipo = tabla.tipos[columna]
    negar = False
    if expresion.startswith("not."):
        negar, expresion = True, expresion[4:]
    op, _, valor = expresion.partition(".")

    def evaluar(fila: dict[str, Any]) -> bool:
        actual = _comparable(tipo, fila.get(columna))
        if op == "is":
            resultado = actual is None if valor == "null" else actual is {"true": True, "false": False}[valor]
        elif op == "in":
            assert valor.startswith("(") and valor.endswith(")"), valor
            opciones = [_coercer(tipo, v) for v in _partir(valor[1:-1])]
            resultado = actual is not None and actual in opciones
        else:
            if actual is None:
                return False  # SQL: comparar con null no es verdadero (ni negado)
            otro = _coercer(tipo, valor)
            resultado = {
                "eq": actual == otro, "neq": actual != otro, "gt": actual > otro,
                "gte": actual >= otro, "lt": actual < otro, "lte": actual <= otro,
            }[op]
        return not resultado if negar else resultado

    return evaluar


def _filtro(tabla: Tabla, params: list[tuple[str, str]]) -> Callable[[dict[str, Any]], bool]:
    condiciones: list[Callable[[dict[str, Any]], bool]] = []
    for clave, valor in params:
        if clave in ("select", "order", "limit", "on_conflict", "offset"):
            continue
        if clave == "or":
            assert valor.startswith("(") and valor.endswith(")"), valor
            alternativas = []
            for parte in _partir(valor[1:-1]):
                columna, _, expresion = parte.partition(".")
                alternativas.append(_condicion(tabla, columna, expresion))
            condiciones.append(lambda f, alts=alternativas: any(a(f) for a in alts))
        else:
            condiciones.append(_condicion(tabla, clave, valor))
    return lambda fila: all(c(fila) for c in condiciones)


def _ordenar(tabla: Tabla, filas: list[dict[str, Any]], orden: str | None) -> list[dict[str, Any]]:
    if not orden:
        return filas
    for parte in reversed(orden.split(",")):
        columna, _, sentido = parte.partition(".")
        if columna not in tabla.tipos:
            raise ErrorPg(400, "42703")
        desc = sentido == "desc"
        tipo = tabla.tipos[columna]
        con = [f for f in filas if f.get(columna) is not None]
        sin = [f for f in filas if f.get(columna) is None]
        con.sort(key=lambda f: _comparable(tipo, f[columna]), reverse=desc)
        filas = (sin + con) if desc else (con + sin)   # nulls first en desc, last en asc
    return filas


def _seleccionar(tabla: Tabla, fila: dict[str, Any], select: str | None) -> dict[str, Any]:
    if not select or select == "*":
        return json.loads(json.dumps(fila))
    columnas = select.split(",")
    for c in columnas:
        if c not in tabla.tipos:
            raise ErrorPg(400, "42703")
    return json.loads(json.dumps({c: fila.get(c) for c in columnas}))


# -- la base ------------------------------------------------------------------------


class BaseFalsa:
    """Las tablas del gateway y el transporte que las sirve."""

    def __init__(self) -> None:
        self.tablas = tablas_gateway()
        self.pedidos: list[httpx.Request] = []
        self.interceptar: Callable[[httpx.Request], httpx.Response | None] | None = None

    def transporte(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def filas(self, tabla: str) -> list[dict[str, Any]]:
        return json.loads(json.dumps(self.tablas[tabla].filas))

    # -- restricciones ----------------------------------------------------------

    def _violaciones(self, tabla: Tabla, fila: dict[str, Any], *, salvo: dict[str, Any] | None) -> None:
        for check in tabla.checks:
            if not check(fila):
                raise ErrorPg(400, "23514")
        for col, otra, ref in tabla.fks:
            if fila.get(col) is not None and not any(
                f.get(ref) == fila[col] for f in self.tablas[otra].filas
            ):
                raise ErrorPg(409, "23503")
        for unica in [Unica(tabla.pk), *tabla.unicas]:
            if unica.donde is not None and not unica.donde(fila):
                continue
            clave = tuple(fila.get(c) for c in unica.columnas)
            if any(v is None for v in clave):
                continue
            for otra_fila in tabla.filas:
                if otra_fila is salvo:
                    continue
                if unica.donde is not None and not unica.donde(otra_fila):
                    continue
                if tuple(otra_fila.get(c) for c in unica.columnas) == clave:
                    raise ErrorPg(409, "23505")

    def _actualizar(self, tabla: Tabla, vieja: dict[str, Any], cambios: dict[str, Any]) -> dict[str, Any] | None:
        nueva = {**vieja, **cambios}
        if tabla.antes_de_actualizar is not None:
            nueva = tabla.antes_de_actualizar(vieja, nueva)
            if nueva is None:
                return None
        self._violaciones(tabla, nueva, salvo=vieja)
        vieja.clear()
        vieja.update(nueva)
        return vieja

    def _limpiar(self, tabla: Tabla, cuerpo: dict[str, Any]) -> dict[str, Any]:
        for columna in cuerpo:
            if columna not in tabla.tipos:
                raise ErrorPg(400, "42703")
        if tabla.identity is not None and tabla.identity in cuerpo:
            raise ErrorPg(400, "428C9")
        return {c: _normalizar(tabla.tipos[c], v) for c, v in cuerpo.items()}

    # -- verbos -------------------------------------------------------------------

    def _post(self, tabla: Tabla, params: dict[str, str], prefer: set[str], cuerpo: Any) -> list[dict[str, Any]]:
        filas_cuerpo = cuerpo if isinstance(cuerpo, list) else [cuerpo]
        conflicto = tuple(params["on_conflict"].split(",")) if "on_conflict" in params else None
        afectadas: list[dict[str, Any]] = []
        for crudo in filas_cuerpo:
            cambios = self._limpiar(tabla, crudo)
            fila = {c: None for c in tabla.tipos}
            for c, d in tabla.defaults.items():
                fila[c] = _normalizar(tabla.tipos[c], d())
            fila.update(cambios)
            if tabla.identity is not None:
                fila[tabla.identity] = tabla.siguiente
                tabla.siguiente += 1
            existente = None
            if conflicto is not None:
                clave = tuple(fila.get(c) for c in conflicto)
                existente = next(
                    (f for f in tabla.filas if tuple(f.get(c) for c in conflicto) == clave), None
                )
            if existente is not None:
                if "resolution=ignore-duplicates" in prefer:
                    continue
                if "resolution=merge-duplicates" in prefer:
                    hecha = self._actualizar(tabla, existente, cambios)
                    if hecha is not None:
                        afectadas.append(hecha)
                    continue
            self._violaciones(tabla, fila, salvo=None)
            tabla.filas.append(fila)
            afectadas.append(fila)
        return afectadas

    def _patch(self, tabla: Tabla, params: list[tuple[str, str]], cuerpo: dict[str, Any]) -> list[dict[str, Any]]:
        cambios = self._limpiar(tabla, cuerpo)
        coincide = _filtro(tabla, params)
        afectadas = []
        for fila in [f for f in tabla.filas if coincide(f)]:
            hecha = self._actualizar(tabla, fila, cambios)
            if hecha is not None:
                afectadas.append(hecha)
        return afectadas

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        if self.interceptar is not None:
            propia = self.interceptar(request)
            if propia is not None:
                return propia
        partes = request.url.path.split("/rest/v1/")
        tabla = self.tablas.get(partes[-1]) if len(partes) == 2 else None
        if tabla is None:
            return httpx.Response(404, json={"code": "42P01"})
        params_lista = list(request.url.params.multi_items())
        params = dict(params_lista)
        prefer = {p.strip() for p in request.headers.get("prefer", "").split(",") if p.strip()}
        representacion = "return=representation" in prefer
        cuerpo = json.loads(request.content) if request.content else None
        try:
            if request.method == "GET":
                filas = [f for f in tabla.filas if _filtro(tabla, params_lista)(f)]
                total = len(filas)
                filas = _ordenar(tabla, filas, params.get("order"))
                if "limit" in params:
                    filas = filas[: int(params["limit"])]
                salida = [_seleccionar(tabla, f, params.get("select")) for f in filas]
                headers = {}
                if "count=exact" in prefer:
                    headers["Content-Range"] = f"0-{len(salida) - 1}/{total}" if salida else f"*/{total}"
                return httpx.Response(200, json=salida, headers=headers)
            if request.method == "POST":
                # Una sentencia: si falla una fila, no queda ninguna (como la base).
                copia = json.loads(json.dumps(tabla.filas))
                siguiente = tabla.siguiente
                try:
                    filas = self._post(tabla, params, prefer, cuerpo)
                except ErrorPg:
                    tabla.filas, tabla.siguiente = copia, siguiente
                    raise
                if not representacion:
                    return httpx.Response(201)
                return httpx.Response(201, json=[_seleccionar(tabla, f, params.get("select")) for f in filas])
            if request.method == "PATCH":
                copia = json.loads(json.dumps(tabla.filas))
                try:
                    filas = self._patch(tabla, params_lista, cuerpo)
                except ErrorPg:
                    tabla.filas = copia
                    raise
                if not representacion:
                    return httpx.Response(204)
                return httpx.Response(200, json=[_seleccionar(tabla, f, params.get("select")) for f in filas])
        except ErrorPg as e:
            return httpx.Response(e.status, json={"code": e.codigo, "message": "error falso"})
        return httpx.Response(405)
