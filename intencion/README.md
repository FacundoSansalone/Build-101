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
- descarta el cronograma del modelo si la respuesta nombra más fechas que
  las que trae: uno a medias es más laxo que lo que contó el usuario;
- arma la explicación para el usuario (`Oportunidad.por_que`) con una
  **plantilla**: qué cambia, de qué a qué y la necesidad en palabras del
  usuario. No lleva texto del vendedor (de marca, categoría o uso solo dice
  que son distintos; los valores están en `cambios`) ni texto libre del
  modelo, que ya no redacta nada;
- nombra la oferta con su **título público** (`titulo_publico()`: el de la
  fuente que lo verificó, catálogo, página pública o fabricante) o con
  ninguno, nunca con el que declara el vendedor; sin título, el dashboard
  muestra el SKU. El proxy lo vuelve a forzar antes de emitir la consulta;
- lee el valor de cada requisito como el Guard (`valor_ofrecido()` con
  `guard.chequeos.observado_de`): un atributo (memoria, peso…) por su nombre
  y con su unidad. Un atributo no es un campo flexible: una oferta que no
  cumple uno obligatorio no se propone.

## Qué campos se pueden cambiar

`campos_cambiables(contexto)` devuelve:

- los campos que el usuario **dijo** flexibles: una `Flexibilidad` con
  `origen == "dicha"` atada a cantidad, entregas, marca, categoría, uso o
  talle;
- cantidad y entregas, si la necesidad tiene un cronograma: la oferta tiene
  que cubrirlo. Si la cantidad cambia solo por el cronograma (el usuario no
  dijo que puede variar), la oferta trae el total del cronograma, ni más ni
  menos: "son 20 que entran el lunes" no habilita una oferta de 40.

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
que exporta `world/` (`demo_world.export_fixtures`) desde el escenario
`computadoras`.

## Intake con decisiones: repreguntar hasta tener todo

