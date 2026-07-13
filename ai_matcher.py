#!/usr/bin/env python3
"""
AI-powered project classifier for the activity tracker.
Scans recent log files, finds activity keys (Terminal directory names,
Chrome tab domains, or other app names) with no Kimai mapping, and uses
Claude to match them against your Kimai projects in one batch call. Claude
picks a project; the project's activity is resolved locally (Kimai timesheets
need both), so a saved mapping is {"project": id, "activity": id}.
Matched results are saved to project_mappings.json and never re-queried.

`classify_and_save()` is also called automatically by tracker.py right
before every send, so new activity gets classified without any manual step.
This CLI remains for manual review, dry runs, and corrections.

Usage:
  python3 ai_matcher.py                        # match unmapped keys (last 7 days)
  python3 ai_matcher.py --dry-run              # preview matches without saving
  python3 ai_matcher.py --days N                # look back N days (default 7)
  python3 ai_matcher.py --interactive          # propose each match, you confirm
  python3 ai_matcher.py --link KEY "Name"      # fuzzy-link an activity key to a project by name
"""

import json
import os
import sys
import datetime
import logging

import common

LOG_FILE = os.path.join(common.BASE_DIR, "ai_matcher.log")

DEFAULT_DAYS = 7
SAMPLE_TITLES_PER_KEY = 5

# Sentinel returned from interactive prompts to signal "go back to previous item"
GO_BACK = object()

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Log scanning
# ---------------------------------------------------------------------------

def collect_unmapped_keys_from_entries(entries: list, mappings: dict, config: dict) -> dict:
    """
    Scan a list of log entries, return {key: [sample raw titles]} for activity
    keys with no existing mapping. Skips Terminal noise dirnames (shell
    artifacts like "-zsh" or bare "~") and generic Chrome domains (sign-in
    flows, bare search homepages — see common.is_generic_domain) — neither
    carries real project signal, so neither is worth asking Claude about.
    """
    unmapped = {}
    for e in entries:
        key = common.entry_key(e)
        if key in mappings:
            continue
        if key.startswith("chrome:"):
            if common.is_generic_domain(key[len("chrome:"):], config):
                continue
        elif not key.startswith("app:") and common.is_noise_dirname(key):
            continue
        titles = unmapped.setdefault(key, [])
        title = e.get("window", key)
        if title not in titles and len(titles) < SAMPLE_TITLES_PER_KEY:
            titles.append(title)
    return unmapped


