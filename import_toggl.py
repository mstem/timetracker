#!/usr/bin/env python3
"""Load Toggl Track detailed-report CSVs into toggl-archive/toggl.sqlite.

The raw CSVs in toggl-archive/raw/ are the source of truth and are never
modified. The database is rebuilt from them on every run, so re-running is
safe and picks up any newly added year. Two files whose date ranges overlap
are refused, since that would count the same entries twice.

Every CSV column is stored verbatim, plus the source file and the line the
record starts on, so any row can be traced back to the export it came from.

Exports of a shared workspace include other members' entries. They are kept,
and per-member totals are in source_members; filter on email for one person.
"""
import csv
import glob
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(HERE, "toggl-archive")
RAW = os.path.join(ARCHIVE, "raw")
DB = os.path.join(ARCHIVE, "toggl.sqlite")

EXPECTED = ["Description", "Duration", "Member", "Email", "Project",
            "Tags", "Start date", "Stop date"]
DURATION = re.compile(r"^(\d+):(\d{2}):(\d{2})$")


class ImportError_(Exception):
    pass


def seconds(duration, where):
    # Toggl writes "-" for entries it stored without a duration. The raw text
    # is kept in the duration column and the seconds stay NULL.
    if duration == "-":
        return None
    m = DURATION.match(duration)
    if not m:
        raise ImportError_(f"{where}: unreadable Duration {duration!r}")
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def load(con, path):
    name = os.path.basename(path)
    members = {}
    n = total = 0
    first = last = None
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != EXPECTED:
            raise ImportError_(f"{name}: unexpected columns {reader.fieldnames}")
        start_line = reader.line_num + 1
        for row in reader:
            n += 1
            secs = seconds(row["Duration"], f"{name} line {start_line}")
            total += secs or 0
            d = row["Start date"]
            first = d if first is None or d < first else first
            last = d if last is None or d > last else last
            key = (row["Email"], row["Member"])
            r, s = members.get(key, (0, 0))
            members[key] = (r + 1, s + (secs or 0))
            con.execute(
                "INSERT INTO entries (source_file, source_line, description,"
                " duration, duration_seconds, member, email, project, tags,"
                " start_date, stop_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, start_line, row["Description"], row["Duration"],
                 secs, row["Member"], row["Email"], row["Project"],
                 row["Tags"], row["Start date"], row["Stop date"]))
            start_line = reader.line_num + 1
    con.execute("INSERT INTO sources VALUES (?,?,?,?,?)",
                (name, n, total, first, last))
    for (email, member), (r, s) in members.items():
        con.execute("INSERT INTO source_members VALUES (?,?,?,?,?)",
                    (name, email, member, r, s))
    return name, n, total, first, last, members


def check_overlaps(con):
    rows = con.execute(
        "SELECT a.file, b.file FROM sources a JOIN sources b"
        " ON a.file < b.file AND a.first_date <= b.last_date"
        " AND b.first_date <= a.last_date").fetchall()
    if rows:
        pairs = "; ".join(f"{a} and {b}" for a, b in rows)
        raise ImportError_(f"date ranges overlap, entries would be counted "
                           f"twice: {pairs}. Remove one file from {RAW}.")


def main():
    files = sorted(glob.glob(os.path.join(RAW, "*.csv")))
    if not files:
        sys.exit(f"No CSVs in {RAW}")

    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    try:
        con.executescript("""
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY,
                source_file TEXT NOT NULL,
                source_line INTEGER NOT NULL,
                description TEXT,
                duration TEXT,
                duration_seconds INTEGER,
                member TEXT,
                email TEXT,
                project TEXT,
                tags TEXT,
                start_date TEXT,
                stop_date TEXT
            );
            CREATE INDEX entries_start ON entries(start_date);
            CREATE INDEX entries_project ON entries(project);
            CREATE INDEX entries_email ON entries(email);
            CREATE TABLE sources (
                file TEXT PRIMARY KEY,
                rows INTEGER,
                total_seconds INTEGER,
                first_date TEXT,
                last_date TEXT
            );
            CREATE TABLE source_members (
                file TEXT,
                email TEXT,
                member TEXT,
                rows INTEGER,
                total_seconds INTEGER
            );
        """)
        report = [load(con, path) for path in files]
        check_overlaps(con)
        con.commit()
    except ImportError_ as e:
        con.close()
        os.remove(tmp)
        sys.exit(f"Import stopped, {DB} left unchanged: {e}")
    con.close()
    os.replace(tmp, DB)

    for name, n, total, first, last, members in report:
        span = f"{first} to {last}" if first else "no entries"
        print(f"{name}: {n} rows, {total / 3600:.1f}h, {span}")
        if len(members) > 1:
            for (email, member), (r, s) in sorted(
                    members.items(), key=lambda kv: -kv[1][1]):
                print(f"    {member} <{email}>: {r} rows, {s / 3600:.1f}h")
    print(f"Wrote {DB}")


if __name__ == "__main__":
    main()