`intake.py` (lógica pura) y `agente_decisiones.py` (`AgenteDecisiones`, el
mismo protocolo `Agente` más `evaluar_intake()`). Valida **con
probabilidades** que estén los parámetros necesarios para comprar y, si no,
el proxy le vuelve a preguntar al usuario. Usa el adaptador `decisiones/`
(OpenAI Decisions `gpt-6-luna` o Jev); `python -m proxy` lo carga por
defecto si hay `OPENAI_API_KEY` o `TYPESAFE_API_KEY`
(`INTENCION_PROVEEDOR=decisiones|gemini`, ver
[proxy/README.md](../proxy/README.md#etapa-2-contexto-filtro-y-oportunidad)).

**Necesarios:** producto (categoría del catálogo, un tipo general de la
taxonomía de Google, o "fuera del catálogo público"), cantidad, tope de
precio y moneda, plazo. Además, los atributos que el usuario mencionó
(marca, talle, memoria, almacenamiento, color, inalámbrico…), con si los
exige o los prefiere. Un tamaño de datos sin decir de qué ("16 GB") no se
adivina, pero no es una cantidad ni un tope. Un producto fuera del catálogo
no es un error: la confirmación dice que no figura en el catálogo público y
que lo que declare el comercio se verifica con otras fuentes antes de la
firma (lo obligatorio que no se pueda verificar, el Guard lo escala).

**Un tipo general es un producto.** "computadoras" no es una categoría del
catálogo del comercio general (laptops, desktops, tablets…), pero sí de la
taxonomía (278, padre de 328, 325 y 4745); "cámaras" (142) frente a webcams
y cámaras digitales, igual. El código encuentra la categoría que nombra el
usuario (`taxonomia.palabras.nombradas`: sin modelo, y sin los complementos
ni los modificadores: "una mochila para la notebook" es una mochila, "un
cargador de notebook" y "a laptop charger" no nombran ninguna, pero "un par
de zapatillas" sí) y la de cada categoría del catálogo
(`intake.taxonomia_de_categoria`: el nombre del árbol, la tabla de palabras,
o un nombre del árbol con todas sus palabras). Si la del usuario es más
general que alguna del catálogo, el modelo ve la opción `general`; y queda
**resuelta y dicha**, con el ID de la taxonomía en `Parametro.eleccion` (el
valor es la palabra, "computadoras") y **sin requisito** (la rama la
verifica el Guard con `Ficha.categoria_taxonomia`), cuando:

- el modelo elige `general`;
- el modelo elige una del catálogo que achica lo que dijo (laptops para
  "computadoras") y ninguna palabra del usuario nombra esa ni una de su rama
  ("notebooks" sí: entonces vale laptops, con su requisito);
- el modelo no reconoce el producto (`no_dicha`, o duda dentro de esa
  rama) y el usuario lo nombró. Si el modelo apunta a otra cosa, aunque dude
  (fuera del catálogo, otra rama), no se fuerza. Si el usuario acaba de
  corregir el producto, vale la que nombra su última respuesta (o ninguna:
  se repregunta). Si es ajena a todo el catálogo (y se sabe la de todas),
  queda fuera del catálogo.

`fuera_de_catalogo` es un juicio del modelo y no se pisa ("un cargador para
la computadora").

**El pedido:** la búsqueda del agente del cliente o, en `/demo`, las
palabras del usuario más esa búsqueda (`"<usuario> / <búsqueda>"`, ver
`proxy/pedido_usuario.py`). El intake no distingue: es lo que dijo el
usuario, con los mismos umbrales y la misma confirmación.

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

"Lo prefiere" vuelve opcional un requisito (el plazo, un atributo): cuenta
solo con ≥ 0,80. Con menos es como "no dice": obligatorio (un atributo,
además, a confirmar). El modelo no afloja lo autorizado con una duda.

**Memoria entre turnos** (el turno anterior; si no está en la caché del
agente, se recalcula):

- si el modelo sigue eligiendo el mismo valor, su probabilidad no baja de la
  de antes;
- **lo que el usuario dijo y quedó resuelto no se pierde** porque una
  respuesta posterior no lo mencione. Visto de punta a punta el 2026-10-19:
  el plazo (lunes 26) pasó a faltante ante "Entran 10 trabajadores el lunes y
  otros 10 el jueves", la ficha se confirmó sin plazo y el Guard aprobó una
  oferta que llegaba tarde. Cambia solo si el usuario **lo corrige**: con un
  turno anterior, por cada necesario resuelto se pregunta
  `corrige_<parámetro>` ("¿en su última respuesta lo corrige o lo cambia?") y
  hace falta ≥ 0,80; sin eso, ni "no dicho" ni otro valor lo pisan. También
  si lo **precisa**: una categoría dentro de la rama de la anterior
  ("computadoras" → laptops) o, después de "fuera del catálogo", una que
  nombran sus palabras (no basta con que la elija el modelo);
- lo que estaba a confirmar no lo borra un "no dicho": un valor nuevo claro
  (≥ 0,60) lo reemplaza;
- se recuerda lo elegido (`Parametro.eleccion`), no lo derivado: del precio,
  el monto y si es por unidad (con la cantidad corregida, el total se
  recalcula); del plazo, la fecha (las entregas usan la cantidad de ahora).
  Un plazo exigido no pasa a preferido sin corrección (una firmeza clara
  solo lo vuelve más estricto);
- un atributo resuelto (marca, talle…) tampoco se pierde por una duda del
  modelo ni baja por una firmeza dudosa: solo un "no" claro (≤ 0,20; la
  pregunta dice "si lo descartó, lo negó o lo corrigió") lo saca.

**Fechas:** un día de la semana es el próximo, nunca hoy: dicho un lunes,
"el lunes" es el de la semana que viene, igual en el pedido y en las
respuestas (y que en el comercio, `demo_world.consulta`). En una misma
oración, un día que viene después de otra fecha y caería hasta 6 días antes
que ella es el de la semana siguiente: dicho un lunes, "10 el lunes y otros
10 el jueves" es el 26 y el 29, no el 22 (el cronograma usa las fechas de la
respuesta entera). No se corre entre oraciones, ante una alternativa ("el
viernes, si no el miércoles") ni por una fecha lejana: ahí queda el próximo,
del lado estricto. Una oración que sigue la cuenta de la anterior ("Entran 10
el lunes. Los otros 10 entran el jueves."; también "el resto", "los demás",
"otros 10") no corta la secuencia. Un día de la semana con su fecha es una sola fecha: "el
viernes 30 de octubre" no suma el próximo viernes (si el 30 es viernes; si
no, quedan las dos), "el jueves 29" es el primer jueves 29 desde hoy (este
mes o el que viene; el 29 no es cantidad ni monto) y "el miércoles de la
semana que viene" es el de esa semana. "Por la mañana" (o "a la", "de la",
"en la") no es mañana. Con decisiones real, "para el lunes" dicho un lunes queda
a veces a confirmar (0,59 a 0,88): es ambiguo, y la confirmación lo muestra.

**El tope:** la pregunta no ofrece los números de cosas ("20 computadoras",
"10 unidades": un producto de la tabla o una palabra de cantidad después),
que nunca son el tope. Con "20" entre las opciones, "hasta 20000 USD"
quedaba en 0,59 con decisiones real; sin ella, 0,80 a 0,95. Un precio por
cosa no es un número de cosas: "800 cada notebook", "100 por teclado" (con
`cada`, `por`, `c/u`, `per`, `each`, `la` o `el` en el medio) sí se ofrecen.
Un número pegado a la moneda ("USD20000", "u$s20.000", "20000USD") es un
monto en esa moneda, como "USD 20000".

**Preguntas:** todas con plantilla (es/en según el pedido); el modelo no
redacta nada. La primera es el porqué más lo que falte; después, "me falta
saber…" (hasta 3 repreguntas, y si sigue faltando lo dice); al final,
confirmar la ficha, con lo inferido listado. Lo inferido solo se autoriza si
el usuario confirma. Una cantidad de 1 no es requisito (un cart es una
unidad). Los atributos (campo `atributo`) y el operador `minimo` **van a la
ficha autorizada**: el Guard los verifica. `requisitos_verificables()` lee
lo que el Guard publica que sabe chequear (`guard.chequeos.CAMPOS_SOPORTADOS`
y `OPERADORES_SOPORTADOS`, hoy todo el contrato); lo que no estuviera ahí se
mostraría pero no se autorizaría.

**Taxonomía de Google:** `AgenteDecisiones.clasificar_taxonomia(descripcion)`
(o `categoria_taxonomia(descripcion, cliente)`) clasifica el pedido nivel por
nivel con `taxonomia.clasificar` y el mismo cliente (unas 3 llamadas, sobre
el subconjunto del repo) y devuelve el último nivel seguro. El proxy lo
llama al cerrar el intake y lo pone en `Ficha.categoria_taxonomia`. Sin
cliente de decisiones queda vacía. Es una propuesta: el Guard no depende de
ella.

**Contexto y oportunidades:** el intake y el contexto de un turno salen de
una sola llamada. La necesidad son las palabras del usuario; el cronograma
("10 el lunes y otros 10 el jueves") y las flexibilidades son juicios sobre
candidatos del código. Un par del cronograma es una cantidad y una fecha en
la misma cláusula; si en una respuesta queda alguna fecha sin su par ("Hay
dos grupos: 10 empiezan el lunes y 10 el jueves"), esa respuesta no aporta
cronograma: uno a medias (solo 10 el jueves) habilitaría una oferta que no
cubre el lunes. Cada flexibilidad lleva la etiqueta de su campo, como
las de Gemini (otra marca → `Marca: …`, otra cantidad → `Cantidad: …`, otra
fecha y entregas parciales → `Entregas: …`), y como no hay cita textual que
comprobar, es `dicha` solo con ≥ 0,80: con menos cuenta como `inferida` y no
habilita cambios. La detección de oportunidades se delega en
`AgenteGemini` si hay `GOOGLE_API_KEY`; si no, no hay.

### Medirlo

```sh
# sin red: cobertura de candidatos y repetición de lo grabado
PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m intencion.evaluar_intake --calibrar
# contra decisiones real (hasta --max-llamadas, 40 por defecto; graba las respuestas)
RUN_DECISIONES_TESTS=1 PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python \
  -m intencion.evaluar_intake --env-file .env
```

Set: [`tests/pedidos_intake.json`](tests/pedidos_intake.json), 17 pedidos
en castellano e inglés (zapatillas, notebooks, auriculares, fuera de
catálogo; completos, incompletos y ambiguos). Cinco son del 2026-10-19, un
lunes, con el catálogo del comercio general (cada caso puede traer su `hoy`
y sus `categorias`): computadoras y cámaras como tipo general, el pedido de
la prueba de punta a punta, uno sin tope que se repregunta y una corrección
del plazo. Respuestas grabadas:
[`tests/respuestas_decisiones.json`](tests/respuestas_decisiones.json).

Resultado con OpenAI Decisions (`gpt-6-luna`, 2026-10-19, 39 llamadas):
17/17 casos perfectos, valores finales 85/85, **0 resueltos con valor
equivocado**, repreguntas 5 de 5, cronograma 4/4, turno 0 84/85 (el de más:
"para el lunes" dicho un lunes, 0,59), latencia 0,46 s de media (máx.
0,78 s). Las preguntas de corrección: 0,88 en la corrección real ("Mejor
para el lunes"), ≤ 0,18 en el resto. Los umbrales siguen en 0,80 y 0,60
(`--calibrar`: de 0,70 a 0,85 en `resuelto` da lo mismo).

Antes (2026-10-18, 12 casos, 30 llamadas): estado en el turno 0 (preguntar o no) 60/60, **0 resueltos con valor
equivocado**, 11/12 casos perfectos (el 12.º sin grabar más allá del turno 0
por el tope de llamadas), repreguntas 3 de 4 esperadas (la que falta es la
del caso sin grabar), latencia por llamada 0,53 s de media (máx. 0,75 s).
Lo que mostró: una elección de una sola opción da HTTP 400 (sin números no
se pregunta el precio) y la confianza de un valor puede bajar de un turno a
otro sin que el usuario cambie nada (de ahí la memoria).
