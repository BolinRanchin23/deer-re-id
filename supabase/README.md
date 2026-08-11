# DeerID Supabase catalog

The versioned migrations add a private relational catalog beside the existing private `tactacam-photos` Storage bucket. They do **not** move or rewrite existing Storage objects.

## Apply

Apply only after authenticating the Supabase CLI as a project owner for project `vypmpmlhuqwvrxypowqa`:

```bash
supabase link --project-ref vypmpmlhuqwvrxypowqa
supabase db push
```

Then set these Vercel production variables:

- `SUPABASE_CATALOG_ENABLED=true`
- `AUTH_ALLOWED_EMAILS=<comma-separated private-library account emails>`
- `SUPABASE_PUBLISHABLE_KEY=<the project's browser-safe publishable key>`
- `LIBRARY_PREVIEW_SECRET=<an independent high-entropy signing secret>`
- `NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN=<a URL-restricted public Mapbox token>`
- `PUBLIC_SITE_URL=https://deer-re-id.vercel.app`

The browser submits email/password to the same-origin `/api/auth` route. Supabase access and
refresh tokens are returned only as `HttpOnly; Secure; SameSite=Lax` cookies. Private library,
camera-map, and preview routes validate that Supabase session before using the server-side
service key. The Mapbox public token and exact camera coordinates are returned only after
authentication; satellite tile requests necessarily disclose the viewed coordinates to Mapbox.

Do not enable the catalog flag before the migration succeeds. Scheduling is intentionally unchanged.

## Validation

Run in the Supabase SQL editor after the first bounded sync:

```sql
-- All private tables must have RLS enabled.
select n.nspname as schema_name, c.relname, c.relrowsecurity
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'deerid' and c.relkind = 'r'
order by c.relname;

-- Browser roles must have no direct table privileges.
select grantee, table_name, privilege_type
from information_schema.role_table_grants
where table_schema = 'deerid' and grantee in ('anon', 'authenticated');

-- Only the server role may call private RPCs.
select routine_name, grantee, privilege_type
from information_schema.role_routine_grants
where routine_schema = 'public'
  and routine_name like 'deerid_%'
order by routine_name, grantee;

-- Cardinality only; do not print IDs, names, raw payloads, paths, or coordinates.
select
  (select count(*) from deerid.cameras) as cameras,
  (select count(*) from deerid.media) as media,
  (select count(*) from deerid.media_weather) as weather,
  (select count(*) from deerid.classification_jobs where status = 'pending') as pending_triage;

-- Every indexed media record is backed by a verified image/checksum identity.
select count(*) as invalid_media
from deerid.media
where image_sha256 !~ '^[0-9a-f]{64}$'
   or image_bytes <= 0
   or object_path = '';
```

Expected after a successful bounded sync:

- all `relrowsecurity` values are `true`;
- no `anon` or `authenticated` table grants;
- private RPC execution is granted only to `service_role` (owner/system entries may also appear);
- `invalid_media = 0`;
- the existing `/api/status` and `/api/preview` behavior remains healthy.

## Rollback

1. Set `SUPABASE_CATALOG_ENABLED=false` and remove/disable private-library access first.
2. Confirm the existing Storage archive and public status endpoint remain healthy.
3. Back up catalog metadata if it must be retained.
4. Revoke and drop all `public.deerid_*` RPCs, then drop the `deerid` schema.

The destructive SQL is intentionally not automated in a migration because it deletes normalized metadata. It never deletes the private Storage bucket or its image/metadata/checksum triplets.
