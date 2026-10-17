# Arquitectura: cómo funciona el Guard

> El porqué está en [CONTEXTO.md](CONTEXTO.md). Los modelos de datos propuestos,
> en [CONTRATOS_PROPUESTOS.md](CONTRATOS_PROPUESTOS.md).

## 1. El flujo AP2 real del sample, paso a paso

`world/` corre el sample oficial de AP2 **human-present / cards**, sin modificar
el protocolo. Esto es lo que pasa en el código (ver
[`roles/shopping_agent/agent.py`](../world/ap2/code/samples/python/src/roles/shopping_agent/agent.py)):

| # | Paso | Quién | Qué viaja | ¿Firmado? |
|---|---|---|---|---|
| 1 | El usuario pide algo en lenguaje natural | Usuario → Shopping Agent (`:8000`) | Texto | No |
| 2 | `find_products` | Shopping Agent → Merchant Agent (`:8001`) por A2A | `catalog_search` (texto), `shopping_agent_id`, `risk_data` | No |
| 3 | El comercio responde | Merchant → Shopping | Un `ap2.cart` por producto + `demo.offer_details` + `demo_scenario` | **No** (fase de navegación) |
| 4 | El agente recomienda; el usuario elige (`select_cart`) | Shopping Agent ↔ Usuario | `cart_id` | No |
| 5 | Dirección de envío y medio de pago | Shopping ↔ Credentials Provider (`:8002`) | — | — |
| 6 | `create_checkout` | Shopping → Merchant | `cart_id` → `ap2.checkout` con `checkout_jwt` y `checkout_hash` | **Sí, lo firma el comercio** (JWT ES256) |
| 7 | El usuario confirma el resumen | Usuario | — | — |
| 8 | `create_checkout_mandate` y `create_payment_mandate` | Shopping Agent | `CheckoutMandate` y `PaymentMandate` (SD-JWT) | **Sí, los firma el usuario** |
| 9 | Envío al Credentials Provider, pago y OTP (`123`) | Shopping → `:8002` → `:8003` | Mandatos firmados | Sí |
| 10 | Recibo de pago | Payment Processor → Shopping | `ap2.PaymentReceipt` | — |

Detalles que importan para el Guard:

- **El texto libre (y la inyección) viaja en `Cart.item_label`.** El modelo
  `Cart` de AP2 (`ap2/models/cart.py`) tiene solo `cart_id`, `item_label`,
  `amount` y `currency`.
- **Los carts no están firmados.** Lo primero que firma el comercio es el
  checkout del paso 6.
- **El `item_label` termina dentro del checkout firmado**: el comercio lo usa
  como `title` del ítem
  ([`merchant_agent/tools.py`](../world/ap2/code/samples/python/src/roles/merchant_agent/tools.py)).
  Por eso el Guard no puede "limpiar" el checkout: rompería la firma.
- **El proxy se entera de la elección en el paso 6**: el `cart_id` del
  `create_checkout` es el carrito elegido. Ahí, antes de que el usuario firme
  los mandatos (paso 8), el Guard tiene todo para emitir su veredicto.

> **Diferencia con el material del pitch.** El diagrama original dice "Usuario
> firma Intent Mandate". En el código del sample human-present **no se crea ni
> se firma un `IntentMandate`**: el pedido viaja como texto en `catalog_search`.
> El modelo `IntentMandate` existe en el SDK (`ap2/models/mandate.py`) y el
> README del sample lo menciona, pero el flujo no lo usa. Lo que el usuario
> firma son el `CheckoutMandate` y el `PaymentMandate` del paso 8. En la demo
> conviene decir "mandatos firmados" y no "Intent Mandate".

## 2. Dónde se ubica el Guard

Después de que el comercio responde por A2A y antes de que el usuario firme.

