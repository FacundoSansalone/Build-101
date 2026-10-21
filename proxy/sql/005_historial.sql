-- Historial del panel del cliente (proxy/estado.py → EstadoSupabase): los
-- índices que usan los filtros nuevos de `GET /v1/compras` (fechas,
-- referencia, estado y el cursor `creada desc, id desc`), el resumen
-- (`GET /v1/resumen`), las lecturas de varias compras (`compra_id=in.(…)`) y
-- la purga de la retención (`AlmacenEstado.purgar`, por la fecha de cada tabla).
-- Aditiva: se aplica DESPUÉS de 003_gateway.sql y 004_serverless.sql y no borra
-- ni reescribe nada (`gw_compras_integrador_idx` de 003 queda: este lo cubre).
-- Idempotente (`if not exists`): correrla otra vez no rompe nada.
-- NO se aplica sola: la aplica el equipo a mano. Nadie la corre desde agentes.
-- Sin datos nuevos ni columnas: el código anda igual sin ella (más lento).

-- 1. El listado y el resumen: por integrador, la más nueva primero, a igual
--    fecha por id (el cursor es `(creada, id)`).
create index if not exists gw_compras_integrador_creada_id_idx
  on public.gw_compras (integrador_id, creada desc, id desc);

-- 2. El listado con `estado` (y los dos estados de una fila que se normaliza).
create index if not exists gw_compras_integrador_estado_idx
  on public.gw_compras (integrador_id, estado, creada desc, id desc);

-- 3. El listado con `referencia` (la del integrador, exacta).
create index if not exists gw_compras_integrador_referencia_idx
  on public.gw_compras (integrador_id, referencia, creada desc, id desc)
  where referencia is not null;

-- 4. La purga: lo creado antes del corte, tabla por tabla. Las de contextos y
--    esperas además se van en cascada con su compra.
create index if not exists gw_compras_creada_idx on public.gw_compras (creada);
create index if not exists gw_contextos_creado_idx on public.gw_contextos (creado);
create index if not exists gw_ofertas_registradas_idx on public.gw_ofertas (registradas);
create index if not exists gw_ultimos_veredictos_ts_idx on public.gw_ultimos_veredictos (ts);
create index if not exists gw_aprobaciones_creada_idx on public.gw_aprobaciones (creada);
create index if not exists gw_entregas_creada_idx on public.gw_entregas_webhook (creada);
create index if not exists gw_esperas_creada_idx on public.gw_esperas (creada);
create index if not exists gw_eventos_ts_idx on public.gw_eventos (ts);
