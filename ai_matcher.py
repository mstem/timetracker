#!/usr/bin/env python3
"""
AI-powered bulk project matcher for the terminal time tracker.
Scans recent log files, finds directory names with no Clockify mapping,
and uses Claude to match them against your Clockify projects in one batch call.
Matched results are saved to project_mappings.json and never re-queried.

Usage:
  python3 ai_matcher.py                        # match unmatched dirnames (last 7 days)
  python3 ai_matcher.py --dry-run              # preview matches without saving
  python3 ai_matcher.py --days N               # look back N days (default 7)
  python3 ai_matcher.py --interactive          # propose each match, you confirm
  python3 ai_matcher.py --link DIR "Name"      # fuzzy-link a dirname to a project by name
"""

import json
import os
import sys
import datetime
import urllib.request
import urllib.error
import logging

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(BASE_DIR, "config.json")
MAPPINGS_PATH = os.path.join(BASE_DIR, "project_mappings.json")
LOGS_DIR      = os.path.join(BASE_DIR, "logs")
LOG_FILE      = os.path.join(BASE_DIR, "ai_matcher.log")

CLAUDE_API_URL    = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_DAYS      = 7

# Directory names that are shell/OS artifacts, not project directories
SKIP_DIRNAMES = {"Terminal", "home", "~", "Desktop", "Downloads", "Documents"}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: config.json not found at {CONFIG_PATH}")
        print("Run setup.sh first and fill in your API key and workspace ID.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def require_anthropic_key(config: dict) -> str:
    key = config.get("anthropic_api_key", "")
    if not key or key.startswith("<"):
        print("anthropic_api_key not set in config.json — AI matching is opt-in.")
        print("Add your Anthropic API key to config.json to enable this feature.")
        sys.exit(0)
    return key


# ---------------------------------------------------------------------------
# Project mappings  (shared format with tracker.py)
# ---------------------------------------------------------------------------

def load_project_mappings() -> dict:
    if os.path.exists(MAPPINGS_PATH):
        with open(MAPPINGS_PATH) as f:
            return json.load(f)
    return {}


def save_project_mappings(mappings: dict):
    """Write atomically to avoid corruption if tracker daemon is running."""
    tmp = MAPPINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(mappings, f, indent=2)
    os.replace(tmp, MAPPINGS_PATH)


# ---------------------------------------------------------------------------
# Window title parsing  (mirrors tracker.py)
# ---------------------------------------------------------------------------

def window_dir(window_name: str) -> str:
    """Extract the leading directory segment from a Terminal window title."""
    return window_name.split(" — ")[0].strip()


def is_noise(dirname: str) -> bool:
    if not dirname or len(dirname) <= 1:
        return True
    if dirname in SKIP_DIRNAMES:
        return True
    if dirname.startswith("-"):   # shell artifacts like "-zsh"
        return True
    if dirname.startswith(".") and len(dirname) == 1:
        return True
    return False


# ---------------------------------------------------------------------------
# Log scanning
# ---------------------------------------------------------------------------

def collect_unmatched_dirnames(days: int, mappings: dict) -> list:
    """
    Scan the last N days of log files, collect unique directory names
    that have no mapping yet.
    """
    today = datetime.date.today()
    dirnames = set()

    for i in range(1, days + 1):          # never include today's partial log
        date = today - datetime.timedelta(days=i)
        path = os.path.join(LOGS_DIR, f"{date.isoformat()}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                entries = json.load(f)
            for e in entries:
                d = window_dir(e.get("window", ""))
                if not is_noise(d):
                    dirnames.add(d)
        except Exception as exc:
            log.warning(f"Could not read {path}: {exc}")

    return sorted(d for d in dirnames if d not in mappings)


# ---------------------------------------------------------------------------
# Clockify
# ---------------------------------------------------------------------------

def clockify_request(method: str, path: str, api_key: str, body: dict = None):
    url = f"https://api.clockify.me/api/v1{path}"
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def list_clockify_projects(config: dict) -> list:
    return clockify_request(
        "GET",
        f"/workspaces/{config['workspace_id']}/projects?archived=false&page-size=500",
        config["api_key"],
    )


# ---------------------------------------------------------------------------
# Anthropic / Claude
# ---------------------------------------------------------------------------

def anthropic_request(api_key: str, messages: list, system: str) -> dict:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": system,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(CLAUDE_API_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise RuntimeError(f"Anthropic API HTTP {e.code}: {err}") from e


def match_with_claude(api_key: str, unmatched: list, projects: list) -> dict:
    """
    Send all unmatched dirnames + all project names to Claude in one call.
    Returns {dirname: project_id_or_None}.
    """
    project_map = {p["name"]: p["id"] for p in projects}

    system = (
        "You are a project-matching assistant for a terminal time tracker. "
        "Map Unix directory names from macOS Terminal window titles to Clockify project IDs. "
        "Reply ONLY with valid JSON. No markdown, no explanation, no code fences."
    )

    user = (
        "Match each directory name to the most appropriate Clockify project.\n\n"
        f"Directory names to match:\n{json.dumps(unmatched)}\n\n"
        f"Available Clockify projects (name → id):\n{json.dumps(project_map, indent=2)}\n\n"
        "Rules:\n"
        "- Return the project ID string if there is a clear, confident match.\n"
        "- Return null if there is no good match. Prefer null over a weak guess.\n"
        "- Every directory name in the input must appear as a key in your response.\n\n"
        f"Respond with exactly this JSON structure:\n"
        '{{"dirname1": "id_or_null", "dirname2": "id_or_null", ...}}'
    )

    response = anthropic_request(api_key, [{"role": "user", "content": user}], system)

    raw = response["content"][0]["text"].strip()
    log.info(f"Claude raw response: {raw}")

    # Strip markdown code fences if present (e.g. ```json ... ```)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]          # drop opening fence line
        raw = raw.rsplit("```", 1)[0].strip()  # drop closing fence

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude returned non-JSON:\n{raw}")

    if not isinstance(result, dict):
        raise RuntimeError(f"Claude returned unexpected type: {type(result)}")

    # Validate: returned IDs must exist in the project list
    valid_ids = {p["id"] for p in projects}
    validated = {}
    for dirname, project_id in result.items():
        if project_id is None:
            validated[dirname] = None
        elif project_id in valid_ids:
            validated[dirname] = project_id
        else:
            log.warning(f"Claude returned unknown project ID '{project_id}' for '{dirname}' — skipping")
            validated[dirname] = None

    return validated


# ---------------------------------------------------------------------------
# Fuzzy project finder  (for --link, no AI needed)
# ---------------------------------------------------------------------------

def fuzzy_find_project(name_query: str, projects: list) -> dict | None:
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

def confirm_match(dirname: str, project: dict, projects: list) -> dict | None:
    """
    Ask the user to confirm, reject, or override a proposed match.
    Returns the confirmed project dict, or None to skip.
    """
    print(f"  {dirname}  →  {project['name']}")
    answer = input("    [y/n/project name]: ").strip()
    if answer.lower() == "y" or answer == "":
        return project
    if answer.lower() == "n":
        return None
    # Treat as a project name query
    found = fuzzy_find_project(answer, projects)
    if found:
        print(f"    Using: {found['name']}")
        return found
    print(f"    No project matched '{answer}' — skipping.")
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_match(days: int, dry_run: bool, interactive: bool):
    config = load_config()
    anthropic_key = require_anthropic_key(config)

    mappings = load_project_mappings()

    print(f"Scanning logs from the last {days} days...")
    unmatched = collect_unmatched_dirnames(days, mappings)

    if not unmatched:
        print("All dirnames already mapped — nothing to do.")
        return

    print(f"Found {len(unmatched)} unmatched dirname(s): {', '.join(unmatched)}\n")

    print("Fetching Clockify projects...")
    try:
        projects = list_clockify_projects(config)
    except Exception as e:
        print(f"ERROR: Could not fetch Clockify projects: {e}")
        sys.exit(1)

    print(f"Asking Claude to match against {len(projects)} project(s)...\n")
    try:
        matches = match_with_claude(anthropic_key, unmatched, projects)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    project_by_id = {p["id"]: p for p in projects}
    to_save = {}
    null_count = 0

    if interactive:
        print("Review each proposed match  (y = accept, n = skip, or type a project name):\n")

    for dirname in unmatched:
        project_id = matches.get(dirname)
        if project_id and project_id in project_by_id:
            project = project_by_id[project_id]
            if interactive:
                confirmed = confirm_match(dirname, project, projects)
                if confirmed:
                    to_save[dirname] = confirmed["id"]
                    log.info(f"Matched '{dirname}' → {confirmed['name']} (interactive)")
                else:
                    null_count += 1
            else:
                to_save[dirname] = project_id
                print(f"  {dirname}  →  {project['name']}")
                log.info(f"Matched '{dirname}' → {project['name']}")
        else:
            if interactive:
                print(f"  {dirname}  →  (no confident match)")
                answer = input("    Type a project name to assign, or press Enter to skip: ").strip()
                if answer:
                    found = fuzzy_find_project(answer, projects)
                    if found:
                        to_save[dirname] = found["id"]
                        print(f"    Using: {found['name']}")
                        log.info(f"Manually mapped '{dirname}' → {found['name']}")
                    else:
                        print(f"    No project matched '{answer}' — skipping.")
                        null_count += 1
                else:
                    null_count += 1
            else:
                print(f"  {dirname}  →  (no confident match — will retry next run)")
                null_count += 1

    print()
    if dry_run:
        print(f"Dry run — {len(to_save)} match(es) proposed, {null_count} skipped. Nothing saved.")
    else:
        if to_save:
            mappings.update(to_save)
            save_project_mappings(mappings)
            print(f"Saved {len(to_save)} match(es) to project_mappings.json.")
        if null_count:
            print(f"{null_count} dirname(s) left unmatched — will retry on next run.")
        if not to_save and not null_count:
            print("Nothing to save.")


def cmd_link(dirname: str, project_name_query: str):
    """Fuzzy-link a dirname to a project by name. No Anthropic key needed."""
    config = load_config()

    print(f"Fetching Clockify projects...")
    try:
        projects = list_clockify_projects(config)
    except Exception as e:
        print(f"ERROR: Could not fetch Clockify projects: {e}")
        sys.exit(1)

    found = fuzzy_find_project(project_name_query, projects)
    if not found:
        print(f"No project matched '{project_name_query}'.")
        print("\nAvailable projects:")
        for p in projects:
            client = f"  [{p.get('clientName', '')}]" if p.get("clientName") else ""
            print(f"  {p['name']}{client}")
        sys.exit(1)

    mappings = load_project_mappings()
    mappings[dirname] = found["id"]
    save_project_mappings(mappings)
    print(f"Mapped '{dirname}'  →  {found['name']}  ({found['id']})")
    log.info(f"Linked '{dirname}' → {found['name']} via --link")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--link" in args:
        idx = args.index("--link")
        try:
            link_dirname = args[idx + 1]
            link_query   = args[idx + 2]
        except IndexError:
            print("Usage: python3 ai_matcher.py --link DIR \"Project Name\"")
            sys.exit(1)
        cmd_link(link_dirname, link_query)
    else:
        dry_run     = "--dry-run" in args
        interactive = "--interactive" in args
        days        = DEFAULT_DAYS
        if "--days" in args:
            idx = args.index("--days")
            try:
                days = int(args[idx + 1])
            except (IndexError, ValueError):
                print("Usage: python3 ai_matcher.py --days N")
                sys.exit(1)
        cmd_match(days=days, dry_run=dry_run, interactive=interactive)
