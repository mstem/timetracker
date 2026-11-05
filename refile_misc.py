#!/usr/bin/env python3
"""
Re-file days whose time went to the default project (Misc) under the current
rules: title splits, Chrome profiles, Granola context, phone app mappings.

For each day: snapshot every default-project timesheet to
refile-misc-snapshot-<date>.json, delete them, then post again only the
entries the old mappings had sent to the default project. Nothing filed
anywhere else is touched.

  python3 refile_misc.py --old project_mappings.json.bak-20261105-misc 2026-11-02 2026-11-03
  python3 refile_misc.py --old ... --dry-run 2026-11-02     # shows what it would post, changes nothing
"""

import argparse
import datetime
import json
import os
import sys

import ai_matcher
import common
import tracker


def old_project(e: dict, old: dict, categories: dict, default):
    key = common.entry_key(e)
    mapping = old.get(key) or {}
    if e.get("source") == "android":
        return (mapping.get("project")
                or (categories.get(e.get("category") or "") or {}).get("project")
                or default)
    return mapping.get("project") or default


def misc_timesheets(config: dict, day: str, default) -> list:
    begin = f"{day}T00:00:00"
    end = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat() + "T00:00:00"
    return common.kimai_request(
        config, "GET",
        f"/timesheets?project={default}&begin={begin}&end={end}&size=500&full=true") or []


def refile(config: dict, day: str, old: dict, dry_run: bool):
    default_project = config["default_project_id"]
    default_activity = config.get("default_activity_id")
    categories = json.load(open(os.path.join(common.BASE_DIR, "android_categories.json")))

    existing = misc_timesheets(config, day, default_project)
    print(f"{day}: {len(existing)} Misc timesheet(s), "
          f"{sum(t['duration'] for t in existing) / 3600:.2f}h in Kimai now")

    mac = json.load(open(os.path.join(common.LOGS_DIR, f"{day}.json")))
    phone_path = os.path.join(common.ANDROID_LOGS_DIR, f"{day}.json")
    phone = json.load(open(phone_path)) if os.path.exists(phone_path) else []

    meetings = [] if dry_run else tracker._sync_meetings(config, day)
    mac = tracker.subtract_meetings(mac, meetings)
    for e in mac:
        e["_old_misc"] = old_project(e, old, categories, default_project) == default_project

    mappings = common.load_project_mappings() if dry_run else ai_matcher.classify_and_save(config, mac)
    mac = common.split_by_title(mac, config, mappings)
    project_customer = {p["id"]: p.get("customer") for p in common.list_kimai_projects(config)}

    def resolve_mac(key):
        return common.resolve_mapping(key, mappings, default_project, default_activity,
                                      config, project_customer)

    mac, extra = common.assign_context(mac, config, resolve_mac, project_customer)
    mappings = {**mappings, **extra}
    mac_misc = [e for e in mac if e["_old_misc"]]

    phone = tracker.subtract_intervals(phone, tracker.entry_intervals(mac) + list(meetings))
    phone_misc = [e for e in phone
                  if old_project(e, old, categories, default_project) == default_project]

    def resolve_android(key):
        mapping = mappings.get(key) or {}
        if mapping.get("project"):
            return mapping["project"], mapping.get("activity") or default_activity
        cats = {common.entry_key(e): e.get("category") or "" for e in phone_misc}
        target = categories.get(cats.get(key, "")) or {}
        if target.get("project"):
            return target["project"], target.get("activity") or default_activity
        return default_project, default_activity

    mac_groups = tracker.aggregate_entries(mac_misc)
    phone_groups = tracker.aggregate_entries(phone_misc)
    after_misc = (sum(g["seconds"] for k, g in mac_groups.items() if resolve_mac(k)[0] == default_project)
                  + sum(g["seconds"] for k, g in phone_groups.items() if resolve_android(k)[0] == default_project))
    print(f"  re-filed, Misc would hold {after_misc / 3600:.2f}h "
          f"of {(sum(g['seconds'] for g in mac_groups.values()) + sum(g['seconds'] for g in phone_groups.values())) / 3600:.2f}h")

    if dry_run:
        return

    snapshot = os.path.join(common.BASE_DIR, f"refile-misc-snapshot-{day}.json")
    with open(snapshot, "w") as f:
        json.dump(existing, f, indent=2)
    for t in existing:
        common.kimai_request(config, "DELETE", f"/timesheets/{t['id']}")
    print(f"  deleted {len(existing)}; snapshot in {snapshot}")

    sent, failed = tracker._post_groups(config, day, mac_groups, resolve_mac)
    common.ensure_kimai_tag(config, "android")
    psent, pfailed = tracker._post_groups(config, day, phone_groups, resolve_android, tags="android")
    print(f"  posted {sent + psent}, failed {failed + pfailed}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="mappings file as it was when these days were sent")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("days", nargs="+")
    args = ap.parse_args()
    config = common.load_config()
    old = {common.normalize_key(k): v for k, v in json.load(open(args.old)).items()}
    for day in args.days:
        refile(config, day, old, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
