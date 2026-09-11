# IP Tracker

A Python 3/Flask application that imports dated BGP prefix-to-origin snapshots and ASN metadata into PostgreSQL. It provides IP lookup (including all covering routes), current and historic ASN pages, exact-prefix history, and a feed of additions, withdrawals, and origin changes.

### Note that this tool has been vibecoded!

## How data is stored

Input files are complete daily snapshots, but PostgreSQL does not duplicate each complete snapshot. The first import establishes a baseline. Each later import is compared with the currently active records and stores only changes:

- A route that is unchanged creates no row.
- A new prefix-to-ASN mapping creates a `route_versions` row with `valid_from` set to the import date.
- A withdrawn mapping sets `valid_until` on its active row.
- An origin change closes the old mapping and creates the new mapping.
- ASN metadata uses the same validity-interval model in `asn_versions`.

Intervals are half-open: a row is active from `valid_from` up to, but not including, `valid_until`. A null `valid_until` means it is current. `prefix_changes` contains the added, withdrawn, or changed origins needed by the changes feed. Historical pages reconstruct any imported date from these intervals.

## Run with Docker

```bash
docker compose up -d --build
docker compose exec web python importer.py /data/table-13-06-26.txt /data/asns-13-06-26.csv
```

The included Compose file mounts the project directory read-only at `/data`, so input files in this directory are visible to the importer. The site is then available at <http://localhost:8000>.

## Import new snapshots

Each snapshot consists of two files with the same date:

- `table-dd-mm-yyyy.txt`, containing whitespace-separated `CIDR ASN` rows
- `asns-dd-mm-yyyy.csv`, containing the columns `asn,name,class,cc`

Two-digit years, such as `table-16-06-26.txt`, are also accepted. You do not need a special system folder.

With Docker, place the files anywhere under this project directory. The directory is mounted inside the web container as `/data`. For example, files placed in an `imports` subdirectory can be imported with:

```bash
mkdir -p imports
# Copy table-16-06-2026.txt and asns-16-06-2026.csv into imports/, then run:
docker compose exec web python importer.py \
  /data/imports/table-16-06-2026.txt \
  /data/imports/asns-16-06-2026.csv
```

With a local installation, the files can be in any location readable by the user running the importer. Pass either relative or absolute paths:

```bash
python importer.py \
  /srv/ip-data/table-16-06-2026.txt \
  /srv/ip-data/asns-16-06-2026.csv
```

The import command connects to the same PostgreSQL database as the web application through `DATABASE_URL` (or through `--dsn`). The running web application does not need to be restarted after an import.

Because imports are stored as deltas, import dates must be chronological. Importing a date older than the latest stored date is rejected. To correct data, replace the latest date first; replacing an older date would invalidate every later delta.

### Importing an existing date

A snapshot date is unique. If that date has already been imported, the command stops with an error and leaves the existing database data unchanged:

```text
ValueError: snapshot 2026-06-16 already exists (use --replace)
```

To intentionally correct or replace that date, rerun the command with `--replace`:

```bash
docker compose exec web python importer.py \
  /data/imports/table-16-06-2026.txt \
  /data/imports/asns-16-06-2026.csv \
  --replace
```

Only the latest imported date can be replaced. Replacement is transactional: the importer temporarily reverses that date's delta and applies the corrected input, and PostgreSQL restores the original data if anything fails. Duplicate identical rows within an input file are stored only once.

### Upgrading from the earlier full-snapshot schema

The temporal schema is intentionally incompatible with the earlier schema that used `announcements` and `asn_details`. The importer detects that schema and stops instead of mixing the two storage models.

For a development deployment where the original input files remain available, recreate the database volume and re-import the files in date order:

```bash
# This deletes the IP Tracker PostgreSQL volume and all imported database data.
# Keep the source table/asns files and make a database backup first if needed.
docker compose down -v
docker compose up -d --build
docker compose exec web python importer.py /data/table-13-06-26.txt /data/asns-13-06-26.csv
docker compose exec web python importer.py /data/table-14-06-26.txt /data/asns-14-06-26.csv
docker compose exec web python importer.py /data/table-15-06-26.txt /data/asns-15-06-26.csv
```

