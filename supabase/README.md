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
- `LIBRARY_PREVIEW_SECRET=<an independent high-entropy signing secret>`
- `NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN=<a URL-restricted public Mapbox token>`

During the active prototype phase, the workspace is intentionally open and does not use Supabase
Auth. The browser reads `/api/library`; that serverless route uses the Supabase service key only on
the server. Permanent Storage object paths remain private. Photo cards receive five-minute opaque
preview tokens, and `/api/library_preview` resolves those tokens server-side before reading the
private bucket. Exact camera coordinates and the public Mapbox browser token are therefore visible
to anyone who opens this prototype. Satellite tile requests also disclose viewed coordinates to
Mapbox. Reintroduce an authorization layer before treating the workspace as a hardened deployment.

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
