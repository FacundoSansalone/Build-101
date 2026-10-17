# dashboard/ (Dev 3)

Panel de la demo del Pre-Sign Guard: el **escenario B lado a lado**, el flujo de
cada compra, el veredicto con sus chequeos, el recibo firmado (verificado en el
navegador), la ficha del pedido y el gráfico del benchmark "de X% a Y%".

Sin paso de build ni CDN: Starlette + HTML, CSS y JS vanilla (módulos ES).
Gráficos en SVG hecho a mano. Funciona sin internet.

## Cómo levantarlo

Desde la raíz del repo, con el entorno de `world/ap2`:

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m dashboard     # Linux/macOS: world/ap2/.venv/bin/python
# → http://localhost:8020
```

| Variable | Default | Para qué |
|---|---|---|
| `DASHBOARD_PORT` | `8020` | Puerto del panel |
| `DASHBOARD_HOST` | `127.0.0.1` | Interfaz. Abrilo como `localhost`: WebCrypto (verificar el recibo) no anda por IP de red sin HTTPS |
| `PROXY_URL` | `http://localhost:8010` | Proxy del Guard |

Atajos de teclado: `1`–`6` cambian de vista. El tema (auto, claro, oscuro) se
cambia con el botón al lado del nombre.

## Fuentes de datos

Arriba a la derecha se elige la fuente de eventos:

- **En vivo (proxy)**: los `Evento` llegan por SSE desde `GET {proxy}/guard/events`.
  El servidor del panel lo reenvía desde el mismo origen (`/api/proxy/...`), así
  que el proxy no necesita CORS. Si el proxy no responde, el panel dice
  **"proxy desconectado"** y reintenta solo.
- **Replay (archivo)**: lee un `.jsonl` (un `Evento` por línea) de
  `.logs/guard_events.jsonl` (lo escribe el proxy) o de `dashboard/demo/*.jsonl`.
  **Reproducir** los vuelve a pasar con su ritmo (entre 0,3 y 1,4 s por evento).

El resto sale de archivos del repo:

| Qué | De dónde |
|---|---|
| Resultados del benchmark | `results/*.json` y después `world/ap2/results/*.json`. Por defecto, el más nuevo con `meta.guard` no nulo; si no hay, el más nuevo |
| Ficha del pedido | `fixtures/scenario_<X>.json` → `intent`, con `Ficha.desde_intent()` del contrato; si la compra trae recibo, la ficha del recibo |
| Catálogo público | `fixtures/public_catalog.json` (para marcar qué parte del texto libre no figura en el catálogo y nombrar lo que se compró) |
| Clave pública del Guard | `GET {proxy}/guard/public-key`; si el proxy no responde, la parte pública de `.temp-db/guard_signing_key.pem`, o `dashboard/demo/guard_public_key.json` |

**No se inventan datos.** Si no hay eventos, resultados o recibo, la vista lo
dice con un estado vacío y explica cómo conseguirlos. Lo único que se muestra
sin compra en vivo es el `ap2.cart` del fixture del escenario, rotulado como
fixture. Si el proxy usa el evaluador `stub`, una franja avisa que los
veredictos son de prueba. Si el archivo del benchmark es de `--decider target`
o `best`, el panel avisa que no mide un modelo.

## Modo offline (plan B)

Para no depender del flujo en vivo el día de la demo:

1. **Grabar un ensayo real.** Con todo levantado y el Shopping Agent pasando por
   el proxy, hacer la compra de B con el Guard apagado y después con el Guard
   prendido (el interruptor del panel hace `POST {proxy}/guard/state`). Después,
   una de dos:
   - en la vista **Flujo**, botón **Guardar eventos (.jsonl)**, y mover el
     archivo a `dashboard/demo/`;
   - o copiar `.logs/guard_events.jsonl` a `dashboard/demo/ensayo-B.jsonl`.

   Commitear el archivo. No usar el nombre `guard_events.jsonl` en `demo/`.
2. **Clave pública para verificar el recibo sin proxy.** Si en la máquina de la
   demo está `.temp-db/guard_signing_key.pem`, no hace falta nada. Si no:
   `curl localhost:8010/guard/public-key > dashboard/demo/guard_public_key.json`
   mientras el proxy está levantado, y commitearlo (es solo la parte pública).
3. **Resultados del benchmark** en `results/` (ver `CLAUDE.md` → Decisiones).
4. El día de la demo: **Replay (archivo)** → elegir el ensayo. El panel recuerda
   la fuente y el archivo elegidos.

## Qué se ve en cada vista

