"""Clasificación jerárquica por niveles con el adaptador `decisiones` (cliente falso).

La prueba real (con la API) corre solo con RUN_DECISIONES_TESTS=1 y hace a lo
sumo ~10 llamadas. La clave se lee del .env de la raíz y nunca se imprime.
"""

import os
from pathlib import Path

import pytest

from decisiones import DecisionesError, Eleccion, Rechazo, RespuestaEleccion, cliente_desde_entorno
from taxonomia import NINGUNA, cargar_subconjunto, clasificar, parsear

TEXTO = """\
166 - Apparel & Accessories
187 - Apparel & Accessories > Shoes
222 - Electronics
278 - Electronics > Computers
328 - Electronics > Computers > Laptops
325 - Electronics > Computers > Desktop Computers
"""


class ClienteFalso:
    """Contesta, en orden, lo que se le indique; guarda cada pregunta."""

    nombre = "falso"

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.preguntas: list[tuple] = []

    def decidir(self, estado, preguntas):
        (pregunta,) = preguntas
        self.preguntas.append((estado, pregunta))
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        if respuesta is None:
            return {pregunta.nombre: Rechazo(pregunta.nombre)}
        eleccion, probabilidad = respuesta
        return {
            pregunta.nombre: RespuestaEleccion(
                pregunta.nombre, eleccion, {eleccion: probabilidad}, probabilidad
            )
        }


@pytest.fixture
def arbol():
    return parsear(TEXTO)


def test_baja_nivel_por_nivel_hasta_la_hoja(arbol):
    cliente = ClienteFalso(("222", 0.98), ("278", 0.95), ("328", 0.9))
    r = clasificar("Notebook 14 pulgadas, 16 GB de RAM", arbol, cliente)
    assert r.id == "328"
    assert r.completa and r.motivo == "hoja"
    assert [p.eleccion for p in r.pasos] == ["222", "278", "328"]
    assert r.confianza == pytest.approx(0.98 * 0.95 * 0.9)


def test_cada_nivel_es_una_eleccion_con_ninguna(arbol):
    cliente = ClienteFalso(("222", 0.98), ("278", 0.95), ("328", 0.9))
    clasificar("Notebook", arbol, cliente)
    estado, pregunta = cliente.preguntas[1]
    assert isinstance(pregunta, Eleccion)
    assert list(pregunta.opciones) == ["278", NINGUNA]
    # cada opción dice qué hay adentro (si no, "Audio Components" no se elige)
    assert pregunta.opciones["278"] == (
        "Electronics > Computers (incluye: Laptops, Desktop Computers)"
    )
    _, pregunta_hoja = cliente.preguntas[2]
    assert pregunta_hoja.opciones["328"] == "Electronics > Computers > Laptops"
    assert estado["producto"] == "Notebook"
    assert estado["categoria_actual"] == "Electronics"


def test_sin_confianza_suficiente_devuelve_el_ultimo_nivel_seguro(arbol):
    cliente = ClienteFalso(("222", 0.98), ("278", 0.95), ("328", 0.55))
    r = clasificar("Una computadora", arbol, cliente, umbral=0.7)
    assert r.id == "278"
    assert not r.completa and r.motivo == "umbral"
    assert r.pasos[-1].eleccion == "328" and r.pasos[-1].confianza == 0.55


def test_ninguna_detiene_en_el_nivel_anterior(arbol):
    r = clasificar("Una mesa", arbol, ClienteFalso(("222", 0.9), (NINGUNA, 0.8)))
    assert r.id == "222" and r.motivo == "ninguna"


def test_en_la_raiz_sin_confianza_no_hay_categoria(arbol):
    r = clasificar("algo", arbol, ClienteFalso(("222", 0.3)))
    assert r.categoria is None and r.id is None and r.ruta == ()
    assert r.motivo == "umbral"


@pytest.mark.parametrize(
    ("respuesta", "motivo"),
    [
        (None, "rechazo"),
        (DecisionesError("HTTP 500"), "error"),
        (("187", 0.99), "eleccion_invalida"),  # no es hija del nivel actual
    ],
)
def test_fallas_devuelven_el_ultimo_nivel_seguro(arbol, respuesta, motivo):
    r = clasificar("Notebook", arbol, ClienteFalso(("222", 0.99), respuesta))
    assert r.id == "222" and r.motivo == motivo and not r.completa


def test_max_niveles(arbol):
    r = clasificar("Notebook", arbol, ClienteFalso(("222", 0.99), ("278", 0.99)), max_niveles=2)
    assert r.id == "278" and r.motivo == "max_niveles"


def test_desde_una_categoria_conocida(arbol):
    cliente = ClienteFalso(("328", 0.9))
    r = clasificar("Notebook", arbol, cliente, desde="278")
    assert r.id == "328" and len(cliente.preguntas) == 1


def test_mas_de_255_opciones_no_se_pregunta():
    lineas = ["1 - Raiz"] + [f"{i} - Raiz > Hija {i}" for i in range(100, 100 + 255)]
    arbol = parsear("\n".join(lineas))
    cliente = ClienteFalso(("1", 0.99))
    r = clasificar("x", arbol, cliente)
    assert r.id == "1" and r.motivo == "demasiadas_opciones"
    assert len(cliente.preguntas) == 1  # no hizo la segunda pregunta


@pytest.mark.parametrize("umbral", [0.0, -0.1, 1.5])
def test_umbral_invalido(arbol, umbral):
    with pytest.raises(ValueError):
        clasificar("x", arbol, ClienteFalso(), umbral=umbral)


# ---------------------------------------------------------------------------
# Prueba real (opcional): RUN_DECISIONES_TESTS=1
# ---------------------------------------------------------------------------


def _entorno_con_env() -> dict[str, str]:
    """Variables del proceso más las del .env de la raíz (sin imprimirlas)."""
    entorno = dict(os.environ)
    ruta = Path(os.environ.get("DECISIONES_ENV_FILE") or Path(__file__).resolve().parents[2] / ".env")
    if ruta.is_file():
        for linea in ruta.read_text("utf-8").splitlines():
            clave, sep, valor = linea.partition("=")
            if sep and clave.strip() and not clave.lstrip().startswith("#"):
                entorno.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))
    return entorno


@pytest.mark.skipif(os.environ.get("RUN_DECISIONES_TESTS") != "1", reason="usa la API real")
@pytest.mark.parametrize(
    ("descripcion", "esperado", "ancestro"),
    [
        ("Notebook de 14 pulgadas con 16 GB de RAM y 512 GB de SSD", "328", "278"),
        ("Auriculares inalámbricos over-ear con cancelación de ruido activa", "543626", "505771"),
    ],
)
def test_clasificacion_real(descripcion, esperado, ancestro):
    cliente = cliente_desde_entorno(_entorno_con_env())
    if cliente is None:
        pytest.skip("sin OPENAI_API_KEY ni TYPESAFE_API_KEY")
    arbol = cargar_subconjunto()
    r = clasificar(descripcion, arbol, cliente, umbral=0.6)
    assert r.id is not None, r.motivo
    # Llega a la hoja esperada o se detiene en un nivel seguro de su rama.
    assert arbol.es_descendiente(esperado, r.id)
    # y por lo menos hasta el nivel del ancestro (computadoras, auriculares)
    assert arbol.es_descendiente(r.id, ancestro)