```mermaid
sequenceDiagram
    actor U as Usuario
    participant S as Shopping Agent :8000
    participant P as Proxy del Guard :8010
    participant G as Guard (guard/)
    participant M as Merchant Agent :8001
    participant C as Catálogo público :8011

    U->>S: pedido en lenguaje natural
    Note over S,G: (live) el Guard arma la Ficha con un LLM y el usuario la confirma
    S->>P: find_products (catalog_search)
    P->>M: reenvía sin cambios
    M-->>P: ap2.cart[] + demo.offer_details
    P->>C: GET /products/<sku>
    P-->>S: carts (versión limpia para el LLM: decisión abierta)
    S->>U: recomienda; el usuario elige
    S->>P: create_checkout(cart_id elegido)
    P->>M: reenvía sin cambios
    M-->>P: ap2.checkout (JWT firmado por el comercio)
    P->>G: evaluar(ficha, ofertas, cart_id elegido)
    G-->>P: Veredicto + Recibo
    alt aprobado / con_avisos
        P-->>S: checkout intacto (+ veredicto aparte)
        S->>U: resumen (+ avisos) → firma mandatos
    else escalar
        P-->>S: checkout intacto + evidencia: decide el humano
    else bloqueado
        P-->>S: tarea fallida con el motivo: no se presenta para firma
    end
```

Vista simplificada (la del pitch):

```
Usuario: pedido + ficha confirmada
        │
Shopping Agent ⇄ [GUARD / proxy :8010] ⇄ Merchant Agent :8001
        │                │
        │          Catálogo público :8011
        ▼
veredicto + recibo
        │
Usuario firma (o no) CheckoutMandate + PaymentMandate → pago
```

### Cómo se mete el proxy sin tocar AP2

El Shopping Agent lee la URL del comercio de `MERCHANT_AGENT_URL`
([`remote_agents.py`](../world/ap2/code/samples/python/src/roles/shopping_agent/remote_agents.py),
cambio listado en [`world/NOTICE.md`](../world/NOTICE.md)):

```sh
MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent bash world/run_world.sh B
```

El benchmark acepta lo mismo con `--merchant-url`.

> **Ojo con la Agent Card.** El cliente A2A del sample
> ([`common/payment_remote_a2a_client.py`](../world/ap2/code/samples/python/src/common/payment_remote_a2a_client.py))
> busca la Agent Card en `<base_url>/.well-known/agent-card.json` y arma el
> cliente a partir de ella. La card del comercio
> ([`merchant_agent/agent.json`](../world/ap2/code/samples/python/src/roles/merchant_agent/agent.json))
> dice `"url": "http://localhost:8001/a2a/merchant_agent"`. Si el proxy reenvía
> la card tal cual, es probable que las llamadas siguientes vayan directo a
> `:8001` y se salteen el proxy. Es lo primero que tiene que verificar Dev 3.
> La Agent Card no es un objeto firmado de AP2, así que el proxy puede servir
> una con su propia URL.

## 3. Reglas de diseño

1. **El Guard nunca altera objetos firmados** (checkout JWT del comercio,
   mandatos del usuario). Si los editara, la firma dejaría de valer. Guarda el
   original intacto para AP2, le da al LLM una versión limpia solo para
   razonar, y bloquea, marca o escala en lugar de "corregir".
2. **El extractor estructurado es el núcleo.** De la respuesta del comercio se
   toman solo campos tipados (nombre, marca, categoría, uso, precio, stock,
   talles), cada uno con su origen. El texto libre no llega al LLM que decide.
   Si no hay texto libre, la inyección no tiene por dónde entrar.
3. **Las decisiones críticas son determinísticas.** El LLM solo convierte el
   pedido en lenguaje natural en la ficha.

## 4. Componentes del Guard (`guard/`, Dev 2)

