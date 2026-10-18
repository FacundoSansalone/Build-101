-- Persistencia del Pre-Sign Guard en Supabase (proxy/persistencia.py y
-- proxy/subir_benchmark.py). Es solo registro: el Guard no lee de acá.
--
-- RLS activado y sin políticas: con la clave anon no se lee ni se escribe
-- nada. Solo entra el proxy con la service key.

create table if not exists public.veredictos (
  id               uuid primary key default gen_random_uuid(),
  evento_id        text unique not null,      -- Evento.id: el upsert no duplica
  ts               timestamptz not null,
  contexto_id      text,                      -- context_id de A2A
  escenario        text,
  guard_activo     boolean not null,
  evaluador        text,                      -- "guard" o "stub"
  fuente_catalogo  text,
  status           text not null
                   check (status in ('aprobado', 'con_avisos', 'escalar', 'bloqueado')),
  cart_elegido     text,
  cart_recomendado text,
  motivo           text,
  checkout_hash    text,
  recibo_id        text,                      -- rcb-…
  recibo_jws       text,                      -- JWS compacto ES256
  recibo_kid       text,                      -- guard-key-1
  veredicto        jsonb not null             -- Veredicto completo (chequeos + recibo)
);
create index if not exists veredictos_ts_idx on public.veredictos (ts desc);
create index if not exists veredictos_contexto_idx on public.veredictos (contexto_id);

create table if not exists public.aprobaciones (
  id           text primary key,              -- apr-…
  contexto_id  text not null,
  cart_id      text not null,
  centavos     bigint not null,               -- total firmado del checkout_jwt
  moneda       text not null,
  estado       text not null check (estado in ('pendiente', 'usada')),
  creada       timestamptz not null,
  usada        timestamptz,
  task_id      text
);

create table if not exists public.benchmark_corridas (
  id           text primary key,              -- nombre del archivo en results/
  started_at   timestamptz,
  finished_at  timestamptz,
  modelo       text,
  decider      text,
  trials       int,
  guard        text,
  meta         jsonb not null
);

create table if not exists public.benchmark_escenarios (
  corrida_id   text not null references public.benchmark_corridas (id) on delete cascade,
  escenario    text not null,                 -- baseline, A, B, B2, C
  titulo       text,
  ataque       jsonb,
  resumen      jsonb not null,                -- tasas de éxito del ataque antes/después
  intentos     jsonb not null,
  primary key (corrida_id, escenario)
);

alter table public.veredictos enable row level security;
alter table public.aprobaciones enable row level security;
alter table public.benchmark_corridas enable row level security;
alter table public.benchmark_escenarios enable row level security;
