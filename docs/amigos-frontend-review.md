# Amigos frontend review

Reviewed artifact: `amigosdeer.bundle`, commit `82c7cd6` (`docs: add developer handoff guide`).
The extracted repository passed **233 tests**, with 5 archive-dependent skips, and passed Ruff.

## Verdict

**HARD-BUT-DOABLE:** reuse the interaction model and information architecture, not the application code wholesale. The Amigos app is a local FastAPI/Jinja/SQLite alpha; DeerID is already ahead on verified Reveal ingestion, private Supabase storage/cataloging, cloud deployment, and exact camera mapping.

## Patterns to carry into DeerID

- Phone-first viewport, adaptive card grids, horizontally scrollable tables, and 42–44 px controls.
- A persistent top-level structure: Dashboard, Review, Deer, Stations/Cameras, and Duplicates.
- Review cards that combine a frame, confidence tier, top candidate, score, station, season, and label provenance.
- A review detail view that explains *why* each candidate ranked, keeps ambiguous matches human-only, and exposes explicit confirm/reject/defer/unusable actions.
- Deer profiles with confirmed sightings, season history, range/station history, aliases, antler records, "confused with" notes, and a reference gallery.
- Visible read-only/mutation state and a permanent decision audit trail.
- Vendored browser dependencies and graceful behavior on weak field connections.

## What not to merge directly

- SQLite `create_all` schema management; DeerID already uses versioned Supabase migrations.
- Local archive paths or direct filesystem media routes; DeerID media remains in private Supabase Storage behind same-origin proxies.
- Whole-frame deterministic identity as if it were visual re-identification. It remains useful archive evidence but cannot auto-confirm identity.
- The Amigos authentication boundary, which is localhost/token oriented. DeerID uses Supabase email/password sessions in server-managed HttpOnly cookies.

## Integration sequence

1. Keep the current operational dashboard, authenticated photo library, and private satellite camera map.
2. Add mobile navigation matching the Amigos information architecture once each route has a real Supabase-backed API.
3. Build the review queue against DeerID model predictions, human classifications, deer profiles, cameras, and media assets.
4. Build deer profile/detail views against season-scoped appearance profiles and confirmed sightings.
5. Add duplicates and audit-history views without weakening immutable source provenance or human confirmation rules.

This sequence retains the friend's strongest UX work while avoiding a second persistence/auth stack and preserving DeerID's more advanced field-ingestion foundation.
