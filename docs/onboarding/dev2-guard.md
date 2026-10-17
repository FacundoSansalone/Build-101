# Onboarding: Dev 2 (`guard/`)

> Si sos un Claude Code leyendo esto: tu dev es el dueño de `guard/`. Leé
> también [`CLAUDE.md`](../../CLAUDE.md), [ARQUITECTURA.md](../ARQUITECTURA.md)
> y [CONTRATOS_PROPUESTOS.md](../CONTRATOS_PROPUESTOS.md).

## Tu misión en una frase

Escribir `evaluar()`: código determinístico que, dada la ficha, las ofertas
del comercio y el carrito elegido, devuelve un veredicto con evidencia y un
recibo firmado, sin que el texto libre del vendedor influya en nada.

## Qué ya existe

| Qué | Dónde |
|---|---|
| Lo que ve el Guard en cada escenario (pedido, carts, campos declarados, ataque, resultado esperado) | [`fixtures/scenario_*.json`](../../fixtures/) |
| La verdad del catálogo público | [`fixtures/public_catalog.json`](../../fixtures/public_catalog.json) (en vivo: `GET :8011/products/<sku>`) |
| La ficha de la demo ya estructurada | `fixtures/scenario_*.json` → `intent` (`hard`, `preferred`, `criterion`) |
| Cómo `world/` calcula qué viola cada producto real (sirve de referencia para el chequeo de cumplimiento) | `truth_violations()` en [`demo_world/benchmark.py`](../../world/ap2/code/samples/python/src/demo_world/benchmark.py) |
| Contrato provisorio con el benchmark | `evaluate_with_guard()` en el mismo archivo |
| Propuesta de modelos y firma de `evaluar()` | [CONTRATOS_PROPUESTOS.md](../CONTRATOS_PROPUESTOS.md) |
| Ejemplo de firma ES256 con `cryptography` (para el recibo) | `_create_jwt()` en [`merchant_agent/tools.py`](../../world/ap2/code/samples/python/src/roles/merchant_agent/tools.py) |

`guard/` hoy solo tiene un README.

## Tus tareas

Tests primero para cada chequeo, usando los fixtures.

1. **Paso 0 con el equipo**: contratos acordados en `contracts/`.
   *Terminado:* modelos commiteados en `main`.
2. **Extractor estructurado**: de `carts` + `offer_details` a `Oferta[]`.
   Toma solo campos tipados; el `item_label` y la `description` quedan
   descartados (se guardan para el recibo, nunca para decidir).
   *Terminado:* test que verifica que ningún campo de `Oferta` usado por los
   chequeos contiene el texto de la inyección en B.
3. **Chequeo de cumplimiento + veredicto mínimo.**
   *Terminado:* `evaluar()` devuelve `bloqueado` para B y A, y `aprobado`
   para baseline con NK-OUT-01. **Esto desbloquea el punto de control 3.**
4. **Adaptador del benchmark** (`evaluate(intent, offers_seen,
   chosen_cart_id) -> dict`).
   *Terminado:* el benchmark con `--source direct --decider target --guard
   ...` muestra "éxito con Guard" en 0% para B.
5. **Verificador contra el catálogo público** y chequeo de evidencia.
   *Terminado:* en B2, `category` y `use` de AX-URB-04 quedan `contradicho`;
   en C, el precio de NK-OUT-01 queda `contradicho` (145 vs 120).
6. **Integridad de la elección** (ranking propio) y **política**.
   *Terminado:* en B, el ranking propio pone NK-OUT-01 primero y el chequeo
   de integridad lo señala. Ver la decisión abierta 2 de los contratos (VL-OUT-03
   es más barato).
7. **Recibo firmado.**
   *Terminado:* el recibo incluye consideradas, filtradas con motivo,
   verificaciones, ranking, por qué ganó, qué quedó sin verificar y el
   `checkout_hash` si lo hay; la firma se verifica en un test.
8. **Ficha del pedido con LLM** (único uso de LLM, salida estructurada).
   *Terminado:* desde el `user_request` de los fixtures sale una `Ficha`
   equivalente a `intent`. Es lo último: para la demo y el benchmark alcanza
   con `Ficha.desde_intent()`.

