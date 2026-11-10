#!/usr/bin/env python3
"""
Re-file Evens timesheets that held CTFG, personal or other non-Evens work under
the current rules: per-title splits of multi-project folders and the CTFG title
rule (common.CTFG_TITLE).

A timesheet is in scope when the tracker sent it (a "Sent [...]" line in
tracker.log), it sits on a project of the Evens customer, and it came from one
of the --keys folders or holds an entry whose title names CTFG work.

Each such timesheet keeps its exact Kimai duration. The duration is divided
across the timesheet's window titles in proportion to their seconds in that
day's log, each title is resolved under the current rules, and the pieces are
summed per target project. A timesheet whose pieces all land where it already
is stays untouched. Kimai's day totals cannot change, and no calendar
meeting is synced or re-clipped along the way.

  python3 refile_evens.py 2026-09-25 2026-11-08            # plan only, changes nothing in Kimai
  python3 refile_evens.py 2026-09-25 2026-11-08 --apply    # snapshot, delete, post

The plan run asks the model about titles it has not seen and saves its answers
to project_mappings.json, as the tracker's own send would.
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys
import tempfile
import urllib.error

import ai_matcher
import common
import tracker

SENT = re.compile(r"Sent \[(\d{4}-\d\d-\d\d)\] '(.*)' \((.*)\) → (\d+)m$", re.S)
LOG_RECORD = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ")
DEFAULT_KEYS = ["~/projects/hub", "~/projects/daily-briefing"]


def sends(first: str, last: str, path: str = None) -> list:
    """(day, description, key, minutes) per Sent line. A window title with a
    line break in it carries over onto the following log lines, so an
    unfinished Sent line takes up to three more."""
    out = []
    with open(path or tracker.LOG_FILE, errors="replace") as f:
        lines = f.read().split("\n")
    i = 0
    while i < len(lines):
        text = lines[i]
        m = SENT.search(text)
        if not m and "INFO Sent [" in text:
            for extra in range(1, 4):
                # A line with its own timestamp is the next log record, so the
                # unfinished one ended there.
                if i + extra >= len(lines) or LOG_RECORD.match(lines[i + extra]):
                    break
                text += "\n" + lines[i + extra]
                m = SENT.search(text)
                if m:
                    i += extra
                    break
            if not m:
                print(f"  ! unreadable Sent line, skipped: {lines[i][:100]}")
        if m and first <= m.group(1) <= last:
            out.append((m.group(1), m.group(2), m.group(3), int(m.group(4))))
        i += 1
    return out


def timesheets(config: dict, first: str, last: str) -> list:
    end = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat()
    out = []
    for page in range(1, 100):
        try:
            rows = common.kimai_request(
                config, "GET",
                f"/timesheets?begin={first}T00:00:00&end={end}T00:00:00&size=500&page={page}&full=true") or []
        except urllib.error.HTTPError as e:
            # Kimai answers a page past the end with 404. Any other error
            # would make the plan look empty, so it stops the run.
            if e.code == 404 and page > 1:
                break
            raise
        out += rows
        if len(rows) < 500:
            break
    return out


POSTED = "refile-evens-posted.json"


def load_posted(folder: str = common.BASE_DIR) -> set:
    """Ids of the timesheets earlier --apply runs posted. Their descriptions and
    lengths can match some other Sent line, so a later run skips them instead of
    splitting them again."""
    path = os.path.join(folder, POSTED)
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return set(json.load(f))


def save_posted(ids: set, folder: str = common.BASE_DIR):
    with open(os.path.join(folder, POSTED), "w") as f:
        json.dump(sorted(ids), f)


def pick_sheet(hits: list, mins: int):
    """The timesheet a Sent line produced, among those with its description and
    a length within a minute of it. When those sit on different projects,
    picking by closeness can claim the wrong one, so none is picked."""
    if len({t["project"]["id"] for t in hits}) > 1:
        return None
    return min(hits, key=lambda t: abs(t["duration"] // 60 - mins))


def movable(sheet: dict) -> bool:
    """An exported timesheet already went into a report, so it stays put."""
    return not sheet.get("exported")


def day_log(day: str):
    path = os.path.join(common.LOGS_DIR, f"{day}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def group_entries(day_entries: list, key: str, day_keys: set) -> list:
    """The log entries a sent key was summed from. A title key holds the entries
    with that title; a base key holds the entries whose title key did not go
    out on its own that day."""
    base = key.split(common.TITLE_SEP, 1)[0]
    out = []
    for e in day_entries:
        if common.entry_key(e) != base:
            continue
        title_key = base + common.TITLE_SEP + common.normalize_title(e.get("window", ""))
        if common.TITLE_SEP in key:
            if title_key == key:
                out.append(e)
        elif title_key not in day_keys:
            # Whatever no title key of that day claimed went out under the base.
            out.append(e)
    return out


def plan(config: dict, first: str, last: str, keys: list, evens_project: int) -> list:
    projects = common.list_kimai_projects(config)
    project_customer = {p["id"]: p.get("customer") for p in projects}
    names = {p["id"]: p["name"] for p in projects}
    if evens_project not in project_customer:
        sys.exit(f"Project {evens_project} is not a visible Kimai project; pass --evens-project with an active Evens project id")
    evens = project_customer[evens_project]
    keys = {common.normalize_key(k) for k in keys}

    by_day_desc = collections.defaultdict(list)
    posted = load_posted()
    for t in timesheets(config, first, last):
        if t["id"] in posted:
            continue
        by_day_desc[(t["begin"][:10], t.get("description") or "")].append(t)

    sent = sends(first, last)
    day_keys = collections.defaultdict(set)
    for day, _, key, _ in sent:
        day_keys[day].add(key)

    logs = {}
    out = []
    claimed = set()
    for day, desc, key, mins in sent:
        # Several keys can send the same description on one day ("CTFG - Google
        # Chrome - Matt" from three domains), so the logged minutes pick the
        # timesheet too, and each timesheet answers for one send only.
        hits = [t for t in by_day_desc.get((day, desc), [])
                if t["id"] not in claimed and abs(t["duration"] // 60 - mins) <= 1]
        if not hits:
            continue
        sheet = pick_sheet(hits, mins)
        if sheet is None:
            print(f"  ! {day} '{desc}' {mins}m: {len(hits)} timesheets fit on different projects, skipped")
            continue
        claimed.add(sheet["id"])
        if project_customer.get(sheet["project"]["id"]) != evens:
            continue
        if not movable(sheet):
            print(f"  ! {day} #{sheet['id']} '{desc}': exported, left as it is")
            continue
        if day not in logs:
            logs[day] = day_log(day)
            if logs[day] is None:
                print(f"  ! {day}: no log file, its sends are skipped")
        if logs[day] is None:
            continue
        entries = group_entries(logs[day], key, day_keys[day])
        base = key.split(common.TITLE_SEP, 1)[0]
        if not entries:
            continue
        if base not in keys and not any(common.CTFG_TITLE.search(common.normalize_title(e.get("window", "")))
                                        for e in entries):
            continue

        mappings = ai_matcher.classify_and_save(config, entries)
        split = common.split_by_title(entries, config, mappings)

        current = (sheet["project"]["id"], sheet["activity"]["id"])
        whole = base in keys

        def resolve(k):
            # Outside the multi-project folders only the CTFG rule moves time;
            # everything else stays where it was sent.
            if not whole and not common.title_rule(k, config):
                return current
            return common.resolve_mapping(k, mappings, config["default_project_id"],
                                          config.get("default_activity_id"), config, project_customer)

        total = sum(e["seconds"] for e in split) or 1
        pieces = collections.defaultdict(lambda: {"seconds": 0.0, "start": None, "titles": collections.Counter()})
        for e in split:
            target = resolve(common.entry_key(e))
            p = pieces[target]
            p["seconds"] += sheet["duration"] * e["seconds"] / total
            p["titles"][e.get("window", "")] += e["seconds"]
            p["start"] = min(p["start"] or e["start"], e["start"])
        whole_seconds = dict(zip(pieces, split_seconds(sheet["duration"], [p["seconds"] for p in pieces.values()])))
        # Checked after slivers fold, so a move that rounds to nothing is no move.
        if sum(x for t, x in whole_seconds.items() if t != current) < 30:
            continue
        out.append({"sheet": sheet, "key": key, "day": day, "pieces": [
            {"project": proj, "activity": act, "project_name": names.get(proj, str(proj)),
             "seconds": whole_seconds[(proj, act)], "start": p["start"],
             "description": p["titles"].most_common(1)[0][0],
             "titles": {t: round(s / 60, 1) for t, s in p["titles"].most_common()}}
            for (proj, act), p in pieces.items()]})
    return out


def show(moves: list):
    moved = collections.defaultdict(float)
    for m in moves:
        s = m["sheet"]
        print(f"\n{m['day']}  #{s['id']}  {s['duration'] / 60:.1f}m  {s['project']['name']}  "
              f"({m['key']})  '{s.get('description')}'")
        for p in sorted(m["pieces"], key=lambda p: -p["seconds"]):
            print(f"    -> {p['project_name']:28} {p['seconds'] / 60:6.1f}m")
            for t, mins in list(p["titles"].items())[:8]:
                print(f"         {mins:5.1f}m  {t[:90]}")
            if p["project"] != s["project"]["id"]:
                moved[p["project_name"]] += p["seconds"] / 3600
    print("\nHours leaving Evens projects, by destination:")
    for name, h in sorted(moved.items(), key=lambda kv: -kv[1]):
        print(f"  {name:28} {h:.2f}h")


def is_gone(config: dict, sheet_id: int):
    """True when Kimai says 404, False when it returns the timesheet, None when
    it gives no answer."""
    try:
        common.kimai_request(config, "GET", f"/timesheets/{sheet_id}")
    except urllib.error.HTTPError as e:
        return True if e.code == 404 else None
    except Exception:
        return None
    return False


def saved_anyway(config: dict, body: dict, posted: list, original: int) -> list:
    """Timesheets the lost post created: same start, project and description,
    and neither the original nor a piece already known."""
    known = {new.get("id") for new in posted if new} | {original}
    try:
        rows = common.kimai_request(
            config, "GET", f"/timesheets?begin={body['begin']}&end={body['end']}&full=true") or []
    except Exception:
        return []
    return [t for t in rows if t.get("id") not in known
            and (t.get("project") or {}).get("id") == body["project"]
            and (t.get("description") or "") == body["description"]
            and (t.get("begin") or "")[:19] == body["begin"][:19]]


def take_back(config: dict, posted: list, folder: str = common.BASE_DIR):
    """Delete the pieces just posted. Any that will not go are recorded as
    posted, so a later run leaves them alone, and named for a person to remove."""
    stuck = []
    for new in posted:
        if new and new.get("id"):
            try:
                common.kimai_request(config, "DELETE", f"/timesheets/{new['id']}")
            except Exception:
                stuck.append(new["id"])
    if stuck:
        save_posted(load_posted(folder) | set(stuck), folder)
        print(f"  could not take back {stuck}; delete them in Kimai by hand")


def split_seconds(duration: int, weights: list) -> list:
    """Whole seconds per weight that add up to duration exactly; the rounding
    remainder goes to the largest piece."""
    total = sum(weights) or 1
    out = [round(duration * w / total) for w in weights]
    if out:
        # A sliver under the tracker's own minimum would never have been sent
        # on its own, so it joins the largest piece and gets no timesheet.
        out = [0 if x < tracker.MIN_ENTRY_SECONDS else x for x in out]
        out[max(range(len(out)), key=lambda i: weights[i])] += duration - sum(out)
    return out


def apply(config: dict, moves: list, snapshot_dir: str = common.BASE_DIR):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot = os.path.join(snapshot_dir, f"refile-evens-snapshot-{stamp}.json")
    with open(snapshot, "w") as f:
        json.dump(moves, f, indent=2, default=str)
    print(f"Snapshot of {len(moves)} timesheet(s) and their planned pieces: {snapshot}")
    done = load_posted(snapshot_dir)
    for m in moves:
        # Post every piece first and delete the original only once all of them
        # landed, so a rejected post never loses the time. A failure rolls back
        # the pieces already posted for that timesheet and stops the run.
        posted = []
        tags = ",".join(t if isinstance(t, str) else t.get("name", "") for t in m["sheet"].get("tags") or [])
        for p in m["pieces"]:
            if p["seconds"] <= 0:
                continue
            start = common.parse_z(p["start"])
            body = {
                "begin": common.kimai_local_time(start, config),
                "end": common.kimai_local_time(start + datetime.timedelta(seconds=p["seconds"]), config),
                "project": p["project"],
                "activity": p["activity"],
                "description": p["description"],
                "billable": False,
                "exported": False,
            }
            if tags:
                body["tags"] = tags
            try:
                posted.append(common.kimai_request(config, "POST", "/timesheets", body))
            except Exception:
                # A post that timed out may still have saved; find it so it goes too.
                posted += saved_anyway(config, body, posted, m["sheet"]["id"])
                take_back(config, posted, snapshot_dir)
                print(f"  #{m['sheet']['id']} {m['day']}: a piece was rejected, left as it was; stopping")
                raise
        try:
            common.kimai_request(config, "DELETE", f"/timesheets/{m['sheet']['id']}")
        except Exception:
            gone = is_gone(config, m["sheet"]["id"])
            if gone is None:
                # Taking the pieces back could lose the time if the original is
                # gone, and keeping them could count it twice. Keep them, record
                # them, and stop for a person to look.
                done |= {new["id"] for new in posted if new and new.get("id")}
                save_posted(done, snapshot_dir)
                print(f"  #{m['sheet']['id']} {m['day']}: Kimai did not answer; check whether #{m['sheet']['id']} "
                      f"still exists next to its new pieces {sorted(new['id'] for new in posted if new)}; stopping")
                raise
            if gone:
                # The delete went through and only the answer was lost.
                done |= {new["id"] for new in posted if new and new.get("id")}
                save_posted(done, snapshot_dir)
                print(f"  #{m['sheet']['id']} {m['day']} re-filed into {len(posted)} timesheet(s)")
                continue
            # The original is still there, so its pieces would count the time twice.
            take_back(config, posted, snapshot_dir)
            print(f"  #{m['sheet']['id']} {m['day']}: could not delete the original, its pieces taken back; stopping")
            raise
        done |= {new["id"] for new in posted if new and new.get("id")}
        save_posted(done, snapshot_dir)
        print(f"  #{m['sheet']['id']} {m['day']} re-filed into {len(posted)} timesheet(s)")


def self_test() -> int:
    """Fake Kimai, no network."""
    import io, urllib.error
    calls, fails = [], []

    def check(name, got, want):
        ok = got == want
        print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
        if not ok:
            fails.append(name)

    def fake(pages=None, error=None, post_fails=False):
        def request(config, method, path, body=None):
            calls.append((method, path))
            if method == "GET":
                page = int(path.rsplit("page=", 1)[1].split("&")[0])
                if error and page == 1:
                    raise error
                if page > len(pages):
                    raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO())
                return pages[page - 1]
            if method == "POST" and post_fails:
                raise urllib.error.HTTPError(path, 400, "Bad Request", {}, io.BytesIO())
        return request

    real = common.kimai_request
    try:
        common.kimai_request = fake(pages=[[{}] * 500, [{}] * 3])
        check("pages are read until a short page", len(timesheets({}, "2026-01-01", "2026-01-02")), 503)
        common.kimai_request = fake(pages=[[{}] * 500])
        check("a 404 past the last page ends paging", len(timesheets({}, "2026-01-01", "2026-01-02")), 500)
        common.kimai_request = fake(error=urllib.error.HTTPError("x", 401, "Unauthorized", {}, io.BytesIO()))
        try:
            timesheets({}, "2026-01-01", "2026-01-02")
            check("an auth error is raised, not read as no timesheets", "returned", "raised")
        except urllib.error.HTTPError:
            check("an auth error is raised, not read as no timesheets", "raised", "raised")

        move = {"day": "2026-01-01", "sheet": {"id": 7, "tags": []}, "pieces": [
            {"project": 63, "activity": 76, "seconds": 600, "start": "2026-01-01T10:00:00Z", "description": "a"}]}
        calls.clear()
        common.kimai_request = fake(post_fails=True)
        try:
            apply({"timezone": "UTC"}, [move], snapshot_dir=tempfile.mkdtemp())
        except Exception:
            pass
        check("a rejected post leaves the original timesheet in place",
              [c for c in calls if c[0] == "DELETE"], [])
        calls.clear()
        common.kimai_request = fake()
        apply({"timezone": "UTC"}, [move], snapshot_dir=tempfile.mkdtemp())
        check("pieces are posted before the original is deleted",
              [c[0] for c in calls], ["POST", "DELETE"])
        d = tempfile.mkdtemp()
        common.kimai_request = lambda config, method, path, body=None: {"id": 501} if method == "POST" else None
        apply({"timezone": "UTC"}, [move], snapshot_dir=d)
        check("posted timesheets are remembered so a later run leaves them alone",
              load_posted(d), {501})
        calls.clear()
        d = tempfile.mkdtemp()
        def delete_fails(config, method, path, body=None):
            calls.append((method, path))
            if method == "POST":
                return {"id": 601}
            if method == "DELETE" and path == "/timesheets/7":
                raise urllib.error.HTTPError(path, 500, "Server Error", {}, io.BytesIO())
        common.kimai_request = delete_fails
        try:
            apply({"timezone": "UTC"}, [move], snapshot_dir=d)
        except Exception:
            pass
        check("a failed delete of the original takes its new pieces back out",
              ([c for c in calls if c[0] == "DELETE"], load_posted(d)),
              ([("DELETE", "/timesheets/7"), ("DELETE", "/timesheets/601")], set()))
    finally:
        common.kimai_request = real
    check("pieces add up to the original duration to the second",
          sum(split_seconds(1802, [1, 1, 1, 1])), 1802)
    check("a piece under the tracker's minimum folds into the largest one",
          split_seconds(1800, [1799, 1]), [1800, 0])
    calls.clear()
    d = tempfile.mkdtemp()
    def delete_times_out(config, method, path, body=None):
        calls.append((method, path))
        if method == "POST":
            return {"id": 701}
        if method == "DELETE" and path == "/timesheets/7":
            raise TimeoutError("timed out")
        if method == "GET" and path == "/timesheets/7":
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO())
    common.kimai_request = delete_times_out
    move0 = {"day": "2026-01-01", "sheet": {"id": 7, "tags": []}, "pieces": [
        {"project": 63, "activity": 76, "seconds": 600, "start": "2026-01-01T10:00:00Z", "description": "a"},
        {"project": 23, "activity": 22, "seconds": 0, "start": "2026-01-01T10:00:00Z", "description": "b"}]}
    try:
        apply({"timezone": "UTC"}, [move0], snapshot_dir=d)
    finally:
        common.kimai_request = real
    check("a delete that timed out but went through keeps the new pieces",
          load_posted(d), {701})
    check("a zero-second piece is not posted",
          len([c for c in calls if c[0] == "POST"]), 1)
    a12 = {"id": 1, "duration": 720, "project": {"id": 23}}
    c13 = {"id": 2, "duration": 780, "project": {"id": 63}}
    check("two timesheets on different projects that both fit are skipped", pick_sheet([a12, c13], 12), None)
    check("  one that fits alone is picked", pick_sheet([a12], 12), a12)
    check("an exported timesheet is not moved", movable({"exported": True}), False)
    calls.clear()
    d = tempfile.mkdtemp()
    def post_times_out(config, method, path, body=None):
        calls.append((method, path))
        if method == "POST":
            raise TimeoutError("timed out")
        if method == "GET" and path.startswith("/timesheets?"):
            return [{"id": 801, "project": {"id": 63}, "description": "a", "begin": "2026-01-01T10:00:00+0000"},
                    {"id": 7, "project": {"id": 63}, "description": "a", "begin": "2026-01-01T10:00:00+0000"},
                    {"id": 803, "project": {"id": 63}, "description": "a", "begin": "2026-01-01T10:05:00+0000"},
                    {"id": 802, "project": {"id": 23}, "description": "other", "begin": "2026-01-01T10:00:00+0000"}]
    common.kimai_request = post_times_out
    try:
        apply({"timezone": "UTC"}, [move], snapshot_dir=d)
    except Exception:
        pass
    finally:
        common.kimai_request = real
    check("a post that timed out but saved is found and taken back out, never the original",
          [c for c in calls if c[0] == "DELETE"], [("DELETE", "/timesheets/801")])
    t = lambda w: {"source": "terminal", "key": "~/projects/daily-briefing", "window": w}
    sent_keys = {"~/projects/daily-briefing", "~/projects/daily-briefing | ctfg weekly summary doc"}
    check("a folder with one CTFG title split out keeps its other titled entries",
          [e["window"] for e in group_entries([t("✳ CTFG weekly summary doc"), t("✳ Evening brief")],
                                              "~/projects/daily-briefing", sent_keys)], ["✳ Evening brief"])
    calls.clear()
    d = tempfile.mkdtemp()
    def all_lost(config, method, path, body=None):
        calls.append((method, path))
        if method == "POST":
            return {"id": 901}
        raise TimeoutError("timed out")
    common.kimai_request = all_lost
    try:
        apply({"timezone": "UTC"}, [move], snapshot_dir=d)
    except Exception:
        pass
    finally:
        common.kimai_request = real
    check("when Kimai cannot say whether the original is gone, the new pieces stay",
          [c for c in calls if c == ("DELETE", "/timesheets/901")], [])
    d = tempfile.mkdtemp()
    def rollback_fails(config, method, path, body=None):
        if method == "DELETE":
            raise TimeoutError("timed out")
    common.kimai_request = rollback_fails
    try:
        take_back({}, [{"id": 951}], d)
    finally:
        common.kimai_request = real
    check("a piece that cannot be taken back is recorded so later runs leave it alone",
          load_posted(d), {951})
    log = os.path.join(tempfile.mkdtemp(), "tracker.log")
    with open(log, "w") as f:
        f.write("2026-11-07 00:00:16,083 INFO Sent [2026-11-06] 'Find in page\n"
                "    EVENS' (chrome:us19.admin.mailchimp.com) \u2192 15m\n"
                "2026-11-07 00:00:17,000 INFO Sent [2026-11-06] 'Inbox' (chrome:mail.google.com) \u2192 3m\n")
    with open(log, "a") as f:
        f.write("2026-11-07 00:00:18,000 INFO Sent [2026-11-07] 'Cut off\n"
                "2026-11-07 00:00:19,000 INFO Sent [2026-11-07] 'Gmail' (chrome:mail.google.com) \u2192 2m\n")
    check("a cut-off Sent line never swallows the next one",
          sends("2026-11-07", "2026-11-07", log), [("2026-11-07", "Gmail", "chrome:mail.google.com", 2)])
    check("a Sent line whose description spans two lines is read whole",
          sends("2026-11-06", "2026-11-06", log),
          [("2026-11-06", "Find in page\n    EVENS", "chrome:us19.admin.mailchimp.com", 15),
           ("2026-11-06", "Inbox", "chrome:mail.google.com", 3)])
    check("a day with no log file gives no entries", day_log("1999-01-01"), None)
    print(f"{len(fails) == 0 and 'all' or len(fails)} {'passed' if not fails else 'failed'}")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    if "--self-test" in sys.argv:
        return self_test()
    ap.add_argument("first")
    ap.add_argument("last")
    ap.add_argument("--keys", nargs="*", default=DEFAULT_KEYS)
    ap.add_argument("--evens-project", type=int, default=23,
                    help="any Evens project id; its customer is the one being cleaned")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    config = common.load_config()
    if not config.get("ctfg_title_rule"):
        sys.exit("ctfg_title_rule is not set in config.json")
    moves = plan(config, args.first, args.last, args.keys, args.evens_project)
    show(moves)
    if args.apply:
        apply(config, moves)
    else:
        print("\nPlan only. Nothing in Kimai changed.")


if __name__ == "__main__":
    sys.exit(main())
