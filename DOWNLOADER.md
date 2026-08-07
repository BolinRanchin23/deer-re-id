# Standalone Reveal Downloader

This downloader uses the unofficial Tactacam Reveal web API without Home Assistant. It logs into a Reveal account, pages through cloud-synced photos, and stores images, original API metadata, and SHA-256 checksums locally.

> This is unofficial software. Tactacam can change or disable the API at any time.

## Security

The CLI intentionally has **no `--password` option**, so a password cannot be exposed in shell history or the process list. By default it asks for the password using a hidden terminal prompt.

Do not put credentials in source code. `TACTACAM_PASSWORD` is supported for unattended operation, but a dedicated secret manager or locked-down service configuration is safer than a shell profile or committed `.env` file.

## Requirements

- Python 3.9 or newer
- A Tactacam Reveal account with an activated camera
- At least one photo synced to the Reveal cloud

No third-party Python packages are required.

## Use directly from the repository

```bash
cd /Users/partnersai/Projects/deer-re-id

# Confirm which cameras are visible to the account
python3 -m reveal_downloader cameras \
  --username YOUR_REVEAL_EMAIL

# Download all available cloud photos
python3 -m reveal_downloader sync \
  --username YOUR_REVEAL_EMAIL \
  --output /path/to/reveal-archive

# Check for new photos every five minutes
python3 -m reveal_downloader watch \
  --username YOUR_REVEAL_EMAIL \
  --output /path/to/reveal-archive \
  --interval 300
```

Each command prompts for the password without displaying it.

## Optional installation

Inside a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/reveal-downloader --help
```

## Useful options

- `--camera-id ID`: download only one camera
- `--page-size N`: photos requested per API page; default `100`
- `--max-pages N`: safety limit; `0` continues until there are no more pages
- `watch --interval N`: seconds between checks; default `300`

## Archive format

```text
reveal-archive/
└── CAMERA_ID/
    └── YYYY/MM/DD/
        ├── YYYYMMDDTHHMMSSZ_PHOTO_ID.jpg
        ├── YYYYMMDDTHHMMSSZ_PHOTO_ID.json
        └── YYYYMMDDTHHMMSSZ_PHOTO_ID.sha256
```

- `.jpg`: raw bytes downloaded from the photo URL
- `.json`: unmodified photo metadata returned by the Reveal API
- `.sha256`: integrity checksum of the downloaded image

Existing complete triplets are skipped, making repeated syncs duplicate-safe. Downloads are staged as `.part` files before being moved into place. A malformed photo is counted as failed without stopping the remaining downloads.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The automated suite uses fake transports and never contacts Tactacam or requires real credentials.

## Vercel + Supabase deployment

The repository includes a secured Vercel Python function at `GET /api/sync`. Vercel Cron invokes it and the function writes images, original metadata, and checksum completion markers to a private Supabase Storage bucket. It never relies on Vercel's ephemeral filesystem.

Configure these values in **Vercel → Project → Settings → Environment Variables** for the Production environment:

| Variable | Purpose |
|---|---|
| `CRON_SECRET` | Random value of at least 16 characters; Vercel sends it as a Bearer token |
| `TACTACAM_USERNAME` | Reveal account email |
| `TACTACAM_PASSWORD` | Current Reveal account password |
| `SUPABASE_URL` | Project URL such as `https://PROJECT.supabase.co`; a `/rest/v1/` URL is also normalized correctly |
| `SUPABASE_SECRET_KEY` | Server-side Supabase secret key; never expose it to a browser |
| `SUPABASE_BUCKET` | Optional private bucket name; defaults to `tactacam-photos` |
| `REVEAL_PAGE_SIZE` | Optional photos per page; defaults to `100` |
| `REVEAL_MAX_PAGES` | Optional page cap per invocation; defaults to `2` to bound runtime |

The function creates the private Storage bucket if it does not exist. It checks for a `.sha256` completion marker before downloading a photo, then uploads the image, JSON metadata, and checksum in that order. A missing marker causes a partial prior upload to be retried.

`vercel.json` schedules `/api/sync` once daily at 05:00 UTC because Vercel Hobby plans only permit one cron execution per day. On a Pro or Enterprise plan, change the schedule to `*/15 * * * *` for 15-minute polling.

For a manual production test, invoke the endpoint with the configured cron secret:

```bash
curl -H "Authorization: Bearer $CRON_SECRET" \
  https://YOUR-VERCEL-DOMAIN/api/sync
```

A successful response reports `downloaded`, `skipped`, and `failed` counts without returning any credentials.

## Current boundaries

- This is a Tactacam **cloud downloader**, not a direct connection to the physical camera.
- Cognito MFA and other interactive login challenges are reported but are not implemented.
- Pre-signed photo URLs can expire; rerun the sync to obtain current URLs.
- Vercel Hobby cron runs only once per day; more frequent polling requires Vercel Pro/Enterprise or another scheduler.
