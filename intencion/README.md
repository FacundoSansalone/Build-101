# intencion/ — agente de intención (etapa 2)

La segunda capa del flujo único ([docs/FLUJO.md](../docs/FLUJO.md)): le
pregunta al usuario **para qué** quiere lo que pide y, si ninguna oferta
cumple el pedido literal, busca una que cubra la **necesidad**. Propone;
nunca autoriza. Lo autoriza el usuario, y el Guard (`guard/`) verifica la
compra contra lo autorizado antes de la firma.

Lo usa el proxy (`proxy/`): `python -m proxy` lo carga si hay
`GOOGLE_API_KEY` o `GEMINI_API_KEY` (ver [proxy/README.md](../proxy/README.md#etapa-2-contexto-filtro-y-oportunidad)).
Con el agente cargado, el filtro de seguridad (`GUARD_FILTRO`) es
obligatorio: sin él el proxy no arranca, salvo `PROXY_PERMITIR_SIN_FILTRO=1`
explícito (tests y demo), que se avisa en el log.

## Las tres funciones

| Función | Entra | Sale | Lee texto del proveedor |
|---|---|---|---|
| `preguntar(pedido, hoy, cliente, categorias=)` | La búsqueda del agente del cliente | `Pregunta`: la pregunta del porqué y los **parámetros explícitos** (tope, moneda, cantidad, entrega, categoría) | No |
| `armar_contexto(pedido, pregunta, respuesta, hoy, cliente)` | Lo anterior + la respuesta del usuario | `Contexto`: la necesidad, el cronograma real y las flexibilidades (`dicha` o `inferida`), cada una atada a un campo | No |
| `detectar_oportunidad(contexto, ficha, ofertas, cliente)` | El contexto, los parámetros autorizados y las `Oferta` ya extraídas y verificadas | `Oportunidad` o `None` | Solo campos tipados, con los textos acotados, y solo de las ofertas que se pueden proponer |
| `campos_cambiables(contexto) -> set[str]` | El contexto | Los campos que una oportunidad puede proponer cambiar | No (no llama al modelo) |

`AgenteGemini` junta las tres primeras con un cliente de `google-genai` (el
que usa el proxy). El modelo es `gemini-3.1-flash-lite-preview` por defecto
(`INTENCION_MODELO` para cambiarlo).

## Qué decide el código y qué el modelo

El modelo pregunta, entiende la respuesta y elige qué oferta proponer entre
las que el código ya aceptó. El código:

- valida todo lo que devuelve el modelo (fechas ISO no pasadas, montos
  finitos, moneda ISO 4217, categoría solo del catálogo público);
- busca alternativas solo si **ninguna** oferta cumple el pedido, y nunca
  entre las que se pasan del tope o cambian la moneda;
- **solo propone cambiar los campos de `campos_cambiables()`** (ver abajo);
  una oferta que cambia otro campo (por ejemplo, la categoría o el uso que
  el usuario no dijo flexibles) se descarta antes de llamar al modelo, así
  su texto no le llega;
- arma los cambios y los parámetros propuestos con **los datos de la
  oferta**, no con el texto del modelo;
- descarta la propuesta si contradice lo verificable: si la necesidad tiene
  un cronograma ("10 el lunes y 10 el jueves"), la oferta tiene que cubrirlo
  acumulado (`guard.normalizar.cubre_entregas`);
- arma la explicación para el usuario (`Oportunidad.por_que`) con una
  **plantilla**: qué cambia, de qué a qué y la necesidad en palabras del
  usuario. No lleva texto del vendedor (de marca, categoría o uso solo dice
  que son distintos; los valores están en `cambios`) ni texto libre del
  modelo, que ya no redacta nada.

## Qué campos se pueden cambiar

`campos_cambiables(contexto)` devuelve:

- los campos que el usuario **dijo** flexibles: una `Flexibilidad` con
  `origen == "dicha"` atada a cantidad, entregas, marca, categoría, uso o
  talle;
- cantidad y entregas, si la necesidad tiene un cronograma: la oferta tiene
  que cubrirlo.

Una flexibilidad `inferida` no habilita ningún cambio. Precio y moneda (el
mandato) nunca. Sin respuesta del usuario, ninguno.

Cómo una flexibilidad (texto libre) queda atada a un campo, sin cambiar el
contrato: `armar_contexto()` le pide al modelo el `campo` (enum) y, si es
`dicha`, una `cita` textual de la respuesta del usuario. El código:

- escribe el campo como etiqueta al principio de la descripción
  (`"Entregas: …"`); `campo_de_flexibilidad()` la lee. Un campo fuera de la
  lista (precio, moneda) queda como `"Otro: …"` y no habilita nada;
- baja a `inferida` una `dicha` cuya cita no está en la respuesta (sin
  distinguir mayúsculas, espacios ni comillas; al menos 4 caracteres).

Si el contrato sumara un campo `Flexibilidad.campo`, la etiqueta dejaría de
hacer falta.

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

## Intake con decisiones: repreguntar hasta tener todo

`intake.py` (lógica pura) y `agente_decisiones.py` (`AgenteDecisiones`, el
mismo protocolo `Agente` más `evaluar_intake()`). Valida **con
probabilidades** que estén los parámetros necesarios para comprar y, si no,
el proxy le vuelve a preguntar al usuario. Usa el adaptador `decisiones/`
(OpenAI Decisions `gpt-6-luna` o Jev); `python -m proxy` lo carga por
defecto si hay `OPENAI_API_KEY` o `TYPESAFE_API_KEY`
(`INTENCION_PROVEEDOR=decisiones|gemini`, ver
[proxy/README.md](../proxy/README.md#etapa-2-contexto-filtro-y-oportunidad)).

**Necesarios:** producto (categoría del catálogo, o "fuera del catálogo"),
cantidad, tope de precio y moneda, plazo. Además, los atributos que el
usuario mencionó (marca, talle, memoria, color, inalámbrico…), con si los
exige o los prefiere.

**Seleccionar, no generar.** El código encuentra los candidatos en las
palabras del usuario (montos, cantidades, fechas resueltas contra hoy,
monedas, marcas, talles, unidades) y arma preguntas de decisiones:
elecciones entre esos candidatos con una opción "no dicho" ("¿cuál es el
tope?", "¿para cuándo?"), predicados ("¿pide marca Nike?") y "¿lo exige o
lo prefiere?". El modelo solo elige; los valores salen del candidato.

**Umbrales (en código, calibrados con el set):** sobre la probabilidad del
valor elegido (las opciones que dan el mismo valor se suman):

| Probabilidad | Dicho por el usuario | Inferido (una unidad, `$` suelto) |
|---|---|---|
| ≥ 0,80 | resuelto | a confirmar |
| 0,60 a 0,80 | a confirmar | a confirmar |
| < 0,60, o "no dicho" | faltante: se repregunta | faltante |

Si el modelo sigue eligiendo un valor ya resuelto en un turno anterior, su
probabilidad no baja de la de antes (memoria entre turnos).

**Preguntas:** todas con plantilla (es/en según el pedido); el modelo no
redacta nada. La primera es el porqué más lo que falte; después, "me falta
saber…" (hasta 3 repreguntas, y si sigue faltando lo dice); al final,
confirmar la ficha, con lo inferido listado. Lo inferido solo se autoriza si
el usuario confirma. Una cantidad de 1 no es requisito (un cart es una
unidad); lo que el Guard todavía no chequea (campo `atributo`, operador
`minimo`) se muestra pero no va a la ficha autorizada
(`requisitos_verificables`).

**Contexto y oportunidades:** el intake y el contexto de un turno salen de
una sola llamada. La necesidad son las palabras del usuario; el cronograma
("10 el lunes y otros 10 el jueves") y las flexibilidades son juicios sobre
candidatos del código. La detección de oportunidades se delega en
`AgenteGemini` si hay `GOOGLE_API_KEY`; si no, no hay.

### Medirlo

```sh
# sin red: cobertura de candidatos y repetición de lo grabado
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m intencion.evaluar_intake --calibrar
# contra decisiones real (hasta --max-llamadas, 30 por defecto; graba las respuestas)
RUN_DECISIONES_TESTS=1 PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python \
  -m intencion.evaluar_intake --env-file .env
```

Set: [`tests/pedidos_intake.json`](tests/pedidos_intake.json), 12 pedidos
en castellano e inglés (zapatillas, notebooks, auriculares, fuera de
catálogo; completos, incompletos y ambiguos). Respuestas grabadas:
[`tests/respuestas_decisiones.json`](tests/respuestas_decisiones.json).

Resultado con OpenAI Decisions (`gpt-6-luna`, 2026-10-18, 30 llamadas):
estado en el turno 0 (preguntar o no) 60/60, **0 resueltos con valor
equivocado**, 11/12 casos perfectos (el 12.º sin grabar más allá del turno 0
por el tope de llamadas), repreguntas 3 de 4 esperadas (la que falta es la
del caso sin grabar), latencia por llamada 0,53 s de media (máx. 0,75 s).
Lo que mostró: una elección de una sola opción da HTTP 400 (sin números no
se pregunta el precio) y la confianza de un valor puede bajar de un turno a
otro sin que el usuario cambie nada (de ahí la memoria).
