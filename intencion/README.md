# intencion/ — agente de intención (etapa 2)

La segunda capa del flujo único ([docs/FLUJO.md](../docs/FLUJO.md)): le
pregunta al usuario **para qué** quiere lo que pide y, si ninguna oferta
cumple el pedido literal, busca una que cubra la **necesidad**. Propone;
nunca autoriza. Lo autoriza el usuario, y el Guard (`guard/`) verifica la
compra contra lo autorizado antes de la firma.

Lo usa el proxy (`proxy/`): `python -m proxy` lo carga si hay
`GOOGLE_API_KEY` o `GEMINI_API_KEY` (ver [proxy/README.md](../proxy/README.md#etapa-2-contexto-filtro-y-oportunidad)).

## Las tres funciones

| Función | Entra | Sale | Lee texto del proveedor |
|---|---|---|---|
| `preguntar(pedido, hoy, cliente, categorias=)` | La búsqueda del agente del cliente | `Pregunta`: la pregunta del porqué y los **parámetros explícitos** (tope, moneda, cantidad, entrega, categoría) | No |
| `armar_contexto(pedido, pregunta, respuesta, hoy, cliente)` | Lo anterior + la respuesta del usuario | `Contexto`: la necesidad, el cronograma real y las flexibilidades (`dicha` o `inferida`) | No |
| `detectar_oportunidad(contexto, ficha, ofertas, cliente)` | El contexto, los parámetros autorizados y las `Oferta` ya extraídas y verificadas | `Oportunidad` o `None` | Solo campos tipados, con los textos acotados |

`AgenteGemini` junta las tres con un cliente de `google-genai` (el que usa
el proxy). El modelo es `gemini-3.1-flash-lite-preview` por defecto
(`INTENCION_MODELO` para cambiarlo).

## Qué decide el código y qué el modelo

El modelo pregunta, entiende la respuesta y elige qué oferta proponer. El
código:

- valida todo lo que devuelve el modelo (fechas ISO no pasadas, montos
  finitos, moneda ISO 4217, categoría solo del catálogo público);
- busca alternativas solo si **ninguna** oferta cumple el pedido, y nunca
  entre las que se pasan del tope o cambian la moneda;
- arma los cambios y los parámetros propuestos con **los datos de la
  oferta**, no con el texto del modelo;
- descarta la propuesta si contradice lo verificable: si la necesidad tiene
  un cronograma ("10 el lunes y 10 el jueves"), la oferta tiene que cubrirlo
  acumulado (`guard.normalizar.cubre_entregas`).

## Probar

```sh
# tests (cliente de Gemini falso), desde la raíz del repo:
PYTHONUTF8=1 world/ap2/.venv/bin/python -m pytest intencion -q

# contra Gemini real (con GOOGLE_API_KEY o GEMINI_API_KEY en el .env):
# 3 casos (miércoles → propone; viernes y martes → no). 9/9 el 2026-10-18.
set -a; source .env; set +a
PYTHONPATH=. world/ap2/.venv/bin/python -m intencion.evaluar_casos --veces 3 --pausa 4
```

Los datos de prueba están en
[`fixtures/etapa2/computadoras.json`](../fixtures/etapa2/computadoras.json),
escrito a mano hasta que `world/` tenga el escenario de computadoras.
