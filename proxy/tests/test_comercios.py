"""Registro de comercios del gateway (`proxy/comercios.py`).

Lo que importa: el upstream sale siempre del registro (un id raro nunca da
otro servidor), `http` solo vale en loopback, todo comercio que no es el por
defecto declara sus `merchant_ids` y la política del Guard sale de ellos.
"""

from __future__ import annotations

import json

import pytest

from contracts import Politica
from proxy.comercios import (
    RegistroComercios,
    base_url,
    comercio_por_defecto,
    merchant_id_coincide,
    politica_de,
    url_publica_comercio,
    validar_url_upstream,
)
from proxy.tipos_gateway import Comercio, ConfigInvalida, Integrador


def _datos() -> dict:
    return {
        "por_defecto": "merchant_1",
        "comercios": [
            {
                "id": "merchant_1",
                "nombre": "Demo",
                "a2a_url": "http://127.0.0.1:8001/a2a/merchant_agent",
                "catalogo_url": "http://127.0.0.1:8011",
                "merchant_ids": ["merchant_1"],
                "respaldo_fixture": False,
            },
            {
                "id": "tienda_b",
                "nombre": "Tienda B",
                "a2a_url": "https://b.example/a2a/agent/",
                "catalogo_url": None,
                "merchant_ids": ["tienda-b"],
            },
        ],
    }


def _integrador(*comercios: str) -> Integrador:
    return Integrador(id="acme", nombre="ACME", comercios=tuple(comercios))


# -- archivo válido -----------------------------------------------------------


def test_archivo_valido(tmp_path):
    path = tmp_path / "comercios.json"
    path.write_text(json.dumps(_datos()), encoding="utf-8")
    registro = RegistroComercios.desde_archivo(path)

    assert registro.por_defecto.id == "merchant_1"
    assert [c.id for c in registro.todos()] == ["merchant_1", "tienda_b"]
    b = registro.get("tienda_b")
    assert b == Comercio(
        id="tienda_b",
        nombre="Tienda B",
        a2a_url="https://b.example/a2a/agent",  # sin la / final
        catalogo_url=None,
        merchant_ids=("tienda-b",),
        respaldo_fixture=False,
    )
    assert registro.get("merchant_1").catalogo_url == "http://127.0.0.1:8011"


def test_por_defecto_puede_no_tener_merchant_ids():
    datos = _datos()
    del datos["comercios"][0]["merchant_ids"]
    registro = RegistroComercios.desde_dict(datos)
    assert registro.por_defecto.merchant_ids == ()


def test_merchant_ids_repetidos_se_unifican():
    datos = _datos()
    datos["comercios"][1]["merchant_ids"] = ["tienda-b", "tienda-b", "b:2"]
    assert RegistroComercios.desde_dict(datos).get("tienda_b").merchant_ids == (
        "tienda-b",
        "b:2",
    )


def test_archivo_ilegible_o_no_json(tmp_path):
    with pytest.raises(ConfigInvalida):
        RegistroComercios.desde_archivo(tmp_path / "no_existe.json")
    roto = tmp_path / "roto.json"
    roto.write_text("{no es json", encoding="utf-8")
    with pytest.raises(ConfigInvalida):
        RegistroComercios.desde_archivo(roto)


# -- archivos inválidos -------------------------------------------------------


def _invalido(cambio) -> None:
    datos = _datos()
    cambio(datos)
    with pytest.raises(ConfigInvalida):
        RegistroComercios.desde_dict(datos)


def test_por_defecto_inexistente():
    _invalido(lambda d: d.update(por_defecto="otro"))


def test_por_defecto_ausente_o_invalido():
    _invalido(lambda d: d.pop("por_defecto"))
    _invalido(lambda d: d.update(por_defecto="../x"))


def test_id_duplicado():
    _invalido(lambda d: d["comercios"][1].update(id="merchant_1"))


@pytest.mark.parametrize("malo", ["../x", "Mayus", "", "a" * 41, None, 3, "-x", "a b"])
def test_id_invalido(malo):
    _invalido(lambda d: d["comercios"][1].update(id=malo))


