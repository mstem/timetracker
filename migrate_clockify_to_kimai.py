#!/usr/bin/env python3
"""
One-off migration: mirror your Clockify workspace into Kimai.

Reads Clockify clients and projects, then in Kimai creates:
  - a Customer for each Clockify client (Clockify projects with no client go
    under a single "No client" customer),
  - a Project for each Clockify project (under the mapped customer),
  - a "General" activity per project (Kimai timesheets need an activity), and
  - a "Meetings" customer/project/activity for calendar-synced meetings.

It's idempotent — existing Kimai customers/projects/activities are reused by
name, so re-running won't create duplicates. Finally it rewrites
project_mappings.json from Clockify project IDs to Kimai {project, activity}
pairs, and prints the ids to drop into config.json (default_* and meetings_*).

Needs BOTH backends in config.json:
    kimai_url / kimai_token
    clockify_api_key / clockify_workspace_id   (only used here, for the read)

Usage:
    python3 migrate_clockify_to_kimai.py            # do it
    python3 migrate_clockify_to_kimai.py --dry-run  # show what would be created
"""

import json
import sys
import urllib.request

import common

CLOCKIFY_API_BASE = "https://api.clockify.me/api/v1"
NO_CLIENT = "No client"


def _clockify_get(config: dict, path: str):
    req = urllib.request.Request(
        f"{CLOCKIFY_API_BASE}{path}",
        headers={"X-Api-Key": config["clockify_api_key"], "Content-Type": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _require(config: dict, *keys):
    missing = [k for k in keys if common._is_unset(config.get(k))]
    if missing:
        print(f"ERROR: missing config.json keys for migration: {', '.join(missing)}")
        sys.exit(1)


def main(dry_run: bool):
    config = common.load_config()   # validates kimai_url/kimai_token
    _require(config, "clockify_api_key", "clockify_workspace_id")
    wid = config["clockify_workspace_id"]

    print("Reading Clockify clients and projects...")
    clients = _clockify_get(config, f"/workspaces/{wid}/clients?page-size=500")
    projects = _clockify_get(config, f"/workspaces/{wid}/projects?archived=false&page-size=500")
    client_name_by_id = {c["id"]: c["name"] for c in clients}
    print(f"  {len(clients)} client(s), {len(projects)} project(s)")

    # Existing Kimai state, indexed by name for idempotency.
    kimai_customers = {c["name"]: c for c in common.list_kimai_customers(config)}
    kimai_projects = {p["name"]: p for p in common.list_kimai_projects(config)}
    kimai_activities = common.list_kimai_activities(config)
    # activity lookup keyed by (project_id, name); project None => global
    act_index = {(a.get("project"), a["name"]): a for a in kimai_activities}

    def ensure_customer(name):
        if name in kimai_customers:
            return kimai_customers[name]["id"]
        if dry_run:
            print(f"  [dry-run] would create customer: {name}")
            return f"<customer:{name}>"
        cust = common.create_kimai_customer(config, name)
        kimai_customers[name] = cust
        print(f"  + customer: {name} ({cust['id']})")
        return cust["id"]

    def ensure_project(name, customer_id):
        if name in kimai_projects:
            return kimai_projects[name]["id"]
        if dry_run:
            print(f"  [dry-run] would create project: {name} (customer {customer_id})")
            return f"<project:{name}>"
        proj = common.create_kimai_project(config, name, customer_id)
        kimai_projects[name] = proj
        print(f"  + project: {name} ({proj['id']})")
        return proj["id"]

    def ensure_activity(name, project_id):
        key = (project_id, name)
        if key in act_index:
            return act_index[key]["id"]
        if dry_run:
            print(f"  [dry-run] would create activity: {name} (project {project_id})")
            return f"<activity:{name}>"
        act = common.create_kimai_activity(config, name, project_id)
        act_index[key] = act
        print(f"  + activity: {name} ({act['id']}) for project {project_id}")
        return act["id"]

    # 1. Mirror clients -> customers, projects -> projects, + a General activity each.
    clockify_to_kimai = {}   # clockify project id -> {"project": id, "activity": id}
    for p in projects:
        client_name = client_name_by_id.get(p.get("clientId")) or NO_CLIENT
        customer_id = ensure_customer(client_name)
        project_id = ensure_project(p["name"], customer_id)
        activity_id = ensure_activity("General", project_id)
        clockify_to_kimai[p["id"]] = {"project": project_id, "activity": activity_id}

    # 2. Dedicated Meetings + a default catch-all, for config fallbacks.
    meetings_cust = ensure_customer("Meetings")
    meetings_proj = ensure_project("Meetings", meetings_cust)
    meetings_act = ensure_activity("Meeting", meetings_proj)

    default_cust = ensure_customer("Internal")
    default_proj = ensure_project("Misc", default_cust)
    default_act = ensure_activity("General", default_proj)

    # 3. Rewrite existing mappings from Clockify project ids to Kimai pairs.
    mappings = common.load_project_mappings()   # values already normalized to {project, activity}
    rewritten, unmatched = {}, []
    for key, val in mappings.items():
        old_pid = val.get("project")   # legacy value: this is a Clockify project id
        if old_pid in clockify_to_kimai:
            rewritten[key] = clockify_to_kimai[old_pid]
        else:
            unmatched.append(key)

    print(f"\nMapping rewrite: {len(rewritten)} key(s) remapped, {len(unmatched)} unmatched.")
    if unmatched:
        print(f"  Unmatched (left as-is, re-classify later): {', '.join(unmatched)}")

    if dry_run:
        print("\n[dry-run] No changes written. Re-run without --dry-run to apply.")
        return

    if rewritten:
        # keep unmatched keys untouched so nothing silently breaks
        merged = dict(mappings)
        merged.update(rewritten)
        common.save_project_mappings(merged)
        print("  project_mappings.json updated.")

    print("\nDone. Add these to config.json:")
    print(json.dumps({
        "default_project_id": default_proj,
        "default_activity_id": default_act,
        "default_customer_id": default_cust,
        "meetings_project_id": meetings_proj,
        "meetings_activity_id": meetings_act,
    }, indent=2))


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv[1:])
