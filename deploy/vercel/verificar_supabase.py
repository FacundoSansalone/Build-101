"""¿Están aplicadas en Supabase las migraciones 003 y 004 del gateway? Solo lectura.

Con `SUPABASE_URL` y la clave de servicio del entorno
(`SUPABASE_SERVICE_ROLE_KEY` o `SUPABASE_SECRET_KEY`, las mismas que usa el
motor) pregunta por PostgREST, tabla por tabla, si existen las tablas y las
columnas que crean `proxy/sql/003_gateway.sql` y
`proxy/sql/004_serverless.sql`, y dice qué falta. Lo que espera lo saca de
esos archivos; si 004 todavía no está en el repo, usa la lista del plan
(rotulada "según el plan").

Cada consulta es un `GET /rest/v1/<tabla>?select=<columnas>&limit=0`: no lee
filas ni escribe nada. La clave nunca se imprime.

Con `SUPABASE_ANON_KEY` (o `SUPABASE_PUBLISHABLE_KEY`) además comprueba que la
clave pública NO pueda leer las tablas `gw_*` (RLS y `revoke`).

Los índices, el trigger y RLS no se ven por PostgREST: la consulta para el SQL
Editor está en deploy/vercel/README.md.

    SUPABASE_URL=… SUPABASE_SERVICE_ROLE_KEY=… \\
        world/ap2/.venv/Scripts/python deploy/vercel/verificar_supabase.py

Sale con 0 si está todo, 1 si falta algo y 2 si no se pudo verificar.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parents[2]
SQL = RAIZ / "proxy" / "sql"
MIGRACIONES = ("003_gateway.sql", "004_serverless.sql")
TIMEOUT = httpx.Timeout(10.0)

# Lo que agrega 004 según el plan (diseño 2.4 y "Despliegue"), por si el
# archivo todavía no está en esta rama.
ESPERADO_004_PLAN: dict[str, tuple[str, ...]] = {
    "gw_compras": ("espera_id",),
    "gw_esperas": (
        "id", "compra_id", "tipo", "estado", "oportunidad_id", "vence", "respuesta",
        "creada", "resuelta",
    ),
    "gw_eventos": (
        "orden", "id", "ts", "integrador_id", "compra_id", "contexto_id", "tipo", "datos",
    ),
    "gw_entregas_webhook": ("cuerpo", "proximo_intento", "tomada_hasta"),
}

_PALABRAS_NO_COLUMNA = frozenset({
    "constraint", "primary", "unique", "check", "foreign", "exclude", "like",
})
_CREATE_RE = re.compile(
    r"create\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?\"?(\w+)\"?\s*\(",
    re.IGNORECASE,
)
_ALTER_RE = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?:public\.)?\"?(\w+)\"?(.*?);",
    re.IGNORECASE | re.DOTALL,
)
_ADD_COLUMN_RE = re.compile(
    r"add\s+column\s+(?:if\s+not\s+exists\s+)?\"?(\w+)\"?", re.IGNORECASE
)

Salida = Callable[[str], None]


# ---------------------------------------------------------------------------
# lo que esperan las migraciones
# ---------------------------------------------------------------------------


def _sin_comentarios(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def _hasta_el_parentesis(texto: str, inicio: int) -> str:
    """Lo que hay entre el `(` en `inicio - 1` y su `)`, salteando comillas."""
    profundidad = 1
    i = inicio
    en_comillas = False
    while i < len(texto):
        c = texto[i]
        if c == "'":
            en_comillas = not en_comillas
        elif not en_comillas:
            if c == "(":
                profundidad += 1
            elif c == ")":
                profundidad -= 1
                if profundidad == 0:
                    return texto[inicio:i]
        i += 1
    raise ValueError("paréntesis sin cerrar en un create table")


def _partir_nivel_cero(texto: str) -> list[str]:
    partes, actual, profundidad, en_comillas = [], [], 0, False
    for c in texto:
        if c == "'":
            en_comillas = not en_comillas
        elif not en_comillas:
            if c == "(":
                profundidad += 1
            elif c == ")":
                profundidad -= 1
            elif c == "," and profundidad == 0:
                partes.append("".join(actual))
                actual = []
                continue
        actual.append(c)
    partes.append("".join(actual))
    return [p.strip() for p in partes if p.strip()]


def columnas_de_sql(sql: str) -> dict[str, list[str]]:
    """Tabla → columnas que crea o agrega el SQL (create table y alter table
    … add column), en orden y sin repetir."""
    sql = _sin_comentarios(sql)
    tablas: dict[str, list[str]] = {}
    for m in _CREATE_RE.finditer(sql):
        tabla = m.group(1).lower()
        cuerpo = _hasta_el_parentesis(sql, m.end())
        for definicion in _partir_nivel_cero(cuerpo):
            primera = definicion.split()[0].strip('"').lower()
            if primera in _PALABRAS_NO_COLUMNA:
                continue
            columnas = tablas.setdefault(tabla, [])
            if primera not in columnas:
                columnas.append(primera)
    for m in _ALTER_RE.finditer(sql):
        tabla = m.group(1).lower()
        for columna in _ADD_COLUMN_RE.findall(m.group(2)):
            columnas = tablas.setdefault(tabla, [])
            if columna.lower() not in columnas:
                columnas.append(columna.lower())
    return tablas


@dataclass
class Esperado:
    tablas: dict[str, list[str]] = field(default_factory=dict)
    origen: dict[str, str] = field(default_factory=dict)  # tabla → de dónde salió

    def sumar(self, tablas: Mapping[str, list[str] | tuple[str, ...]], origen: str) -> None:
        for tabla, columnas in tablas.items():
            actuales = self.tablas.setdefault(tabla, [])
            for columna in columnas:
                if columna not in actuales:
                    actuales.append(columna)
            self.origen.setdefault(tabla, origen)
            if origen not in self.origen[tabla]:
                self.origen[tabla] += f" + {origen}"


def esperado(carpeta: Path = SQL) -> Esperado:
    resultado = Esperado()
    for nombre in MIGRACIONES:
        ruta = carpeta / nombre
        if ruta.exists():
            resultado.sumar(columnas_de_sql(ruta.read_text(encoding="utf-8")), nombre[:3])
        elif nombre.startswith("004"):
            resultado.sumar(ESPERADO_004_PLAN, "004 según el plan")
        else:
            raise FileNotFoundError(f"falta {ruta}")
    return resultado


# ---------------------------------------------------------------------------
# lo que hay en la base
# ---------------------------------------------------------------------------


@dataclass
class Estado:
    tabla: str
    existe: bool | None  # None: no se pudo saber
    faltan: list[str] = field(default_factory=list)
    error: str | None = None


def _cabeceras(clave: str) -> dict[str, str]:
    return {"apikey": clave, "Authorization": f"Bearer {clave}", "Accept": "application/json"}


def _codigo(resp: httpx.Response) -> str:
    try:
        datos = resp.json()
    except ValueError:
        return ""
    return str(datos.get("code") or "") if isinstance(datos, dict) else ""


def _select(http: httpx.Client, tabla: str, columnas: list[str]) -> httpx.Response:
    return http.get(f"/{tabla}", params={"select": ",".join(columnas), "limit": "0"})


def revisar_tabla(http: httpx.Client, tabla: str, columnas: list[str]) -> Estado:
    resp = _select(http, tabla, columnas)
    if resp.status_code == 200:
        return Estado(tabla, True)
    codigo = _codigo(resp)
    if resp.status_code == 404 or codigo in ("PGRST205", "42P01"):
        return Estado(tabla, False)
    if resp.status_code in (401, 403):
        return Estado(tabla, None, error=f"HTTP {resp.status_code} {codigo}: ¿es la clave de servicio?")
    if resp.status_code == 400 and codigo in ("42703", "PGRST204", "PGRST100"):
        # Falta al menos una columna: de a una para decir cuáles.
        faltan = []
        for columna in columnas:
            una = _select(http, tabla, [columna])
            if una.status_code != 200:
                faltan.append(columna)
        return Estado(tabla, True, faltan=faltan)
    return Estado(tabla, None, error=f"HTTP {resp.status_code} {codigo}".strip())


def revisar_anon(http: httpx.Client, tabla: str) -> str | None:
    """None si la clave pública no puede leer la tabla (bien); si no, el problema."""
    resp = http.get(f"/{tabla}", params={"select": "*", "limit": "1"})
    if resp.status_code in (401, 403, 404) or _codigo(resp) == "42501":
        return None
    if resp.status_code == 200:
        return "la clave pública puede leerla: falta RLS o el revoke de 003"
    return f"respuesta inesperada con la clave pública (HTTP {resp.status_code})"


def credenciales(env: Mapping[str, str]) -> tuple[str, str] | None:
    url = (env.get("SUPABASE_URL") or "").strip().rstrip("/")
    clave = (env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip() or (
        env.get("SUPABASE_SECRET_KEY") or ""
    ).strip()
    if not url or not clave:
        return None
    return url, clave


def verificar(
    env: Mapping[str, str],
    *,
    salida: Salida = print,
    transporte: httpx.BaseTransport | None = None,
    carpeta_sql: Path = SQL,
) -> int:
    datos = credenciales(env)
    if datos is None:
        salida("Falta SUPABASE_URL o la clave de servicio (SUPABASE_SERVICE_ROLE_KEY o "
               "SUPABASE_SECRET_KEY) en el entorno.")
        return 2
    url, clave = datos
    try:
        espera = esperado(carpeta_sql)
    except (OSError, ValueError) as e:
        salida(f"No se pudieron leer las migraciones: {e}")
        return 2
    host = httpx.URL(url).host or url
    salida(f"Supabase {host}: {len(espera.tablas)} tablas a revisar (solo lectura).")

    problemas = 0
    sin_saber = 0
    try:
        with httpx.Client(base_url=f"{url}/rest/v1", headers=_cabeceras(clave), timeout=TIMEOUT,
                          transport=transporte, trust_env=False) as http:
            for tabla, columnas in espera.tablas.items():
                estado = revisar_tabla(http, tabla, columnas)
                origen = espera.origen[tabla]
                if estado.existe is None:
                    sin_saber += 1
                    salida(f"  ?? {tabla} ({origen}): {estado.error}")
                elif not estado.existe:
                    problemas += 1
                    salida(f"  FALTA la tabla {tabla} ({origen})")
                elif estado.faltan:
                    problemas += 1
                    salida(f"  FALTAN columnas en {tabla} ({origen}): {', '.join(estado.faltan)}")
                else:
                    salida(f"  ok {tabla} ({len(columnas)} columnas, {origen})")
        anon = (env.get("SUPABASE_ANON_KEY") or env.get("SUPABASE_PUBLISHABLE_KEY") or "").strip()
        if anon:
            salida("Con la clave pública (tiene que dar acceso denegado):")
            with httpx.Client(base_url=f"{url}/rest/v1", headers=_cabeceras(anon), timeout=TIMEOUT,
                              transport=transporte, trust_env=False) as http:
                for tabla in espera.tablas:
                    problema = revisar_anon(http, tabla)
                    if problema:
                        problemas += 1
                        salida(f"  MAL {tabla}: {problema}")
                    else:
                        salida(f"  ok {tabla}: no la puede leer")
    except httpx.HTTPError as e:
        salida(f"No se pudo hablar con Supabase ({type(e).__name__}).")
        return 2

    if problemas:
        salida(f"Falta aplicar algo ({problemas}). Aplicá proxy/sql/003_gateway.sql y después "
               "proxy/sql/004_serverless.sql en el SQL Editor (ver deploy/vercel/README.md).")
        return 1
    if sin_saber:
        salida("No se pudo verificar todo (ver arriba).")
        return 2
    salida("Todo en orden. Los índices, el trigger y RLS se revisan en el SQL Editor "
           "(consulta en deploy/vercel/README.md).")
    return 0


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description="Verifica (solo lectura) que Supabase tenga las tablas y columnas de "
        "proxy/sql/003_gateway.sql y 004_serverless.sql. Lee SUPABASE_URL y "
        "SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SECRET_KEY) del entorno; con "
        "SUPABASE_ANON_KEY comprueba además que la clave pública no lea nada."
    ).parse_args(argv)
    return verificar(os.environ)


if __name__ == "__main__":
    sys.exit(main())
