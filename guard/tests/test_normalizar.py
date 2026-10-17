"""Tests de la normalización de valores y de los montos válidos."""

import pytest

from guard.normalizar import iguales, monto_valido, numero


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [(120, 120.0), (120.5, 120.5), ("99.5", 99.5), (" 42 ", 42.0), (-3, -3.0)],
)
def test_numero_acepta_numeros_finitos(valor, esperado):
    assert numero(valor) == esperado


@pytest.mark.parametrize(
    "valor", [True, False, None, "abc", "", float("nan"), float("inf"), -float("inf"), "inf", [1]]
)
def test_numero_rechaza_lo_que_no_es_un_numero_finito(valor):
    assert numero(valor) is None


@pytest.mark.parametrize("valor", [120, 0.01, "150", "150.25"])
def test_monto_valido(valor):
    assert monto_valido(valor) == float(valor)


@pytest.mark.parametrize(
    "valor",
    [True, False, 0, 0.0, -5, "-5", float("nan"), float("inf"), "inf", "NaN", "abc", None,
     [120], {"a": 1}],
)
def test_monto_invalido(valor):
    assert monto_valido(valor) is None


def test_iguales_con_listas_de_no_hasheables_no_lanza():
    assert iguales([[1], {"a": 1}], [{"a": 1}, [1]]) is True
    assert iguales([[1]], [[2]]) is False
    assert iguales({"a": 1}, {"a": 1}) is True
    assert iguales(["Nike"], "Nike") is False
