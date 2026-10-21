#!/usr/bin/env python3
"""Load Toggl Track detailed-report CSVs into toggl-archive/toggl.sqlite.

The raw CSVs in toggl-archive/raw/ are the source of truth and are never
modified. The database is rebuilt from them on every run, so re-running is
safe and picks up any newly added year.

Every CSV column is stored verbatim, plus the source file and line number,
so any row can be traced back to the export it came from.
"""
import csv
import glob
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(HERE, "toggl-archive")
RAW = os.path.join(ARCHIVE, "raw")
DB = os.path.join(ARCHIVE, "toggl.sqlite")

EXPECTED = ["Description", "Duration", "Member", "Email", "Project",
            "Tags", "Start date", "Stop date"]


def seconds(duration):
    # A timer still running at export time has Duration "-"; the raw text is
    # kept in the duration column and the seconds stay NULL.
    if duration.count(":") != 2:
        return None
    h, m, s = (int(x) for x in duration.split(":"))
    return h * 3600 + m * 60 + s


def main():
    files = sorted(glob.glob(os.path.join(RAW, "*.csv")))
    if not files:
        sys.exit(f"No CSVs in {RAW}")

    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
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
        CREATE TABLE sources (
            file TEXT PRIMARY KEY,
            rows INTEGER,
            total_seconds INTEGER,
            first_date TEXT,
            last_date TEXT
        );
    """)

    for path in files:
        name = os.path.basename(path)
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != EXPECTED:
                sys.exit(f"{name}: unexpected columns {reader.fieldnames}")
            n = total = 0
            dates = []
            for row in reader:
                n += 1
                secs = seconds(row["Duration"])
                total += secs or 0
                dates.append(row["Start date"])
                con.execute(
                    "INSERT INTO entries (source_file, source_line, description,"
                    " duration, duration_seconds, member, email, project, tags,"
                    " start_date, stop_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (name, reader.line_num, row["Description"], row["Duration"],
                     secs, row["Member"], row["Email"], row["Project"],
                     row["Tags"], row["Start date"], row["Stop date"]))
        con.execute("INSERT INTO sources VALUES (?,?,?,?,?)",
                    (name, n, total, min(dates), max(dates)))
        print(f"{name}: {n} rows, {total / 3600:.1f}h, {min(dates)} to {max(dates)}")

    con.commit()
    con.close()
    os.replace(tmp, DB)
    print(f"Wrote {DB}")


if __name__ == "__main__":
    main()
