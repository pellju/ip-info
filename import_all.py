#!/usr/bin/env python3
"""Import every dated table/ASN pair in a directory, oldest first."""

import argparse
import os
from pathlib import Path

from importer import import_snapshot, snapshot_pairs


def existing_dates(dsn: str):
    import psycopg

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            relation = cur.execute("SELECT to_regclass('public.snapshots')").fetchone()[0]
            if not relation:
                return set()
            legacy = cur.execute("SELECT to_regclass('public.announcements')").fetchone()[0]
            if legacy:
                raise RuntimeError(
                    "legacy full-snapshot schema detected; follow the README's schema-upgrade instructions"
                )
            return {row[0] for row in cur.execute("SELECT snapshot_date FROM snapshots")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("imports"))
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL", "postgresql://iptracker:iptracker@localhost/iptracker"),
    )
    args = parser.parse_args()

    pairs = snapshot_pairs(args.directory)
    if not pairs:
        print(f"No snapshot pairs found in {args.directory}")
        return
    present = existing_dates(args.dsn)
    imported = skipped = 0
    for snapshot_date, table, asns in pairs:
        if snapshot_date in present:
            print(f"Skipping {snapshot_date}: already imported")
            skipped += 1
            continue
        import_snapshot(args.dsn, table, asns)
        imported += 1
    print(f"Finished: {imported} imported, {skipped} already present")


if __name__ == "__main__":
    main()