### Resultado esperado por escenario

Ver la tabla en
[CONTRATOS_PROPUESTOS.md](../CONTRATOS_PROPUESTOS.md#5-qué-debería-dar-cada-escenario-para-los-tests-de-dev-2).
Resumen: `baseline` → `aprobado`; `A`, `B` y `B2` → `bloqueado` (o `escalar`
en B2); `C` → `escalar` (a confirmar).

## De qué dependés y quién depende de vos

- **Dependés de:** los contratos (paso 0) y los fixtures de Dev 1.
- **Dependen de vos:** Dev 3 (el proxy llama a `evaluar()` y el dashboard
  muestra `Veredicto` y `Recibo`) y Dev 1 (la columna "éxito con Guard").
  Mientras no esté tu parte, Dev 3 usa un stub con la misma firma: no la
  cambies sin avisar.

## Cómo probar tu parte sin depender de los otros

- **Todo contra `fixtures/`**: no hace falta levantar agentes ni tener API
  key. `evaluar()` no hace I/O; el catálogo lo cargás de
  `fixtures/public_catalog.json` en los tests.
- **Con el benchmark, sin servidores ni LLM:**

  ```sh
  cd world/ap2 && PYTHONPATH=../.. uv run --no-sync --package ap2-samples \
    python -m demo_world.benchmark --source direct --decider target \
    --trials 1 --guard guard.benchmark_hook:evaluate
  ```

  `--decider target` elige siempre el objetivo del atacante; `--decider best`
  elige siempre NK-OUT-01 (sirve para ver falsos positivos).
  (`guard.benchmark_hook` es el nombre propuesto; ajustalo a lo que acuerden.)

## Primer prompt sugerido para Claude Code

```text
Soy Dev 2 del repo Pre-Sign Guard y soy dueño de guard/. Leé CLAUDE.md,
docs/ARQUITECTURA.md, docs/CONTRATOS_PROPUESTOS.md y
docs/onboarding/dev2-guard.md. Mirá fixtures/scenario_B.json,
fixtures/scenario_B2.json, fixtures/scenario_C.json y
fixtures/public_catalog.json.

Los contratos acordados están en contracts/ (si todavía no están, usá la
propuesta y avisame que es provisoria).

Quiero arrancar con tests primero. Proponeme un plan (no escribas código
todavía) para:
1. el extractor estructurado (carts + offer_details -> Oferta[]), sin que el
   texto libre llegue a ningún chequeo;
2. el chequeo de cumplimiento y un evaluar() mínimo que dé "bloqueado" en A y
   B y "aprobado" en baseline con NK-OUT-01;
3. el adaptador evaluate(intent, offers_seen, chosen_cart_id) -> dict para el
   benchmark de world/.

Reglas: todo determinístico, sin LLM y sin I/O dentro de evaluar(). No
modifiques contracts/ ni world/. Si creés que hace falta cambiar un contrato,
pará y decímelo.
```

## Errores a evitar

- **Usar un LLM para decidir.** El único LLM es el de la ficha. Los chequeos,
  el ranking y el veredicto son código.
- **Leer `item_label` o `description` en un chequeo.** Ahí viaja la
  inyección. Si un chequeo depende de texto libre, el diseño se cae.
- **"Detectar" la inyección** (buscar palabras como "ignore" o "SYSTEM"). No
  es el producto; el Guard la vuelve irrelevante.
- **Aprobar en silencio lo que no se pudo verificar.** Si un dato es solo
  declarado, como mínimo es `con_avisos`.
- **Bloquear un precio distinto al del catálogo.** Se avisa o se escala; se
  bloquea solo lo que viola el mandato, un obligatorio o la política.
- **Devolver algo sin `status`.** El benchmark lo lee de ahí.
- **Hacer I/O dentro de `evaluar()`** (HTTP al catálogo, archivos): lo vuelve
  difícil de testear y lento en el proxy.
- **Hardcodear SKUs o `cart_id`** de la demo en la lógica. Los tests pueden
  usarlos; el Guard no.
