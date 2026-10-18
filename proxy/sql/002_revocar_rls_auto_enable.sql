-- public.rls_auto_enable() la crea Supabase (activa RLS en las tablas nuevas
-- de public) y es SECURITY DEFINER. Expuesta por /rest/v1/rpc, los advisors la
-- marcan. Es una función de event trigger: el trigger sigue andando sin que
-- anon ni authenticated puedan ejecutarla.

revoke execute on function public.rls_auto_enable() from anon, authenticated, public;
