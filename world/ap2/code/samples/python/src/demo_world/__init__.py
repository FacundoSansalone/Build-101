"""Mundo de la demo del Pre-Sign Guard (hackatón).

Este paquete NO es parte de AP2. Agrega, sobre el sample oficial:

- scenario.py       escenarios del comercio: zapatillas (honesto, A, B, B2, C),
                    notebooks y auriculares (honesto y malicioso, con GTIN y
                    atributos) y computadoras (etapa 2); los presets del
                    comercio general (etapa 3) los delega en general.py
- consulta.py       parser determinístico de catalog_search (es/en)
- general.py        comercio general: busca en data/productos.json y aplica
                    un comportamiento (honesto, inyección, dato falso...)
- attributes.py     comparación de atributos para la verdad del benchmark
- public_catalog.py catálogo público verdadero (puerto 8011)
- ap2_check.py      chequeo de topes usando el evaluador real del SDK de AP2
- benchmark.py      corre cada escenario N veces y mide la tasa de éxito
- export_fixtures.py exporta los escenarios a fixtures/ (zapatillas en
                    fixtures/scenario_*.json; el resto en fixtures/etapa2/;
                    los presets generales en fixtures/general/; fichas del
                    fabricante en formato Icecat en fixtures/fabricante/)
- set_scenario.py   cambia el escenario activo sin reiniciar el comercio

Datos (data/): catalog.json (lo que el comercio publica), unlisted.json (lo
que vende sin publicar: computadoras, notebooks y auriculares),
manufacturers.json (fichas de los fabricantes por GTIN), productos.json (el
dataset del comercio general: catálogo público y fabricante salen de ahí) y
scenarios.json.
"""