def test_comercio_no_por_defecto_sin_merchant_ids():
    _invalido(lambda d: d["comercios"][1].pop("merchant_ids"))
    _invalido(lambda d: d["comercios"][1].update(merchant_ids=[]))


@pytest.mark.parametrize("malo", ["", " x", "a/b", None, 7, "x" * 201])
def test_merchant_id_invalido(malo):
    _invalido(lambda d: d["comercios"][1].update(merchant_ids=[malo]))


def test_merchant_ids_no_lista():
    _invalido(lambda d: d["comercios"][1].update(merchant_ids="tienda-b"))


def test_claves_desconocidas():
    # Una clave mal escrita dejaría sin chequeo lo firmado: no se adivina.
    _invalido(lambda d: d["comercios"][1].update(merchant_id=["tienda-b"]))
    _invalido(lambda d: d.update(extra=1))


def test_estructura_invalida():
    _invalido(lambda d: d.update(comercios=[]))
    _invalido(lambda d: d.update(comercios={"id": "x"}))
    _invalido(lambda d: d["comercios"].append("tienda_c"))
    _invalido(lambda d: d["comercios"][1].pop("a2a_url"))
    _invalido(lambda d: d["comercios"][1].update(respaldo_fixture="no"))
    _invalido(lambda d: d["comercios"][1].update(nombre=""))
    with pytest.raises(ConfigInvalida):
        RegistroComercios.desde_dict([])  # type: ignore[arg-type]


def test_http_fuera_de_loopback():
    _invalido(lambda d: d["comercios"][1].update(a2a_url="http://b.example/a2a/agent"))
    _invalido(lambda d: d["comercios"][1].update(catalogo_url="http://b.example"))


def test_userinfo():
    _invalido(
        lambda d: d["comercios"][1].update(a2a_url="https://user:pw@b.example/a2a/agent")
    )
    _invalido(lambda d: d["comercios"][0].update(catalogo_url="http://u@127.0.0.1:8011"))


# -- validar_url_upstream -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8001/a2a/merchant_agent",
        "http://localhost:8001",
        "http://[::1]:8001/x",
        "https://b.example/a2a/agent",
    ],
)
def test_url_upstream_valida(url):
    assert validar_url_upstream(url + "/") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://b.example/a2a",       # http fuera de loopback
        "http://10.0.0.5/a2a",        # red privada tampoco es loopback
        "https://u:p@b.example/a2a",  # userinfo
        "https://u@b.example/a2a",
        "https://b.example/a2a#x",    # fragment
        "https://b.example/a2a?x=1",  # query: rompe el `/{resto}` de la card
        "ftp://b.example/a2a",
        "file:///etc/passwd",
        "https:///sin-host",
        "https://b.example:puerto/a2a",
        "",
        None,
    ],
)
def test_url_upstream_invalida(url):
    with pytest.raises(ConfigInvalida):
        validar_url_upstream(url)  # type: ignore[arg-type]


# -- solo() y comercio por defecto ---------------------------------------------


def test_solo_con_el_comercio_por_defecto():
    # Sin archivo, como hoy: sin regla de loopback (los tests usan hosts falsos
    # por http) y sin merchant_ids (ningún chequeo nuevo).
    comercio = comercio_por_defecto(
        id="merchant_1",
        a2a_url="http://merchant.test/a2a/merchant_agent",
        catalogo_url="",
        respaldo_fixture=True,
    )
    registro = RegistroComercios.solo(comercio)
    assert registro.por_defecto is comercio
    assert registro.todos() == [comercio]
    assert registro.get(None) is comercio
    assert registro.get("merchant_1") is comercio
    assert comercio.a2a_url == "http://merchant.test/a2a/merchant_agent"
    assert comercio.catalogo_url is None
    assert comercio.merchant_ids == ()
    assert comercio.respaldo_fixture is True


def test_comercio_por_defecto_con_id_invalido():
    with pytest.raises(ConfigInvalida):
        comercio_por_defecto(
            id="../x", a2a_url="http://m/a2a", catalogo_url=None, respaldo_fixture=False
        )


# -- get ----------------------------------------------------------------------