1. **Momento clave.** Escenario B (se puede cambiar) en dos columnas con los
   mismos pasos alineados. *Sin Guard*: el `ap2.cart` elegido con el
   `item_label` marcado como texto libre del vendedor (y subrayado lo que no
   figura en su catálogo público), el `checkout_jwt` con header, payload y
   **firma ES256 del comercio**, el `checkout_hash` recalculado en el navegador,
   **AP2: OK** con el tope (`149 ≤ 150`, de `ap2_violaciones`), la línea de
   firma y los mandatos SD-JWT del usuario decodificados (si el proxy los
   registró), con el control de que referencian el mismo `checkout_hash`.
   *Con Guard*: el texto libre descartado, los campos tipados que sí se usan,
   el mismo checkout intacto, AP2 OK, el verificador contra `:8011`, los
   chequeos, el ranking propio, **bloqueado antes de la firma** y el recibo con
   su verificación. Arriba, la frase: "Todas las firmas son válidas y AP2 aprobó
   todo. Y aun así el usuario compró algo que no quería."
2. **Flujo.** Lista de compras (agrupadas por `contexto_id`) y la línea de
   tiempo de cada una, con los datos de cada evento. El interruptor del Guard
   está siempre en la barra.
3. **Veredicto.** Status grande con color, ícono y texto; motivo; chequeos
   agrupados por tipo (cumplimiento, evidencia, integridad, política, mandato)
   con esperado, observado y origen; ranking propio contra el elegido.
4. **Recibo.** Consideradas, filtradas con motivos, verificaciones contra el
   catálogo, por qué ganó, lo que quedó sin verificar, `checkout_hash` (contra
   el `ap2.checkout` y contra el SHA-256 del JWT), `kid` y **verificación de la
   firma en el navegador**: JWS compacto ES256 (r‖s) verificado con WebCrypto
   (ECDSA P-256 / SHA-256) contra la clave pública del Guard, y el payload
   firmado comparado con el recibo mostrado (`Recibo.payload_canonico()`:
   claves ordenadas, sin `firma` ni `kid`).
5. **Ficha.** El pedido original y sus requisitos (obligatorios y preferidos)
   como **confirmada**, con el criterio de elección.
6. **Benchmark.** Columnas por escenario: éxito del ataque solo con AP2 contra
   con Guard, con la etiqueta "X% → Y%", el caso B destacado y una tabla con
   todas las columnas (elige objetivo, AP2 frena, corridas, errores, elegidos).
   Muestra archivo, fecha, modelo, corridas y Guard. Si `with_guard` es `null`,
   lo dice.

## API del servidor

| Ruta | Qué devuelve |
|---|---|
| `GET /api/config` | `{"proxy_url": ...}` |
| `GET /api/results` | Archivos de resultados válidos, del más nuevo al más viejo |
| `GET /api/results/latest` | El JSON del benchmark elegido, con `_archivo` agregado (nombre, carpeta, fecha) |
| `GET /api/results/{results\|world}/{nombre}` | Un archivo puntual |
| `GET /api/fixtures` · `/api/fixtures/{escenario}` · `/api/fixtures/{escenario}/ficha` | Fixtures y la ficha armada con el contrato |
| `GET /api/catalog` | `fixtures/public_catalog.json` |
| `GET /api/replay` · `/api/replay/{nombre}` | Archivos `.jsonl` y sus `Evento` validados (las líneas inválidas se cuentan en `X-Lineas-Invalidas`) |
| `GET /api/public-key` | Clave pública del Guard (proxy o local) |
| `GET/POST /api/proxy/guard/state`, `GET /api/proxy/guard/events` (SSE), `GET /api/proxy/guard/events/history` | Reenvío al proxy |

Los nombres de archivo se buscan en una lista blanca armada listando las
carpetas: no hay path traversal.

## Tests

```sh
PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m pytest dashboard   # servidor + tests cruzados con node
node --test "dashboard/tests/js/*.test.mjs"                        # lógica JS pura
```

Los datos de los tests son sintéticos y se escriben en `tmp_path`. Hay dos
tests cruzados Python → JS: un recibo firmado en Python con
`Recibo.payload_canonico()` se verifica con el mismo código del navegador, y un
`CheckoutMandate` armado con el `MandateClient` del SDK de AP2 se decodifica
con el decodificador SD-JWT del panel.

## Notas para el equipo

- `busqueda_enviada` llega del proxy con `contexto_id: null` (se emite antes de
  que el comercio asigne el context_id). El panel la asocia a la compra del
  mismo escenario que empieza dentro de los 60 s siguientes y lo marca en la
  línea de tiempo.
- Los mandatos se leen de `reenviado.datos.mandatos` si el proxy los registra
  (un objeto `clave A2A → SD-JWT`, una lista o un string). Sin eso, la columna
  "Sin Guard" muestra que el checkout llegó intacto a la firma pero no puede
  mostrar los mandatos.
