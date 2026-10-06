#!/usr/bin/env python3
"""Split a legacy schema-v5 Clothesline database into Setauket and Clothesline databases.

This is a one-shot operator script for the v0.6 -> v0.7 split. It is not part of the
Setauket CLI: after it has been run once there is no legacy schema left to migrate.

    python split_legacy_db.py \
        --source ~/Library/'Application Support'/Clothesline/clothesline.sqlite3 \
        --setauket-out ~/Library/'Application Support'/Setauket/setauket.sqlite3 \
        --clothesline-out ~/Library/'Application Support'/Clothesline/clothesline.sqlite3

Both stores must be importable, so run it from an environment where both `setauket`
and `clothesline` are installed:

    uv pip install --python .venv/bin/python -e '.[dev]' -e ../clothesline
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import sqlite_vec
from clothesline.storage import Store as ClotheslineStore

from setauket.models import EMBED_MODEL, EMBED_REVISION
from setauket.storage import Store as SetauketStore

LEGACY_VERSION = 5

# Insertion order matters: children follow parents. Both stores enforce immediate
# foreign keys on reopen, and foreign_key_check below proves the result is sound.
# Insertion order matters: children follow parents. Both stores enforce immediate
# foreign keys on reopen, and foreign_key_check below proves the result is sound.
# A table is copied into whichever target schema still has it, so identity and
# projects land in both while sessions land only in Setauket and messages only in Clothesline.
ALL_TABLES = [
    "harnesses", "agents", "projects",
    "sessions", "turns", "summaries", "memories", "chunks", "chunks_fts",
    "vec_history", "vec_memory", "jobs",
    "import_sources", "capture_sources", "capture_events",
    "agent_presence", "conversations", "messages", "message_deliveries", "messages_fts",
]
VECTOR_TABLES = {"vec_history", "vec_memory"}


def table_exists(db: sqlite3.Connection, schema: str, table: str) -> bool:
    return db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone() is not None


def columns(db: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA {schema}.table_info({table})")]


def count(db: sqlite3.Connection, schema: str, table: str) -> int:
    return db.execute(f"SELECT count(*) FROM {schema}.{table}").fetchone()[0]


def copy_table(db: sqlite3.Connection, table: str, targets: list[str]) -> dict:
    """Copy one legacy table into every target schema that still holds it."""
    shared = set(columns(db, "legacy", table))
    for target in targets:
        shared &= set(columns(db, target, table))
    names = ", ".join(f'"{name}"' for name in sorted(shared))
    for target in targets:
        if table in VECTOR_TABLES:
            db.execute(f"INSERT INTO {target}.{table}(rowid, embedding) "
                       f"SELECT rowid, embedding FROM legacy.{table}")
        else:
            projected = ", ".join(f"{name}" for name in sorted(shared))
            db.execute(f"INSERT INTO {target}.{table}({names}) SELECT {projected} FROM legacy.{table}")
    return {target: count(db, target, table) for target in targets} | {"columns": len(shared)}


def split(source: Path, setauket_out: Path, clothesline_out: Path) -> dict:
    for output in (setauket_out, clothesline_out):
        if output.exists():
            raise SystemExit(f"Refusing to overwrite existing file: {output}")
        if source.resolve() == output.resolve():
            raise SystemExit("An output path must differ from the source path")

    probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        version = probe.execute("PRAGMA user_version").fetchone()[0]
        pinned = dict(probe.execute("SELECT key,value FROM metadata").fetchall())
    finally:
        probe.close()
    if version != LEGACY_VERSION:
        raise SystemExit(f"Expected a schema v{LEGACY_VERSION} legacy database, found v{version}")
    # Vectors are copied verbatim, so the legacy indexes must match Setauket's pinned model.
    for key, expected in (("embedding_model", EMBED_MODEL), ("embedding_revision", EMBED_REVISION)):
        if pinned.get(key) != expected:
            raise SystemExit(f"Legacy {key} is {pinned.get(key)!r}, expected {expected!r}; "
                             "rebuild the indexes instead of splitting")
    # The metadata table is not copied: each store maintains its own pin on open.

    for output in (setauket_out, clothesline_out):
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    SetauketStore(setauket_out)
    ClotheslineStore(clothesline_out)

    db = sqlite3.connect(setauket_out)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    # Bulk load with deferred enforcement, then prove there are no violations.
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("ATTACH ? AS legacy", (str(source),))
    db.execute("ATTACH ? AS bus", (str(clothesline_out),))
    try:
        db.execute("BEGIN IMMEDIATE")
        report: dict[str, dict] = {}
        for table in ALL_TABLES:
            if not table_exists(db, "legacy", table):
                continue
            targets = [schema for schema in ("main", "bus") if table_exists(db, schema, table)]
            report[table] = {"legacy": count(db, "legacy", table)} | copy_table(db, table, targets)
        db.commit()
        db.execute("PRAGMA foreign_keys=ON")
        for schema in ("main", "bus"):
            violations = db.execute(f"PRAGMA {schema}.foreign_key_check").fetchall()
            if violations:
                raise SystemExit(f"Foreign key violations in {schema}: {violations[:5]}")
            integrity = db.execute(f"PRAGMA {schema}.integrity_check").fetchone()[0]
            if integrity != "ok":
                raise SystemExit(f"Integrity check failed for {schema}: {integrity}")
        db.commit()
    finally:
        db.close()  # Closing the connection releases the attached schemas.
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", type=Path, required=True, help="Legacy schema-v5 Clothesline database")
    parser.add_argument("--setauket-out", type=Path, required=True, help="Setauket database to create")
    parser.add_argument("--clothesline-out", type=Path, required=True, help="Clothesline database to create")
    args = parser.parse_args()
    report = split(args.source.expanduser().resolve(),
                   args.setauket_out.expanduser(), args.clothesline_out.expanduser())
    print(f"{'table':<18} {'legacy':>10} {'setauket':>10} {'clothesline':>12} {'cols':>5}")
    mismatched = []
    for table, row in report.items():
        if table in VECTOR_TABLES:
            row = {**row, "columns": 2}
        targets = [value for key, value in row.items() if key in {"main", "bus"}]
        if targets and any(value != row["legacy"] for value in targets):
            mismatched.append(table)
        flag = "  <-- MISMATCH" if table in mismatched else ""
        print(f"{table:<18} {row['legacy']:>10} {row.get('main', '-'):>10} "
              f"{row.get('bus', '-'):>12} {row['columns']:>5}{flag}")
    if mismatched:
        print(f"\nFAILED: row counts differ for {', '.join(mismatched)}", file=sys.stderr)
        return 1
    print("\nSplit complete. Both databases passed integrity and foreign key checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())