def collect_unmapped_keys(days: int, mappings: dict, config: dict) -> dict:
    """
    Scan the last N days of log files, merge into one {key: [sample titles]}
    dict of activity keys with no mapping yet.
    """
    today = datetime.date.today()
    merged = {}

    for i in range(1, days + 1):          # never include today's partial log
        date = today - datetime.timedelta(days=i)
        path = os.path.join(common.LOGS_DIR, f"{date.isoformat()}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                entries = json.load(f)
        except Exception as exc:
            log.warning(f"Could not read {path}: {exc}")
            continue
        for key, titles in collect_unmapped_keys_from_entries(entries, mappings, config).items():
            existing = merged.setdefault(key, [])
            for t in titles:
                if t not in existing and len(existing) < SAMPLE_TITLES_PER_KEY:
                    existing.append(t)

    return merged


# ---------------------------------------------------------------------------
# Anthropic / Claude
# ---------------------------------------------------------------------------

def classify_keys_with_claude(api_key: str, unmapped: dict, projects: list, mappings: dict = None) -> dict:
    """
    Send all unmapped activity keys (with sample titles for context) + all
    project names to Claude in one call. Returns {key: project_id_or_None}.

    `mappings` (existing confirmed key → project_id mappings, if any) are
    included as calibration examples so Claude has a concrete sense of what
    "specific enough to map" looks like in this user's own data, rather than
    guessing from the project list alone.
    """
    project_map = {p["name"]: p["id"] for p in projects}
    project_by_id = {p["id"]: p["name"] for p in projects}

    examples_block = ""
    if mappings:
        sample = {}
        for k, v in list(mappings.items())[:15]:
            pid = v.get("project") if isinstance(v, dict) else v
            if pid in project_by_id:
                sample[k] = project_by_id[pid]
        if sample:
            examples_block = (
                "\nExamples of activity keys this user has already confirmed map to a "
                f"specific project (for calibration only — do not reuse these keys):\n"
                f"{json.dumps(sample, indent=2)}\n"
            )

    system = (
        "You are a project-matching assistant for a personal time tracker. "
        "Map activity keys to Kimai project IDs. Activity keys come in three "
        "forms: a bare directory name (from a macOS Terminal window title), "
        "'chrome:<domain>' (a Chrome tab's domain), or 'app:<AppName>' (any other "
        "application's name). Use the sample window/tab titles given for each key "
        "as context for what the user was actually doing. "
        "Reply ONLY with valid JSON. No markdown, no explanation, no code fences."
    )

    user = (
        "Match each activity key to the most appropriate Kimai project.\n"
        f"{examples_block}\n"
        f"Activity keys to match, with sample titles seen for each:\n{json.dumps(unmapped, indent=2)}\n\n"
        f"Available Kimai projects (name → id):\n{json.dumps(project_map, indent=2)}\n\n"
        "Rules:\n"
        "- Only return a project ID when the sample titles reference something "
        "specific — a named project, client, document, or task — that clearly "
        "belongs to one project.\n"
        "- Return null for anything that is just a generic product surface with no "
        "project-specific content: sign-in/login/auth screens, a bare search engine "
        "homepage, an empty inbox or calendar view, a generic app launch screen, or "
        "any title too vague to distinguish between projects. Being on a domain "
        "like docs.google.com or calendar.google.com is NOT itself a signal — the "
        "title still has to name something specific (e.g. \"Q3 Marketing Plan - "
        "Google Docs\" is specific; \"Google Calendar\" or \"Sign in - Google "
        "accounts\" is not).\n"
        "- Prefer null over a weak guess, even if some project seems plausible.\n"
        "- Every activity key in the input must appear as a key in your response.\n\n"
        "Respond with exactly this JSON structure:\n"
        '{"key1": "id_or_null", "key2": "id_or_null", ...}'
    )

    response = common.anthropic_request(api_key, [{"role": "user", "content": user}], system)

    raw = response["content"][0]["text"].strip()
    log.info(f"Claude raw response: {raw}")
    raw = common.strip_markdown_fences(raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude returned non-JSON:\n{raw}")

    if not isinstance(result, dict):
        raise RuntimeError(f"Claude returned unexpected type: {type(result)}")

    # Validate: returned IDs must exist in the project list. Kimai IDs are
    # integers; tolerate Claude returning them as numeric strings.
    valid_ids = {p["id"] for p in projects}
    validated = {}
    for key, project_id in result.items():
        if isinstance(project_id, str) and project_id.isdigit():
            project_id = int(project_id)
        if project_id is None:
            validated[key] = None
        elif project_id in valid_ids:
            validated[key] = project_id
        else:
            log.warning(f"Claude returned unknown project ID '{project_id}' for '{key}' — skipping")
            validated[key] = None

    return validated


# ---------------------------------------------------------------------------
# Activity resolution  (Kimai timesheets need a project AND an activity)
# ---------------------------------------------------------------------------

def build_activity_resolver(config: dict, activities: list):
    """Return resolve(project_id) -> activity_id. Prefers an activity scoped to
    that project (one named 'General' if present), then the configured
    default_activity_id, then a global 'General'/first global activity."""
    by_project, globals_ = {}, []
    for a in activities:
        proj = a.get("project")
        if proj:
            by_project.setdefault(proj, []).append(a)
        else:
            globals_.append(a)

    def _prefer_general(items):
        for a in items:
            if a.get("name", "").strip().lower() == "general":
                return a["id"]
        return items[0]["id"] if items else None

    default_activity = config.get("default_activity_id")

    def resolve(project_id):
        scoped = by_project.get(project_id)
        if scoped:
            return _prefer_general(scoped)
        if default_activity:
            return default_activity
        return _prefer_general(globals_)

    return resolve


def classify_and_save(config: dict, entries: list) -> dict:
    """
    Importable entrypoint used by tracker.py right before sending: find
    activity keys in `entries` with no existing mapping, batch-classify them,
    save confident matches, and return the (possibly updated) mappings dict.
    Never raises — a classification failure should never block a send whose
    entries already have mappings.
    """
    mappings = common.load_project_mappings()

    anthropic_key = common.require_anthropic_key(config)
    if not anthropic_key:
        return mappings

    unmapped = collect_unmapped_keys_from_entries(entries, mappings, config)
    if not unmapped:
        return mappings

    try:
        projects = common.list_kimai_projects(config)
        resolve_activity = build_activity_resolver(config, common.list_kimai_activities(config))
        matches = classify_keys_with_claude(anthropic_key, unmapped, projects, mappings)
        to_save = {}
        for key, pid in matches.items():
            if not pid:
                continue
            activity_id = resolve_activity(pid)
            if not activity_id:
                log.warning(f"'{key}' → project {pid} but no activity resolvable — leaving unmapped")
                continue
            to_save[key] = {"project": pid, "activity": activity_id}
        if to_save:
            mappings.update(to_save)
            common.save_project_mappings(mappings)
            log.info(f"Auto-classified {len(to_save)} activity key(s) before send: {to_save}")
    except Exception as e:
        log.error(f"Classification before send failed (continuing without it): {e}")

    return mappings


# ---------------------------------------------------------------------------
# Fuzzy project finder  (for --link, no AI needed)
# ---------------------------------------------------------------------------

def fuzzy_find_project(name_query: str, projects: list):
    query_lower = name_query.lower()
    query_words = set(query_lower.split())
    best = None
    best_score = 0

    for p in projects:
        name_lower = p["name"].lower()
        if name_lower == query_lower:
            return p   # exact match, done
        score = 0
        if query_lower in name_lower:
            score = 50 + int(len(query_lower) / max(len(name_lower), 1) * 40)
        else:
            for word in query_words:
                if word in name_lower:
                    score += 10
        if score > best_score:
            best_score = score
            best = p

    return best if best_score > 0 else None


# ---------------------------------------------------------------------------
# Interactive confirmation
# ---------------------------------------------------------------------------

def confirm_match(key: str, project: dict, projects: list, config: dict):
    """
    Ask the user to confirm, reject, or override a proposed match.
    Returns: confirmed project dict, None to skip, or GO_BACK sentinel.
    """
    print(f"  {key}  →  {project['name']}")
    answer = input("    [y/n/back/project name]: ").strip()
    if answer.lower() == "y" or answer == "":
        return project
    if answer.lower() == "n":
        return None
    if answer.lower() in ("b", "back"):
        return GO_BACK
    return _resolve_or_create(answer, projects, config)


def _customer_name(p: dict) -> str:
    """Customer label for a Kimai project. The /projects collection exposes the
    customer name as parentTitle; fall back gracefully."""
    return p.get("parentTitle") or "No customer"


def print_projects_by_client(projects: list):
    """Print all Kimai projects grouped by customer, one line per customer."""
    by_client = {}
    for p in projects:
        by_client.setdefault(_customer_name(p), []).append(p["name"])
    print("── Kimai projects ─────────────────────────────")
    for client in sorted(by_client):
        names = ",  ".join(sorted(by_client[client]))
        print(f"  {client}:  {names}")
    print("───────────────────────────────────────────────\n")


def _resolve_or_create(answer: str, projects: list, config: dict):
    """
    Try to fuzzy-match answer against existing projects.
    If no match found, offer to create a new Kimai project with that name under
    the configured default_customer_id (and a 'General' activity). Returns the
    project dict or None.
    """
    found = fuzzy_find_project(answer, projects)
    if found:
        print(f"    Using: {found['name']}")
        return found
    customer_id = config.get("default_customer_id")
    if not customer_id:
        print(f"    No match for '{answer}', and no default_customer_id set in config.json — "
              "create the project in Kimai first, then re-run.")
        return None
    create = input(f"    No match for '{answer}'. Create new Kimai project under customer {customer_id}? [y/n]: ").strip().lower()
    if create == "y":
        try:
            new_project = common.create_kimai_project(config, answer, customer_id)
            common.create_kimai_activity(config, "General", new_project["id"])
            projects.append(new_project)   # make it available for subsequent matches
            print(f"    Created: '{new_project['name']}'  ({new_project['id']})")
            log.info(f"Created new Kimai project '{new_project['name']}' ({new_project['id']})")
            return new_project
        except Exception as e:
            print(f"    ERROR creating project: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_match(days: int, dry_run: bool, interactive: bool):
    config = common.load_config()
    anthropic_key = common.require_anthropic_key(config)
    if not anthropic_key:
        print("anthropic_api_key not set in config.json — AI matching is opt-in.")
        print("Add your Anthropic API key to config.json to enable this feature.")
        return

    mappings = common.load_project_mappings()

    print(f"Scanning logs from the last {days} days...")
    unmapped = collect_unmapped_keys(days, mappings, config)

    if not unmapped:
        print("All activity keys already mapped — nothing to do.")
        return

    print(f"Found {len(unmapped)} unmapped activity key(s): {', '.join(unmapped)}\n")

    print("Fetching Kimai projects and activities...")
    try:
        projects = common.list_kimai_projects(config)
        resolve_activity = build_activity_resolver(config, common.list_kimai_activities(config))
    except Exception as e:
        print(f"ERROR: Could not fetch Kimai projects: {e}")
        sys.exit(1)

    print_projects_by_client(projects)
    print(f"Asking Claude to match against {len(projects)} project(s)...\n")
    try:
        matches = classify_keys_with_claude(anthropic_key, unmapped, projects, mappings)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    project_by_id = {p["id"]: p for p in projects}
    keys = list(unmapped.keys())
    to_save = {}
    null_count = 0

    if interactive:
        print("Review each proposed match  (y = accept, n = skip, back = redo previous, or type a project name):\n")
        i = 0
        while i < len(keys):
            key = keys[i]
            project_id = matches.get(key)

            if project_id and project_id in project_by_id:
                result = confirm_match(key, project_by_id[project_id], projects, config)
            else:
                print(f"  {key}  →  (no confident match)")
                answer = input("    [back/project name/Enter to skip]: ").strip()
                if answer.lower() in ("b", "back"):
                    result = GO_BACK
                elif answer:
                    result = _resolve_or_create(answer, projects, config)
                else:
                    result = None

            if result is GO_BACK:
                if i > 0:
                    prev = keys[i - 1]
                    to_save.pop(prev, None)   # undo previous save decision
                    print(f"  (going back to '{prev}')\n")
                    i -= 1
                else:
                    print("  (already at the first item)")
                continue

            if result:
                activity_id = resolve_activity(result["id"])
                if activity_id:
                    to_save[key] = {"project": result["id"], "activity": activity_id}
                    log.info(f"Matched '{key}' → {result['name']} (interactive)")
                else:
                    to_save.pop(key, None)
                    print(f"    (no activity available for '{result['name']}' — skipped)")
            else:
                to_save.pop(key, None)   # clear if re-answering

            i += 1

        null_count = sum(1 for k in keys if k not in to_save)
    else:
        for key in keys:
            project_id = matches.get(key)
            if project_id and project_id in project_by_id:
                activity_id = resolve_activity(project_id)
                if activity_id:
                    to_save[key] = {"project": project_id, "activity": activity_id}
                    print(f"  {key}  →  {project_by_id[project_id]['name']}")
                    log.info(f"Matched '{key}' → {project_by_id[project_id]['name']}")
                else:
                    print(f"  {key}  →  {project_by_id[project_id]['name']} (no activity available — skipped)")
                    null_count += 1
            else:
                print(f"  {key}  →  (no confident match — will retry next run)")
                null_count += 1

    print()
    if dry_run:
        print(f"Dry run — {len(to_save)} match(es) proposed, {null_count} skipped. Nothing saved.")
    else:
        if to_save:
            mappings.update(to_save)
            common.save_project_mappings(mappings)
            print(f"Saved {len(to_save)} match(es) to project_mappings.json.")
        if null_count:
            print(f"{null_count} activity key(s) left unmapped — will retry on next run.")
        if not to_save and not null_count:
            print("Nothing to save.")


def cmd_link(key: str, project_name_query: str):
    """Fuzzy-link an activity key to a project by name. No Anthropic key needed."""
    config = common.load_config()

    print("Fetching Kimai projects and activities...")
    try:
        projects = common.list_kimai_projects(config)
        resolve_activity = build_activity_resolver(config, common.list_kimai_activities(config))
    except Exception as e:
        print(f"ERROR: Could not fetch Kimai projects: {e}")
        sys.exit(1)

    found = fuzzy_find_project(project_name_query, projects)
    if not found:
        print(f"No project matched '{project_name_query}'.")
        print("\nAvailable projects:")
        for p in projects:
            client = f"  [{_customer_name(p)}]"
            print(f"  {p['name']}{client}")
        sys.exit(1)

    activity_id = resolve_activity(found["id"])
    if not activity_id:
        print(f"'{found['name']}' has no usable activity (and no default_activity_id set) — "
              "add an activity to it in Kimai first.")
        sys.exit(1)

    mappings = common.load_project_mappings()
    mappings[key] = {"project": found["id"], "activity": activity_id}
    common.save_project_mappings(mappings)
    print(f"Mapped '{key}'  →  {found['name']}  (project {found['id']}, activity {activity_id})")
    log.info(f"Linked '{key}' → {found['name']} via --link")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    args = sys.argv[1:]

    if "--link" in args:
        idx = args.index("--link")
        try:
            link_key = args[idx + 1]
            link_query = args[idx + 2]
        except IndexError:
            print('Usage: python3 ai_matcher.py --link KEY "Project Name"')
            sys.exit(1)
        cmd_link(link_key, link_query)
    else:
        dry_run = "--dry-run" in args
        interactive = "--interactive" in args
        days = DEFAULT_DAYS
        if "--days" in args:
            idx = args.index("--days")
            try:
                days = int(args[idx + 1])
            except (IndexError, ValueError):
                print("Usage: python3 ai_matcher.py --days N")
                sys.exit(1)
        cmd_match(days=days, dry_run=dry_run, interactive=interactive)