def test_get_none_da_el_por_defecto_y_un_id_raro_da_none():
    registro = RegistroComercios.desde_dict(_datos())
    assert registro.get(None) is registro.por_defecto
    assert registro.get("../x") is None
    assert registro.get("tienda_b/../merchant_1") is None
    assert registro.get("no_existe") is None
    assert registro.get("") is None
    assert registro.get(5) is None  # type: ignore[arg-type]


# -- permitidos e ids_inexistentes ---------------------------------------------


def test_permitidos_lista_vacia_es_solo_el_por_defecto():
    registro = RegistroComercios.desde_dict(_datos())
    assert registro.permitidos(_integrador()) == [registro.por_defecto]


def test_permitidos_ignora_ids_inexistentes_y_respeta_el_orden():
    registro = RegistroComercios.desde_dict(_datos())
    ids = [c.id for c in registro.permitidos(_integrador("tienda_b", "fantasma", "merchant_1", "tienda_b"))]
    assert ids == ["tienda_b", "merchant_1"]
    # Si ninguno existe, ninguno: no cae al por defecto.
    assert registro.permitidos(_integrador("fantasma")) == []


def test_ids_inexistentes():
    registro = RegistroComercios.desde_dict(_datos())
    integradores = [
        _integrador("tienda_b", "fantasma"),
        Integrador(id="otro", nombre="Otro", comercios=("zeta", "fantasma", "merchant_1")),
        _integrador(),
    ]
    assert registro.ids_inexistentes(integradores) == ["fantasma", "zeta"]
    assert registro.ids_inexistentes([_integrador()]) == []


# -- merchant.id firmado y política --------------------------------------------


def test_merchant_id_coincide_sin_lista():
    comercio = comercio_por_defecto(
        id="merchant_1", a2a_url="http://m/a2a", catalogo_url=None, respaldo_fixture=False
    )
    assert merchant_id_coincide(comercio, "cualquiera")
    assert merchant_id_coincide(comercio, None)


def test_merchant_id_coincide_con_lista():
    comercio = RegistroComercios.desde_dict(_datos()).get("tienda_b")
    assert merchant_id_coincide(comercio, "tienda-b")
    assert not merchant_id_coincide(comercio, "merchant_1")
    assert not merchant_id_coincide(comercio, "TIENDA-B")
    assert not merchant_id_coincide(comercio, None)  # sin merchant.id firmado: no


def test_politica_de_union():
    datos = _datos()
    datos["comercios"][1]["merchant_ids"] = ["tienda-b", "merchant_1"]
    registro = RegistroComercios.desde_dict(datos)
    politica = politica_de(registro.todos())
    assert isinstance(politica, Politica)
    assert politica.comercios_aprobados == ["merchant_1", "tienda-b"]
    assert politica_de([registro.get("tienda_b")]).comercios_aprobados == [
        "tienda-b",
        "merchant_1",
    ]


def test_politica_de_vacia_es_none():
    # Sin merchant_ids el Guard sigue con su Politica() por defecto, como hoy.
    comercio = comercio_por_defecto(
        id="merchant_1", a2a_url="http://m/a2a", catalogo_url=None, respaldo_fixture=False
    )
    assert politica_de([comercio]) is None
    assert politica_de([]) is None


# -- URLs públicas --------------------------------------------------------------


def test_url_publica_comercio():
    assert (
        url_publica_comercio("https://gw.example", "tienda_b")
        == "https://gw.example/a2a/m/tienda_b"
    )
    assert (
        url_publica_comercio("http://localhost:8010/", "merchant_1")
        == "http://localhost:8010/a2a/m/merchant_1"
    )
    with pytest.raises(ValueError):
        url_publica_comercio("https://gw.example", "../x")


def test_base_url():
    assert base_url("http://localhost:8010/a2a/merchant_agent") == "http://localhost:8010"
    assert base_url("https://gw.example") == "https://gw.example"
    assert base_url("https://u:p@gw.example:8443/x?y#z") == "https://gw.example:8443"
    assert base_url("http://[::1]:8010/a2a") == "http://[::1]:8010"
    with pytest.raises(ConfigInvalida):
        base_url("sin-esquema")
