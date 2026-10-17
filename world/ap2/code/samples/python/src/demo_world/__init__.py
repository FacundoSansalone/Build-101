"""Mundo de la demo del Pre-Sign Guard (hackatón).

Este paquete NO es parte de AP2. Agrega, sobre el sample oficial:

- scenario.py       escenarios del comercio (honesto, A, B, B2, C)
- public_catalog.py catálogo público verdadero (puerto 8011)
- ap2_check.py      chequeo de topes usando el evaluador real del SDK de AP2
- benchmark.py      corre cada escenario N veces y mide la tasa de éxito
- export_fixtures.py exporta los escenarios a fixtures/ para el resto del equipo
- set_scenario.py   cambia el escenario activo sin reiniciar el comercio
"""
