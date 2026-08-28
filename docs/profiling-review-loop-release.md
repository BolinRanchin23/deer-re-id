# Profiling Review Loop Release Runbook

This feature has a hard migration-before-code dependency. Do not deploy the Vercel application while the new RPCs are absent.

## 1. Verify the exact candidate

```bash
python -m unittest discover -s tests
node --check public/app.js
npx --yes vercel@latest build --prod --scope deer-intel-pro
supabase db push --linked --dry-run
```

Stop if any command fails or if the dry run lists anything other than `20260828023500_profiling_review_loop.sql`.

## 2. Apply the database migration first

```bash
supabase db push --linked
```

Before deploying application code, verify through the service-role database connection that these calls succeed and return the documented object shapes:

```sql
select public.deerid_pipeline_health();
select public.deerid_hd_review_progress();
select public.deerid_hd_review_queue_page(1, null, 'active');
```

The queue response must contain `items`, `has_more`, and `progress`. Do not continue if `deerid_pipeline_health` or `deerid_hd_review_queue_page` is missing or errors.

## 3. Deploy and test an immutable preview

Deploy the already-built candidate rather than rebuilding different source:

```bash
vercel deploy --prebuilt --scope deer-intel-pro
```

Against the returned preview URL, verify `/api/library`, all four profiling queues, location changes, crop correction, pending assignment confirm/undo, and browser console/network errors using real production data. Keep mutations bounded to designated test instances.

## 4. Promote only the tested preview

Promote the exact verified deployment. Do not run a new production build.

```bash
vercel promote <verified-preview-url> --scope deer-intel-pro
```

Then read back the production alias and repeat the non-destructive queue, health, and browser checks.

## Stop and rollback boundary

If migration verification fails, stop before Vercel deployment and repair with a new forward migration; never edit an applied migration. If preview verification fails, leave production on the existing deployment. If promotion verification fails, immediately restore the prior known-good Vercel deployment while preserving the append-only database evidence for diagnosis.
