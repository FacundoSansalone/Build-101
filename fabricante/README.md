# fabricante/

Ficha del **fabricante** por GTIN: una fuente de verdad que el vendedor no
controla. En el orden del Guard va segunda, después de los datos del
comprador y antes de la página pública del vendedor y su catálogo
(`guard/fuentes.py`).

El Guard no hace I/O: esto se consulta **afuera** de `evaluar()` y entra en el
mismo `catalogo` como `DatoExterno` con origen `"fabricante"`. El contrato
`Evaluador` no cambia.

```python
from fabricante import datos_externos, fuente_desde_entorno
from guard.fuentes import con_fuentes

fuente = fuente_desde_entorno()                  # FixtureFabricante por defecto
resultados = [fuente.consultar(g) for g in gtins]
veredicto = evaluar(ficha, vistas, cart_id, con_fuentes(catalogo, datos_externos(resultados)))
```

`guard.benchmark_hook.evaluate()` ya lo hace (GTIN declarados y los del
catálogo para esos SKU).

| Fuente | Qué es |
|---|---|
| `FixtureFabricante` | Archivos JSON **en formato Icecat** en `fixtures/fabricante/` (los genera `world/`). Son datos de prueba: cada resultado va rotulado (`fuente="fixture"`, `fixture=True`) y el Guard lo dice en el detalle ("ficha del fabricante (fixture de prueba)"). **Por defecto.** |
| `IcecatFabricante` | Icecat en vivo: `GET https://live.icecat.biz/api?lang=EN&shopname=<usuario>&GTIN=<gtin>`, tokens en las cabeceras `api-token`/`content-token`, timeout de 3 s, respuesta de hasta 2 MB y caché (memoria y `.temp-db/icecat/`). **Apagado por defecto.** |

Si la consulta falla (red, timeout, HTTP, formato, producto que no es el del
GTIN) el resultado es "sin dato" (`ficha=None` y el `motivo`): el Guard no
tiene fuente independiente y escala lo obligatorio. Nunca aprueba por eso.

## Configuración

| Variable | Por defecto | Qué hace |
|---|---|---|
| `FABRICANTE_FUENTE` | `fixture` | `fixture`, `icecat` o `ninguna` |
| `FABRICANTE_FIXTURES_DIR` | `fixtures/fabricante` | Carpeta de los fixtures |
| `ICECAT_USUARIO` | — | Usuario de Icecat (obligatorio con `icecat`) |
| `ICECAT_API_TOKEN`, `ICECAT_CONTENT_TOKEN` | — | Tokens de Icecat |
| `ICECAT_IDIOMA` | `EN` | Idioma de la ficha |
| `ICECAT_TIMEOUT` | `3` | Segundos por consulta |
| `ICECAT_CACHE_DIR` | `.temp-db/icecat` | Caché en disco (ignorada por git) |

Los nombres de las características de Icecat ("Internal memory", "Display
diagonal", "Active Noise Cancelling (ANC)"...) se pasan a los nombres
canónicos del Guard con el vocabulario de `guard/atributos.py` (sinónimos de
características, no un mapa por categoría). Las que no tienen sinónimo
quedan con su nombre normalizado (`touchscreen`).

## Atribución

Los datos de producto de Icecat se muestran con la leyenda **"Specs Icecat"**
y un enlace a [icecat.biz](https://icecat.biz), según las condiciones de Open
Icecat. `FichaFabricante.atribucion` la lleva en cada ficha. Los archivos de
`fixtures/fabricante/` **no son de Icecat**: solo usan su formato.

## Tests

Sin red (respuestas simuladas con `httpx.MockTransport`):

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest fabricante
```
