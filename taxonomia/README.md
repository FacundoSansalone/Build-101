# taxonomia/

La [taxonomía de productos de Google](https://support.google.com/merchants/answer/6324436)
(árbol con IDs) para clasificar pedidos y ofertas de cualquier categoría sin
armar un esquema por categoría.

| Archivo | Qué hace |
|---|---|
| `arbol.py` | `Taxonomia` y `Categoria` (puros): hijos, ancestros, `es_descendiente`, `por_ruta`, `subconjunto` |
| `cargar.py` | `cargar_subconjunto()` (en el repo) y `cargar_completa(locale)` (caché local, descarga opcional) |
| `clasificar.py` | `clasificar(texto, taxonomia, cliente)`: nivel por nivel con `decisiones` |
| `datos/taxonomy-with-ids.en-US.subconjunto.txt` | Raíces + ramas de la demo: calzado (187), computadoras (278) y audio (223, con auriculares 543626) |

## Archivo completo

URL vigente (ayuda de Merchant Center):
`https://www.google.com/basepages/producttype/taxonomy-with-ids.<locale>.txt`
(p. ej. `en-US`, `es-ES`; los IDs son los mismos en todos los idiomas).

```sh
python -m taxonomia descargar --locale en-US      # a .temp-db/taxonomia/ (ignorado por git)
python -m taxonomia subconjunto --ids 187,278,223 # regenera el subconjunto del repo
```

`TAXONOMIA_CACHE_DIR` cambia la carpeta de la caché. Los tests nunca descargan.

## Clasificación

En cada nivel se hace una `Eleccion` entre las subcategorías del nivel actual
más `"ninguna"` (como mucho 255 opciones; cada opción nombra sus
subcategorías). Se baja mientras la probabilidad de la opción elegida alcance
el umbral (0,7 por defecto); si no alcanza, el modelo elige `"ninguna"`, no
contesta o la API falla, devuelve **el último nivel seguro** y el motivo. El
resultado es una propuesta: la ficha la confirma el usuario y el Guard no
depende de esta clasificación.

Prueba real opcional (unas 8 llamadas; la clave se lee del `.env` y no se
imprime):

```sh
RUN_DECISIONES_TESTS=1 DECISIONES_ENV_FILE=/ruta/a/.env PYTHONUTF8=1 \
  world/ap2/.venv/Scripts/python -m pytest taxonomia -k real
```
