"""Archivo completo de la taxonomía: caché local y descarga (simulada, sin red)."""

import httpx
import pytest

from taxonomia import URL_TAXONOMIA, cargar_completa, descargar, ruta_cache

TEXTO = "# Google_Product_Taxonomy_Version: 2021-09-21\n222 - Electronics\n278 - Electronics > Computers\n"


def _transporte(texto=TEXTO, status=200, pedidos=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if pedidos is not None:
            pedidos.append(str(request.url))
        return httpx.Response(status, text=texto)

    return httpx.MockTransport(handler)


def test_url_vigente_de_merchant_center():
    assert URL_TAXONOMIA.format(locale="en-US") == (
        "https://www.google.com/basepages/producttype/taxonomy-with-ids.en-US.txt"
    )


def test_sin_cache_y_sin_descargar_es_un_error_claro(tmp_path):
    with pytest.raises(FileNotFoundError, match="python -m taxonomia descargar"):
        cargar_completa("en-US", cache_dir=tmp_path)


def test_descarga_guarda_en_cache_y_despues_no_vuelve_a_pedir(tmp_path):
    pedidos: list[str] = []
    arbol = cargar_completa(
        "es-ES", cache_dir=tmp_path, descargar=True, transport=_transporte(pedidos=pedidos)
    )
    assert arbol["278"].ruta_texto == "Electronics > Computers"
    assert pedidos == [URL_TAXONOMIA.format(locale="es-ES")]
    assert ruta_cache("es-ES", tmp_path).read_text("utf-8") == TEXTO

    otra = cargar_completa("es-ES", cache_dir=tmp_path, descargar=True, transport=_transporte(pedidos=pedidos))
    assert len(otra) == 2 and len(pedidos) == 1  # salió de la caché


@pytest.mark.parametrize("locale", ["en", "../x", "en-US/../../a", "EN-us", ""])
def test_locale_invalido(tmp_path, locale):
    with pytest.raises(ValueError):
        ruta_cache(locale, tmp_path)


@pytest.mark.parametrize(
    "transporte",
    [
        _transporte(status=404),
        _transporte(texto="<html>no es la taxonomía</html>"),
        _transporte(texto=""),
    ],
)
def test_descarga_fallida_no_deja_cache(tmp_path, transporte):
    with pytest.raises((httpx.HTTPError, ValueError)):
        descargar("en-US", cache_dir=tmp_path, transport=transporte)
    assert not ruta_cache("en-US", tmp_path).exists()


def test_la_cache_por_defecto_esta_fuera_de_git(monkeypatch):
    monkeypatch.delenv("TAXONOMIA_CACHE_DIR", raising=False)
    ruta = ruta_cache("en-US")
    assert ".temp-db" in ruta.parts  # .temp-db/ está en .gitignore
    assert ruta.name == "taxonomy-with-ids.en-US.txt"
