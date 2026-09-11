#!/usr/bin/env python3
"""Bulk-import a dated routing table and ASN catalogue into PostgreSQL."""

from __future__ import annotations

import argparse
import csv
import ipaddress
import os
import re
from datetime import date, datetime
from pathlib import Path

DATE_RE = re.compile(r"(?:table|asns)-(\d{2})-(\d{2})-(\d{2}|\d{4})\.(?:txt|csv)$")


def date_from_filename(path: Path) -> date:
    if not ((path.name.startswith("table-") and path.suffix == ".txt") or
            (path.name.startswith("asns-") and path.suffix == ".csv")):
        raise ValueError(f"cannot extract date from {path.name!r}")
    match = DATE_RE.fullmatch(path.name)
    if not match:
        raise ValueError(f"cannot extract date from {path.name!r}")
    day, month, year = match.groups()
    fmt = "%d-%m-%Y" if len(year) == 4 else "%d-%m-%y"
    return datetime.strptime(f"{day}-{month}-{year}", fmt).date()


def parse_asn(value: str) -> int:
    value = value.strip().upper()
    if value.startswith("AS"):
        value = value[2:]
    number = int(value)
    if number < 0:
        raise ValueError("ASN cannot be negative")
    return number


def block_rows(path: Path):
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            fields = line.split()
            if not fields:
                continue
            if len(fields) != 2:
                raise ValueError(f"{path}:{line_number}: expected PREFIX ASN")
            try:
                prefix = ipaddress.ip_network(fields[0], strict=False)
                asn = parse_asn(fields[1])
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            yield str(prefix), asn


def asn_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"asn", "name", "class", "cc"}
        if not reader.fieldnames or not required.issubset({x.lower() for x in reader.fieldnames}):
            raise ValueError(f"{path}: expected columns asn,name,class,cc")
        keys = {key.lower(): key for key in reader.fieldnames}
        for line_number, row in enumerate(reader, 2):
            try:
                yield (
                    parse_asn(row[keys["asn"]]),
                    (row[keys["name"]] or "").strip(),
                    (row[keys["class"]] or "").strip() or None,
                    (row[keys["cc"]] or "").strip().upper()[:2] or None,
                )
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc


def snapshot_pairs(directory: Path):
    """Return validated (date, table path, ASN path) pairs in date order."""
    if not directory.is_dir():
        raise ValueError(f"not a directory: {directory}")
    tables, asns = {}, {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            snapshot_date = date_from_filename(path)
        except ValueError:
            continue
        target = tables if path.name.startswith("table-") else asns
        if snapshot_date in target:
            raise ValueError(
                f"multiple {path.name.split('-', 1)[0]} files for {snapshot_date}: "
                f"{target[snapshot_date].name} and {path.name}"
            )
        target[snapshot_date] = path
    missing_asns = sorted(set(tables) - set(asns))
    missing_tables = sorted(set(asns) - set(tables))
    if missing_asns or missing_tables:
        problems = []
        if missing_asns:
            problems.append("missing ASN CSV for " + ", ".join(map(str, missing_asns)))
        if missing_tables:
            problems.append("missing table TXT for " + ", ".join(map(str, missing_tables)))
        raise ValueError("; ".join(problems))
    return [(day, tables[day], asns[day]) for day in sorted(tables)]


def import_snapshot(dsn: str, blocks: Path, asns: Path, replace: bool = False) -> None:
    import psycopg

    blocks_date = date_from_filename(blocks)
    asns_date = date_from_filename(asns)
    if blocks_date != asns_date:
        raise ValueError(f"file dates differ: {blocks_date} and {asns_date}")

    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('ip-tracker-import'))")
            legacy = cur.execute("SELECT to_regclass('public.announcements')").fetchone()[0]
            if legacy:
                raise RuntimeError(
                    "legacy full-snapshot schema detected; back up the database and follow "
                    "the README's schema-upgrade instructions"
                )
            cur.execute(schema)
            latest = cur.execute(
                "SELECT snapshot_date FROM snapshots ORDER BY snapshot_date DESC LIMIT 1"
            ).fetchone()
            existing = cur.execute(
                "SELECT snapshot_date FROM snapshots WHERE snapshot_date=%s", (blocks_date,)
            ).fetchone()
            if existing and not replace:
                raise ValueError(f"snapshot {blocks_date} already exists (use --replace)")
            if existing:
                if latest[0] != blocks_date:
                    raise ValueError("only the latest snapshot can be replaced")
                # Undo the latest delta to recover the preceding current state.
                cur.execute("DELETE FROM prefix_changes WHERE snapshot_date=%s", (blocks_date,))
                cur.execute("DELETE FROM route_versions WHERE valid_from=%s", (blocks_date,))
                cur.execute("UPDATE route_versions SET valid_until=NULL WHERE valid_until=%s", (blocks_date,))
                cur.execute("DELETE FROM asn_versions WHERE valid_from=%s", (blocks_date,))
                cur.execute("UPDATE asn_versions SET valid_until=NULL WHERE valid_until=%s", (blocks_date,))
                cur.execute("DELETE FROM snapshots WHERE snapshot_date=%s", (blocks_date,))
                latest = cur.execute(
                    "SELECT snapshot_date FROM snapshots ORDER BY snapshot_date DESC LIMIT 1"
                ).fetchone()
            elif latest and blocks_date < latest[0]:
                raise ValueError(
                    f"snapshot {blocks_date} predates latest snapshot {latest[0]}; "
                    "delta snapshots must be imported chronologically"
                )
            cur.execute(
                "INSERT INTO snapshots(snapshot_date, blocks_filename, asns_filename) "
                "VALUES (%s, %s, %s)",
                (blocks_date, blocks.name, asns.name),
            )

            cur.execute("CREATE TEMP TABLE import_asns (asn bigint,name text,class text,country_code varchar(2)) ON COMMIT DROP")
            cur.execute("CREATE TEMP TABLE import_routes (prefix cidr,asn bigint) ON COMMIT DROP")
            with cur.copy("COPY import_asns(asn,name,class,country_code) FROM STDIN") as copy:
                for row in asn_rows(asns):
                    copy.write_row(row)
            with cur.copy("COPY import_routes(prefix,asn) FROM STDIN") as copy:
                for prefix, asn in block_rows(blocks):
                    copy.write_row((prefix, asn))
            cur.execute("CREATE INDEX import_routes_key_idx ON import_routes(prefix,asn)")
            cur.execute("CREATE INDEX import_asns_asn_idx ON import_asns(asn)")
            cur.execute("ANALYZE import_routes")
            cur.execute("ANALYZE import_asns")

            conflicting_asn = cur.execute(
                "SELECT asn FROM import_asns GROUP BY asn "
                "HAVING count(DISTINCT ROW(name,class,country_code))>1 LIMIT 1"
            ).fetchone()
            if conflicting_asn:
                raise ValueError(f"ASN {conflicting_asn[0]} has conflicting CSV rows")

            if latest:
                cur.execute(
                    "INSERT INTO prefix_changes(snapshot_date,prefix,old_asns,new_asns) "
                    "SELECT %s,COALESCE(old.prefix,new.prefix),"
                    "COALESCE(old.asns,'{}'::bigint[]), COALESCE(new.asns,'{}'::bigint[]) "
                    "FROM (SELECT prefix,array_agg(asn ORDER BY asn) asns FROM route_versions "
                    "      WHERE valid_until IS NULL GROUP BY prefix) old "
                    "FULL JOIN (SELECT prefix,array_agg(asn ORDER BY asn) asns FROM "
                    "           (SELECT DISTINCT prefix,asn FROM import_routes) r GROUP BY prefix) new "
                    "USING (prefix) WHERE old.asns IS DISTINCT FROM new.asns",
                    (blocks_date,),
                )

            cur.execute(
                "UPDATE route_versions old SET valid_until=%s WHERE valid_until IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM import_routes new "
                "                WHERE new.prefix=old.prefix AND new.asn=old.asn)",
                (blocks_date,),
            )
            cur.execute(
                "INSERT INTO route_versions(prefix,asn,valid_from) "
                "SELECT DISTINCT new.prefix,new.asn,%s FROM import_routes new "
                "WHERE NOT EXISTS (SELECT 1 FROM route_versions old "
                "                  WHERE old.prefix=new.prefix AND old.asn=new.asn "
                "                  AND old.valid_until IS NULL)",
                (blocks_date,),
            )
            cur.execute(
                "UPDATE asn_versions old SET valid_until=%s WHERE valid_until IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM import_asns new WHERE new.asn=old.asn "
                " AND new.name IS NOT DISTINCT FROM old.name "
                " AND new.class IS NOT DISTINCT FROM old.class "
                " AND new.country_code IS NOT DISTINCT FROM old.country_code)",
                (blocks_date,),
            )
            cur.execute(
                "INSERT INTO asn_versions(asn,name,class,country_code,valid_from) "
                "SELECT DISTINCT new.asn,new.name,new.class,new.country_code,%s FROM import_asns new "
                "WHERE NOT EXISTS (SELECT 1 FROM asn_versions old WHERE old.asn=new.asn "
                " AND old.valid_until IS NULL "
                " AND new.name IS NOT DISTINCT FROM old.name "
                " AND new.class IS NOT DISTINCT FROM old.class "
                " AND new.country_code IS NOT DISTINCT FROM old.country_code)",
                (blocks_date,),
            )
        conn.commit()
    print(f"Imported {blocks_date} from {blocks.name} and {asns.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blocks", type=Path, help="table-dd-mm-yyyy.txt")
    parser.add_argument("asns", type=Path, help="asns-dd-mm-yyyy.csv")
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL", "postgresql://iptracker:iptracker@localhost/iptracker"))
    parser.add_argument("--replace", action="store_true", help="replace an existing snapshot for this date")
    args = parser.parse_args()
    import_snapshot(args.dsn, args.blocks, args.asns, args.replace)


if __name__ == "__main__":
    main()
