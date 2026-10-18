-- Estado del gateway de producción (proxy/estado.py → EstadoSupabase).
-- Solo compras de integradores (/v1); la demo no escribe acá.
-- NO se aplica sola: la aplica el equipo a mano. Nadie la corre desde agentes.
-- RLS activado y sin políticas: solo entra el proxy con la service key.

create table if not exists public.gw_compras (
  id             text primary key check (id ~ '^cmp-[0-9a-f]{16}$'),
  integrador_id  text not null,
  comercio_id    text not null,
  referencia     text,
  estado         text not null check (estado in ('creada','esperando_contexto','negociando',
                   'esperando_decision','sin_oportunidad','bloqueada','aceptada','rechazada','vencida')),
  creada         timestamptz not null,
  actualizada    timestamptz not null,
  datos          jsonb not null
);
create index if not exists gw_compras_integrador_idx on public.gw_compras (integrador_id, creada desc);

create table if not exists public.gw_contextos (
  contexto_id  text primary key,
  compra_id    text not null references public.gw_compras (id) on delete cascade,
  comercio_id  text not null,
  creado       timestamptz not null default now()
);
create index if not exists gw_contextos_compra_idx on public.gw_contextos (compra_id);

create table if not exists public.gw_ofertas (
  contexto_id    text primary key,
  comercio_id    text not null,
  carts          jsonb not null,
  offer_details  jsonb not null,
  escenario      text,
  registradas    timestamptz not null
);

create table if not exists public.gw_ultimos_veredictos (
  contexto_id  text not null,
  cart_id      text not null,
  status       text not null check (status in ('aprobado','con_avisos','escalar','bloqueado')),
  centavos     bigint,
  moneda       text,
  ts           timestamptz not null,
  primary key (contexto_id, cart_id)
);

create table if not exists public.gw_aprobaciones (
  id             text primary key,
  contexto_id    text not null,
  cart_id        text not null,
  centavos       bigint not null,
  moneda         text not null,
  estado         text not null check (estado in ('pendiente','usada')),
  creada         timestamptz not null,
  usada          timestamptz,
  task_id        text,
  integrador_id  text,
  compra_id      text
);
create index if not exists gw_aprobaciones_ctx_idx on public.gw_aprobaciones (contexto_id, cart_id, creada desc);

create table if not exists public.gw_entregas_webhook (
  id             text primary key,
  integrador_id  text not null,
  tipo           text not null,
  compra_id      text,
  intentos       int not null,
  estado         text not null check (estado in ('pendiente','entregada','fallida','descartada')),
  status_http    int,
  error          text,
  destino_host   text,
  creada         timestamptz not null,
  actualizada    timestamptz not null
);
create index if not exists gw_entregas_integrador_idx on public.gw_entregas_webhook (integrador_id, creada desc);

alter table public.gw_compras            enable row level security;
alter table public.gw_contextos          enable row level security;
alter table public.gw_ofertas            enable row level security;
alter table public.gw_ultimos_veredictos enable row level security;
alter table public.gw_aprobaciones       enable row level security;
alter table public.gw_entregas_webhook   enable row level security;
revoke all on public.gw_compras, public.gw_contextos, public.gw_ofertas,
  public.gw_ultimos_veredictos, public.gw_aprobaciones, public.gw_entregas_webhook
  from anon, authenticated;