| Componente | Entrada | Salida | ¿LLM? |
|---|---|---|---|
| **Ficha del pedido** | Pedido en lenguaje natural | `Ficha` (obligatorios, preferidos, criterio) que el usuario confirma | **Sí**, único uso, con salida estructurada |
| **Extractor estructurado** | `ap2.cart[]` + `demo.offer_details` | `Oferta[]` con campos tipados y origen de cada dato | No |
| **Verificador** | `Oferta[]` + catálogo público (`:8011`) | Cada campo marcado como verificado, solo declarado o contradicho | No |
| **Chequeos** | `Ficha`, `Oferta[]` verificadas, `cart_id` elegido, política | `Chequeo[]` | No |
| **Veredicto** | `Chequeo[]` | `aprobado`, `con_avisos`, `escalar` o `bloqueado` | No |
| **Recibo** | Todo lo anterior + `checkout_hash` | `Recibo` firmado | No |

### De dónde salen los requisitos

- **Mandato firmado (AP2):** restricciones duras (tope, moneda, comercio). AP2
  ya las verifica; el Guard las vuelve a chequear antes de mostrar el carrito.
- **Política de compras de la empresa:** proveedores aprobados, montos, reglas
  (por ejemplo, "más de USD 5.000 requiere tres cotizaciones", "nada de
  suscripciones sin aprobación").
- **Ficha del pedido:** el pedido en lenguaje natural convertido en campos
  estructurados que el usuario confirma. Cada campo es obligatorio o preferido,
  y hay un criterio de elección. "Un requisito que no está escrito no se puede
  chequear."

En los datos de `world/`, la ficha de la demo ya existe:
`scenarios.json` → `intent` (`hard`, `preferred`, `criterion`).

### Los cuatro chequeos

1. **Cumplimiento:** cada obligatorio de la ficha contra el producto elegido.
2. **Evidencia:** cada dato queda como *verificado* contra el catálogo público,
   *solo declarado* por el vendedor, o *contradicho*.
3. **Integridad de la elección:** el Guard recalcula el ranking con los datos
   estructurados y el criterio de la ficha, y lo compara con lo que eligió el
   agente. Si el agente eligió algo peor en todo que otra opción disponible, es
   señal de manipulación o error, aunque esté dentro del tope. Este chequeo
   frena el ataque del paper sin necesidad de "entender" la inyección.
4. **Política:** proveedor aprobado, montos, reglas de la empresa.

### Veredictos

| Veredicto | Cuándo | Qué pasa |
|---|---|---|
| `aprobado` | Obligatorios cumplidos con datos verificados y elección coherente con el ranking propio | Se firma normalmente |
| `con_avisos` | Todo cumple, pero algún dato que no es obligatorio es solo declarado o no coincide un preferido | Se firma mostrando los avisos |
| `escalar` | Datos contradichos, un obligatorio respaldado solo por datos declarados, o elección dominada por otra opción | El proxy retiene el checkout; decide el humano con la evidencia (dashboard) |
| `bloqueado` | Se viola un obligatorio, el mandato o la política, o el checkout firmado no coincide con lo evaluado | No se presenta para firma |

**Regla de fondo:** el Guard puede bloquear lo que viola reglas explícitas,
pero **nunca aprueba en silencio lo que no pudo verificar**.

### Precios

El Guard no sabe si B miente; detecta inconsistencias. Por orden de
confiabilidad:

1. consistencia interna de la conversación (siempre; por ejemplo, el monto del
   cart contra el del checkout firmado);
2. catálogo público del comercio;
3. referencias externas (historial, otros comercios);
4. si no hay nada: "no verificable".

Un precio distinto al del catálogo **no se bloquea, se avisa** (puede haber
razones legítimas). Se bloquea lo que viola el mandato.

### Recibo de decisión

Registro firmado de qué se consideró, qué se filtró, qué se verificó, por qué
ganó el elegido y qué quedó sin verificar. Viaja junto a los mandatos **sin
modificarlos**: los referencia por hash (por ejemplo, el `checkout_hash` que ya
calcula el comercio). AP2 prueba *qué* se firmó; el recibo prueba *cómo* se
llegó ahí.

### Límites declarados

La calidad del producto, el cumplimiento de la entrega y la confiabilidad del
comercio no se pueden verificar antes de comprar. El Guard los declara como "no
verificables". A futuro lo cubre la reputación de comercios.

## 5. Qué produce `world/`

Es la fuente real de los contratos. Código en
[`demo_world/`](../world/ap2/code/samples/python/src/demo_world/).

### Respuesta del Merchant Agent a `find_products`

Cada búsqueda devuelve estos artefactos A2A (ver
[`catalog_agent.py`](../world/ap2/code/samples/python/src/roles/merchant_agent/sub_agents/catalog_agent.py)):

| Data part | Contenido | Comentario |
|---|---|---|
| `ap2.cart` (uno por producto) | `{cart_id, item_label, amount, currency}` | Modelo `Cart` de AP2. Es lo único que ve el Shopping Agent. **La inyección viaja en `item_label`.** |
| `demo.offer_details` | Lista de `{cart_id, sku, title, brand, category, use, price, currency, stock, sizes, description}` | Lo que **declara** el comercio. Puede ser falso (escenario B2). La inyección también aparece en `description`. |
| `demo_scenario` | Nombre del escenario activo | Mismo artefacto que `demo.offer_details`. |
| `risk_data` | String falso del sample | Sin uso para el Guard. |

Los `cart_id` son `cart_1`, `cart_2`, … en el orden de respuesta. El producto
del atacante va siempre último.

### Respuesta a `create_checkout`

Data part `ap2.checkout` con `checkout_jwt` (firmado por el comercio con ES256,
`kid: merchant-key-1`) y `checkout_hash` (SHA-256 del JWT, en base64url). El
Guard lo deja pasar intacto.

### Catálogo público (la verdad)

[`demo_world/public_catalog.py`](../world/ap2/code/samples/python/src/demo_world/public_catalog.py),
puerto `8011`, siempre sirve
[`data/catalog.json`](../world/ap2/code/samples/python/src/demo_world/data/catalog.json)
sin importar el escenario:

- `GET /health`
- `GET /products` → `{"merchant": {...}, "products": [...]}`
- `GET /products/<sku>` → el producto, o 404

Copia estática en [`fixtures/public_catalog.json`](../fixtures/public_catalog.json).

### Fixtures

`fixtures/scenario_<nombre>.json`, generados por `demo_world.export_fixtures`.
Claves: `scenario`, `title`, `description`, `intent`, `carts`,
`offer_details`, `attack`, `expected` (`best_sku`, `best_cart_id`,
`attack_target_cart_id`, `ap2_blocks_attack_target`).

### Contrato provisorio del benchmark

[`benchmark.py`](../world/ap2/code/samples/python/src/demo_world/benchmark.py) →
`evaluate_with_guard()`:

```python
def evaluate(intent: dict, offers_seen: dict, chosen_cart_id: str) -> dict:
    # offers_seen = {"carts": [...], "offer_details": [...]}
    # -> {"status": "aprobado" | "con_avisos" | "escalar" | "bloqueado", ...}
```

Se enchufa con `--guard paquete.modulo:funcion` (si no se indica la función,
busca `evaluate`). Acepta un `dict` o un modelo Pydantic (le hace
`model_dump()`). Solo `aprobado` y `con_avisos` cuentan como "llega a la
firma".

## 6. Puertos

| Puerto | Qué es | Dueño |
|---|---|---|
| 8000 | Shopping Agent (UI de ADK en `/dev-ui`) | sample (Dev 1) |
| 8001 | Merchant Agent, **el atacante** (`/a2a/merchant_agent`) | sample + `demo_world` (Dev 1) |
| 8002 | Credentials Provider | sample |
| 8003 | Merchant Payment Processor | sample |
| 8010 | Proxy del Guard (reservado) | Dev 3 |
| 8011 | Catálogo público | Dev 1 |
| — | Dashboard: puerto a definir | Dev 3 |
