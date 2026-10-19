-- Gateway en varias instancias sin estado compartido (Vercel): lo que antes
-- vivía en la memoria de un proceso pasa a la base (proxy/estado.py →
-- EstadoSupabase). Las esperas del porqué y de la decisión, los eventos de
-- cada compra y la cola de webhooks; además, la base protege la fila de la
-- compra de las escrituras viejas.
-- Aditiva: se aplica DESPUÉS de 003_gateway.sql y no borra ni reescribe nada.
-- NO se aplica sola: la aplica el equipo a mano. Nadie la corre desde agentes.
-- RLS activado y sin políticas: solo entra el proxy con la service key.

-- 1. La espera abierta de la compra. Con una espera abierta, solo escribe la
--    fila de la compra la instancia que la retiene.
alter table public.gw_compras
  add column if not exists espera_id text
  check (espera_id is null or espera_id ~ '^esp-[0-9a-f]{16}$');

-- 2. Las esperas (el porqué y la decisión de una oportunidad). La instancia
--    que retiene la búsqueda la abre; la respuesta puede llegar por otra
--    (PATCH condicionado a `estado=eq.abierta&vence=gt.<ahora>`: gana uno solo)
--    y al vencer se cierra con otro PATCH condicionado a `estado=eq.abierta`.
create table if not exists public.gw_esperas (
  id              text primary key check (id ~ '^esp-[0-9a-f]{16}$'),
  compra_id       text not null references public.gw_compras (id) on delete cascade,
  tipo            text not null check (tipo in ('porque','decision')),
  estado          text not null check (estado in ('abierta','respondida','vencida')),
  oportunidad_id  text,                     -- solo en las de tipo decision
  vence           timestamptz not null,
  respuesta       jsonb,                    -- null con respondida = el usuario omitió
  creada          timestamptz not null,
  resuelta        timestamptz,              -- cuándo se respondió o venció
  check ((tipo = 'decision') = (oportunidad_id is not null)),
  check ((estado = 'abierta') = (resuelta is null))
);
-- Una sola espera abierta por compra: la segunda instancia que intenta abrir
-- otra recibe 409 (23505) y no retiene nada.
create unique index if not exists gw_esperas_abierta_uidx
  on public.gw_esperas (compra_id) where estado = 'abierta';
create index if not exists gw_esperas_compra_idx on public.gw_esperas (compra_id, creada desc);

-- 3. Aprobaciones idempotentes entre instancias: una sola pendiente por
--    contexto, cart y total firmado. El segundo POST (en otra instancia) choca
--    con el índice y lee la que ya existe. Es parcial: `on_conflict` de
--    PostgREST no sirve con él, el proxy inserta y ante 23505 lee la existente.
--    (003 nunca se aplicó con datos: no hay pendientes duplicadas que lo frenen.)
create unique index if not exists gw_aprobaciones_pendiente_uidx
  on public.gw_aprobaciones (contexto_id, cart_id, centavos, moneda)
  where estado = 'pendiente';
create index if not exists gw_aprobaciones_compra_idx
  on public.gw_aprobaciones (compra_id, creada);

-- 4. Eventos de las compras de integrador, en orden, para que cualquier
--    instancia los lea (`listar_eventos(compra_id, desde_orden)`). `id` es el
--    Evento.id: reinsertarlo no duplica. Solo compras de integrador: el
--    tráfico de la demo no se guarda acá.
create table if not exists public.gw_eventos (
  orden          bigint generated always as identity primary key,
  id             text not null unique,
  ts             timestamptz not null,
  integrador_id  text not null,
  compra_id      text not null check (compra_id ~ '^cmp-[0-9a-f]{16}$'),
  contexto_id    text,
  escenario      text,
  guard_activo   boolean not null,
  tipo           text not null,
  datos          jsonb not null default '{}'::jsonb
);
create index if not exists gw_eventos_compra_idx on public.gw_eventos (compra_id, orden);
create index if not exists gw_eventos_integrador_idx on public.gw_eventos (integrador_id, orden desc);

-- 5. Cola de webhooks (outbox) sobre la tabla de entregas de 003: el cuerpo
--    exacto (los mismos bytes en cada reintento, por eso text y no jsonb),
--    cuándo toca el próximo intento y hasta cuándo la tiene tomada una
--    instancia (la toma es un PATCH condicionado: una sola la envía).
alter table public.gw_entregas_webhook
  add column if not exists cuerpo text,
  add column if not exists proximo_intento timestamptz,
  add column if not exists tomada_hasta timestamptz;
create index if not exists gw_entregas_por_enviar_idx
  on public.gw_entregas_webhook (proximo_intento)
  where estado = 'pendiente';

-- 6. La fila de la compra, protegida en la base. Con varias instancias, una
--    escritura que llega tarde (con `actualizada` menor que la guardada) no
--    pisa a la más nueva, y una compra cortada por seguridad (`bloqueada`) o
--    vencida (`vencida`, los terminales de proxy/compras.py) no revive. La
--    escritura se descarta sin error (return null): el upsert responde bien,
--    y el PATCH con `return=representation` devuelve cero filas.
--    El paso a un estado terminal entra siempre, antes del chequeo de
--    `actualizada`: si una copia vieja (con un reloj adelantado) llegó antes
--    que el corte, descartar el corte dejaría seguir la conversación que había
--    que cortar. `actualizada` no retrocede (se queda con la mayor).
--    Si 004 ya estaba aplicada, se vuelve a correr este `create or replace`.
create or replace function public.gw_compras_proteger() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.estado in ('bloqueada', 'vencida') and old.estado not in ('bloqueada', 'vencida') then
    new.actualizada := greatest(new.actualizada, old.actualizada);
    return new;
  end if;
  if new.actualizada < old.actualizada then
    return null;
  end if;
  if old.estado in ('bloqueada', 'vencida') and new.estado is distinct from old.estado then
    return null;
  end if;
  return new;
end;
$$;

create or replace trigger gw_compras_proteger
  before update on public.gw_compras
  for each row execute function public.gw_compras_proteger();

-- Es una función de trigger: nadie la llama por /rest/v1/rpc (como
-- rls_auto_enable en 002, los advisors marcan las funciones expuestas).
revoke execute on function public.gw_compras_proteger() from anon, authenticated, public;

alter table public.gw_esperas enable row level security;
alter table public.gw_eventos enable row level security;
revoke all on public.gw_esperas, public.gw_eventos from anon, authenticated;
revoke all on sequence public.gw_eventos_orden_seq from anon, authenticated;