For a production database, take a PostgreSQL backup before changing the schema. Re-importing from the authoritative source files is the safest conversion because it verifies every delta in chronological order.

## Useful application paths

- <http://localhost:8000/> — imported snapshot overview
- <http://localhost:8000/ip> — IP-address lookup form
- <http://localhost:8000/ip?address=2001:4860:4860::8888> — direct IP lookup
- <http://localhost:8000/ip?address=2001:4860:4860::8888&snapshot=2026-06-13> — lookup in a particular snapshot
- <http://localhost:8000/asns> — ASN directory
- <http://localhost:8000/asns?q=Google> — filter ASNs by ASN, name, or country
- <http://localhost:8000/asn/15169> — an ASN's prefixes and history
- <http://localhost:8000/block?prefix=2001:4860::/32> — history of an exact prefix
- <http://localhost:8000/history> — recent additions, withdrawals, and origin changes

The changes page is paginated and can be filtered by date range, exact prefix, ASN, and change type. Filters can also be placed directly in the URL, for example:

```text
http://localhost:8000/history?date_from=2026-06-13&date_to=2026-06-30&asn=15169&kind=changed
```

Each ASN detail page also includes a paginated IP-block change timeline. It labels blocks as acquired or released by that ASN and identifies changes where the ASN remains present but the multi-origin set changes. The “Open full filtered history” link provides the date and change-type filters for deeper browsing.

## JSON API

The read-only API is available under `/api/v1`. It uses the existing database and requires no separate import. Dates use ISO `YYYY-MM-DD` format. Collection endpoints accept `page` and `per_page`; `per_page` defaults to 100 and is capped at 500.

```bash
curl http://localhost:8000/api/v1/snapshots
curl 'http://localhost:8000/api/v1/ip/2001:4860:4860::8888?snapshot=2026-06-13'
curl 'http://localhost:8000/api/v1/asns?q=Google&page=1&per_page=25'
curl 'http://localhost:8000/api/v1/asns/15169?snapshot=2026-06-13'
curl 'http://localhost:8000/api/v1/blocks/2001:4860::%2F32/history'
curl 'http://localhost:8000/api/v1/changes?date_from=2026-06-13&asn=15169&kind=changed&page=1'
```

Available endpoints:

- `GET /api/v1/snapshots`
- `GET /api/v1/ip/{address}`
- `GET /api/v1/asns`
- `GET /api/v1/asns/{asn}`
- `GET /api/v1/blocks/{prefix}/history` (URL-encode the prefix slash as `%2F`)
- `GET /api/v1/changes`

Errors use a consistent JSON shape, for example `{"error":{"code":400,"message":"..."}}`. No authentication or rate limiting is enabled by default; add both at the reverse proxy before exposing the API publicly.

## Run locally

Python 3.11+ and PostgreSQL 14+ are supported.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
createdb iptracker
export DATABASE_URL=postgresql:///iptracker
python importer.py table-13-06-26.txt asns-13-06-26.csv
python importer.py table-14-06-26.txt asns-14-06-26.csv
flask --app app run --port 8000
```

The importer accepts both two- and four-digit years. A pair must carry the same date. Imports are transactional and protected by a PostgreSQL advisory lock, so a failed parse leaves no partial snapshot. Exact duplicate input rows are ignored. Use `--replace` to atomically replace the latest imported date.

```bash
python importer.py table-15-06-26.txt asns-15-06-26.csv --dsn "$DATABASE_URL"
```

The initial import creates the schema automatically. Import snapshots in chronological order. Prefix files contain whitespace-separated `CIDR ASN` rows. ASN CSV files require the columns `asn,name,class,cc`.

## Production notes

- Put the web service behind TLS and use a strong database password.
- The supplied tables are large. PostgreSQL storage will be several gigabytes after indexes and multiple snapshots; monitor disk space.
- The ASN page intentionally caps the visible prefix list at 1,000, while counts remain exact.
- Back up the PostgreSQL volume; the original input files alone can also reconstruct the database.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
