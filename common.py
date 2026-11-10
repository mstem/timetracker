"""
Shared helpers for tracker.py and ai_matcher.py: config loading, the Kimai
and Anthropic HTTP clients, project-mapping storage, activity-key derivation,
blocklist/video-context checks, and idle-time detection.

Time backend is Kimai (self-hosted, https://github.com/kimai/kimai). Kimai's
data model is Customer -> Project -> Activity, and every timesheet needs BOTH
a project and an activity, so mapping values are {"project": id, "activity": id}
(see normalize_mapping_value). Timesheet begin/end are local wall-clock time in
the instance timezone, not UTC — see kimai_local_time.
"""

import datetime
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo   # stdlib 3.9+, backed by the system tzdata on macOS
except ImportError:  # pragma: no cover - very old Python
    ZoneInfo = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
MAPPINGS_PATH = os.path.join(BASE_DIR, "project_mappings.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
# Phone samples live in their own directory: the tracker daemon does an
# unlocked read-modify-write of logs/<date>.json every 2s, so a second
# writer sharing that file would silently truncate Mac spans.
ANDROID_LOGS_DIR = os.path.join(LOGS_DIR, "android")
ANDROID_CATEGORIES_PATH = os.path.join(BASE_DIR, "android_categories.json")

DEFAULT_KIMAI_TIMEZONE = "UTC"
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"

# Directory names that are shell/OS artifacts, not project directories
# (lowercase — compared case-insensitively, see is_noise_dirname)
SKIP_DIRNAMES = {"terminal", "home", "~", "desktop", "downloads", "documents"}

DEFAULT_VIDEO_APPS = ["zoom.us", "FaceTime", "QuickTime Player", "VLC"]
DEFAULT_VIDEO_DOMAINS = [
    "youtube.com", "netflix.com", "vimeo.com", "twitch.tv", "meet.google.com", "zoom.us",
]
DEFAULT_IDLE_THRESHOLD_SECONDS = 600
# Video playback legitimately produces no HID input, so the normal idle
# threshold would cut a film short. But the bypass needs a ceiling: an
# unbounded one bills a movie left running overnight as 14h of work.
DEFAULT_VIDEO_IDLE_THRESHOLD_SECONDS = 7200
DEFAULT_MIN_DURATION_SECONDS = {"terminal": 1, "chrome": 60, "app": 60, "android": 120}

# Domains that are pure auth infrastructure — a sign-in flow never carries
# project signal regardless of title — so these are never sent to Claude for
# classification at all (always fall back to the default project_id, if
# any). Search engines are deliberately NOT here: their titles can carry
# real signal, at the cost of the coarse per-domain aggregation risk
# described in the README (a confident match can attribute unrelated future
# searches to the same project once cached).
DEFAULT_GENERIC_DOMAINS = [
    "accounts.google.com", "myaccount.google.com",
    "login.microsoftonline.com", "okta.com",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _is_unset(val) -> bool:
    return not val or (isinstance(val, str) and val.startswith("<"))


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: config.json not found at {CONFIG_PATH}")
        print("Run setup.sh first and fill in your Kimai URL and API token.")
        sys.exit(1)
    os.chmod(CONFIG_PATH, 0o600)  # holds API keys — keep out of reach of other local users
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for key in ("kimai_url", "kimai_token"):
        if _is_unset(cfg.get(key)):
            print(f"ERROR: '{key}' not set in config.json")
            sys.exit(1)
    cfg["kimai_url"] = cfg["kimai_url"].rstrip("/")
    return cfg


def require_anthropic_key(config: dict):
    """Return the Anthropic API key, or None if not configured (AI features are opt-in)."""
    key = config.get("anthropic_api_key", "")
    return None if _is_unset(key) else key


# ---------------------------------------------------------------------------
# Kimai
# ---------------------------------------------------------------------------

KIMAI_USER_AGENT = "timetracker/1.0 (+https://github.com/civictechguide/timetracker)"


def kimai_request(config: dict, method: str, path: str, body: dict = None):
    """Call the Kimai REST API. Auth is a Bearer API token (Kimai 2.x).

    `path` is relative to /api, e.g. "/timesheets". Returns the decoded JSON,
    or None for empty (204) responses.
    """
    url = f"{config['kimai_url']}/api{path}"
    headers = {
        "Authorization": f"Bearer {config['kimai_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Cloudflare in front of Kimai rejects urllib's default UA outright
        # (error 1010, browser_signature_banned). Identify ourselves instead.
        "User-Agent": KIMAI_USER_AGENT,
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def list_kimai_projects(config: dict) -> list:
    """Return all visible (non-archived) Kimai projects."""
    return kimai_request(config, "GET", "/projects?visible=1") or []


def list_kimai_activities(config: dict, project_id=None) -> list:
    """Return visible Kimai activities, optionally scoped to a project (also
    includes global activities when a project is given)."""
    path = "/activities?visible=1"
    if project_id is not None:
        path += f"&project={project_id}&globals=true"
    return kimai_request(config, "GET", path) or []


def list_kimai_customers(config: dict) -> list:
    return kimai_request(config, "GET", "/customers?visible=1") or []


def create_kimai_customer(config: dict, name: str) -> dict:
    # visible=True is required: Kimai's API defaults new entities to hidden, and
    # a hidden customer is not an accepted "choice" when creating a project under it.
    return kimai_request(config, "POST", "/customers", {"name": name, "country": "US", "currency": "USD", "timezone": kimai_timezone(config), "visible": True})


def create_kimai_project(config: dict, name: str, customer_id: int) -> dict:
    return kimai_request(config, "POST", "/projects", {"name": name, "customer": customer_id, "visible": True})


def create_kimai_activity(config: dict, name: str, project_id=None) -> dict:
    """Create an activity. Omit project_id for a global activity usable everywhere."""
    body = {"name": name, "visible": True}
    if project_id is not None:
        body["project"] = project_id
    return kimai_request(config, "POST", "/activities", body)


def ensure_kimai_tag(config: dict, name: str):
    """Make sure a timesheet tag exists.

    Kimai silently drops a `tags` value naming a tag it doesn't know, so a
    POST /timesheets carrying an unknown tag succeeds with no tag attached and
    no error. Creating it first is the only way the tag sticks. Re-creating an
    existing tag is harmless. Note the tags API on this Kimai version has no
    PATCH and no GET-by-id, and new tags come back visible=false while
    GET /tags lists only visible ones, so the tag cannot be looked up or made
    visible through the API — it works for tagging regardless.
    """
    try:
        kimai_request(config, "POST", "/tags", {"name": name})
    except Exception as e:
        log.debug(f"ensure_kimai_tag({name!r}): {e}")


def kimai_active_timesheet(config: dict) -> list:
    """Return the user's currently-running timesheets (empty if none)."""
    return kimai_request(config, "GET", "/timesheets/active") or []


# ---------------------------------------------------------------------------
# Kimai timestamps
# ---------------------------------------------------------------------------

def kimai_timezone(config: dict) -> str:
    return config.get("kimai_timezone") or DEFAULT_KIMAI_TIMEZONE


def check_kimai_timezone(config: dict):
    """Warn when `kimai_timezone` disagrees with the Kimai user's own timezone.

    Kimai stores timesheet begin/end as naive local wall-clock time and reads it
    back in the *user's* configured zone, so a mismatch here silently shifts
    every entry by the offset between the two with no error anywhere. This went
    unnoticed for months with config on America/New_York and the Kimai user on
    Europe/Lisbon, putting every entry 5 hours early.
    """
    configured = kimai_timezone(config)
    try:
        actual = (kimai_request(config, "GET", "/users/me") or {}).get("timezone")
    except Exception as e:
        log.debug(f"Could not read the Kimai user timezone: {e}")
        return
    if actual and actual != configured:
        log.error(
            f"kimai_timezone is {configured!r} but the Kimai user's timezone is "
            f"{actual!r} — every entry sent will be shifted by the offset between "
            f"them. Set kimai_timezone to {actual!r} in config.json."
        )


def kimai_local_time(dt_utc: datetime.datetime, config: dict) -> str:
    """Convert a naive-UTC datetime to the Kimai instance's local wall-clock
    time in the HTML5 format Kimai expects (no trailing 'Z')."""
    aware = dt_utc.replace(tzinfo=datetime.timezone.utc)
    tz = config.get("kimai_timezone") or DEFAULT_KIMAI_TIMEZONE
    if ZoneInfo is not None:
        try:
            aware = aware.astimezone(ZoneInfo(tz))
        except Exception:
            log.warning(f"Unknown kimai_timezone {tz!r}; sending UTC wall-clock time")
    return aware.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Project mappings  (shared format between tracker.py and ai_matcher.py)
# ---------------------------------------------------------------------------
#
# A mapping value is a dict {"project": <kimai project id>, "activity": <kimai
# activity id>} — Kimai timesheets require both. normalize_mapping_value keeps
# reading the legacy Clockify format (a bare project-id string) so old files and
# hand edits still load; those legacy values carry no activity and are treated
# as project-only (the caller falls back to the default activity).

def normalize_mapping_value(val) -> dict:
    """Coerce a stored mapping value into {"project": id, "activity": id|None},
    plus "manual": true on a mapping set by hand.
    Accepts the legacy Clockify form (a bare project-id string) too."""
    if isinstance(val, dict):
        out = {"project": val.get("project"), "activity": val.get("activity")}
        if val.get("manual"):
            out["manual"] = True   # set by hand: beats the CTFG title rule
        return out
    return {"project": val, "activity": None}   # legacy: bare Clockify project id


def load_project_mappings() -> dict:
    if os.path.exists(MAPPINGS_PATH):
        with open(MAPPINGS_PATH) as f:
            raw = json.load(f)
        # Normalize keys and values on load too, in case the file was
        # hand-edited or predates the current format — later duplicate wins,
        # but keys are normalized at write time so collisions shouldn't occur.
        return {normalize_key(k): normalize_mapping_value(v) for k, v in raw.items()}
    return {}


def save_project_mappings(mappings: dict):
    """Write atomically to avoid corruption if the tracker daemon is running."""
    mappings = {normalize_key(k): normalize_mapping_value(v) for k, v in mappings.items()}
    tmp = MAPPINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(mappings, f, indent=2)
    os.replace(tmp, MAPPINGS_PATH)


# ---------------------------------------------------------------------------
# Activity key extraction
# ---------------------------------------------------------------------------

def window_dir(window_name: str) -> str:
    """Extract the leading directory segment from a Terminal window title."""
    return window_name.split(" — ")[0].strip()


def path_key(path: str) -> str:
    """Turn a shell working directory into a stable project-level mapping key.

    iTerm2 reports a real cwd rather than a title, so the key comes from the
    path instead of from window text. Everything under ~/Projects collapses to
    its project root, so ~/Projects/foo/src and ~/Projects/foo/tests both land
    on `~/projects/foo` instead of splintering into a key per subdirectory.

    The `~/projects/<name>` shape matches the keys already in
    project_mappings.json, so existing mappings keep working.
    """
    if not path:
        return ""
    path = path.strip()
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    parts = [p for p in path.split("/") if p]
    # parts[0] == "~" for anything under the home directory
    if len(parts) >= 3 and parts[0] == "~" and parts[1].lower() == "projects":
        parts = parts[:3]
    return normalize_key("/".join(parts))


def normalize_key(key: str) -> str:
    """
    Canonicalize a mapping/aggregation key so case-only variants (e.g. a
    Terminal window title showing "~/Projects/foo" one day and
    "~/projects/foo" the next, depending on shell prompt config) are treated
    as the same activity rather than silently falling back to the default
    project. Applied everywhere a key is derived, stored, or looked up.
    """
    return key.lower()


def is_noise_dirname(dirname: str) -> bool:
    if not dirname or len(dirname) <= 1:
        return True
    if dirname.lower() in SKIP_DIRNAMES:
        return True
    if dirname.startswith("-"):   # shell artifacts like "-zsh"
        return True
    return False


_CHROME_PROFILE = re.compile(r"^(.*?) - Google Chrome( - .+)?$")


def chrome_window_title(window_title: str, tab_title: str) -> str:
    """The title to record for a Chrome window: the active tab's title, then
    the window's own name when the user gave it one, then the profile.

    "EVENS - Google Chrome - Matt (evensfoundation.eu)" with the OKRs doc in
    front becomes "OKRs - Google Docs - EVENS - Google Chrome - Matt
    (evensfoundation.eu)". The profile stays at the end, where the classifier
    reads which organization the window belongs to."""
    if not tab_title:
        return window_title
    m = _CHROME_PROFILE.match(window_title or "")
    if not m:
        return window_title
    name, profile = m.group(1), m.group(2) or ""
    if name == tab_title:
        return window_title
    return f"{tab_title} - {name} - Google Chrome{profile}"


def chrome_domain(url: str) -> str:
    """Normalize a URL down to a bare domain, for use as a mapping key."""
    if not url:
        return "internal"
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or "internal"


def strip_url_params(url: str) -> str:
    """Drop the query string and fragment before a URL is stored anywhere —
    they can carry OAuth codes/session tokens, and nothing downstream needs
    more than the domain."""
    if not url:
        return url
    return urlparse(url)._replace(query="", fragment="").geturl()


def activity_key(source: str, window: str, url: str = None, app: str = None,
                 path: str = None) -> str:
    """Compute the mapping/aggregation key for a captured activity.

    `path` is the shell's working directory, available from iTerm2 but not from
    Terminal.app. When present it wins: it is the real answer, where a window
    title is a guess that Claude Code overwrites with its own status text.
    """
    if source == "terminal":
        key = path_key(path) if path else window_dir(window)
    elif source == "chrome":
        key = f"chrome:{chrome_domain(url)}"
    elif source == "android":
        # Deliberately its own namespace, not "app:": phone Gmail and desktop
        # Gmail are different activities and may bill to different projects.
        key = f"android:{app}"
    else:
        key = f"app:{app}"
    return normalize_key(key)


def entry_key(entry: dict) -> str:
    """
    Return the mapping/aggregation key for a log entry, new or legacy.

    Legacy entries (written before source/key/app/url existed) only have
    `window`; infer a Terminal dirname key unless it carries the old WIP
    Chrome marker, in which case fall back to the raw string (matches the
    grouping already-logged Chrome WIP entries got before this change).
    """
    if "key" in entry:
        return normalize_key(entry["key"])
    window = entry.get("window", "")
    if window.startswith("[Chrome] "):
        return normalize_key(window)
    return normalize_key(window_dir(window))


# ---------------------------------------------------------------------------
# Title-split keys
# ---------------------------------------------------------------------------
#
# Some keys mix many kinds of work under one name. One Slack app holds the Evens
# and the CTFG (Superbloom) workspaces; `chrome:internal` holds every Chrome
# window whose URL the tracker cannot read, which is most named windows
# ("EVENS - Google Chrome - Matt (evensfoundation.eu)"); a home-directory
# terminal holds whatever Claude session was started there. Mapped as one key,
# each can only land on one project, and that project was Misc. For these the
# window title is the signal, so each title becomes its own key:
# "app:slack | joe (dm) - evens foundation - slack".

DEFAULT_TITLE_SPLIT_KEYS = [
    "chrome:internal", "chrome:newtab", "app:slack", "~", "~/projects/todoist-triage",
]
DEFAULT_TITLE_SPLIT_PREFIXES = ["~/.claude"]
TITLE_SEP = " | "

# Claude Code puts a spinner glyph in front of the session name; Slack appends an
# unread count; iTerm appends the window size. None of them name the work.
_TITLE_LEAD = re.compile(r"^[\s\u2733\u25d0-\u25d3\u2800-\u28ff\u00b7\u2022*]+")
# Slack's "- 2 new items", a tab's leading "(3) ", and Gmail's "Inbox (12)". A
# number in brackets elsewhere ("Budget (2026)") is part of the name.
_TITLE_UNREAD = re.compile(r"\s+-\s+\d+\s+new items?(?=\s+-\s)|^\(\d[\d,]*\)\s*|\s\(\d[\d,]*\)(?=\s-\s.*\bgmail\b)", re.I)
_TITLE_SIZE = re.compile(r"\s*\u2014\s*\d+\s*[\u2715x\u00d7\u2013-]\s*\d+\s*$")
# Chrome decorates the title with passing state: memory warnings, a network
# error, the camera light, the tab group's status mark. None of it names the
# work, and left in, one window becomes a new key every few minutes.
_TITLE_VOLATILE = [
    re.compile(r"\s+-\s+high memory usage\s+-\s+[\d.,]+\s*[kmg]b", re.I),
    re.compile(r"\s+-\s+(network error|camera and microphone recording|camera recording|microphone recording|audio playing)(?=\s+-\s)", re.I),
    re.compile(r"[\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff\U0001f000-\U0001faff\ufe0f]"),
]
_GENERIC_TITLES = {"", "slack", "granola", "today", "beeper", "python", "bash", "zsh",
                   "gh", "env", "id", "shelllauncher", "claude code", "new tab"}


def normalize_title(title: str) -> str:
    """The part of a window title that names the work, lowercased, or "" when
    nothing in it does. An incognito window is never named: it stays out of
    the mappings file and the model's prompt."""
    if "(incognito)" in (title or "").lower():
        return ""
    t = _TITLE_LEAD.sub("", title or "")
    t = _TITLE_UNREAD.sub("", t)
    t = _TITLE_SIZE.sub("", t)
    for pattern in _TITLE_VOLATILE:
        t = pattern.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t.lower() in _GENERIC_TITLES or t.startswith("-") or len(t) <= 2:
        return ""
    if len(t) > 120:
        # Shorten the front, never the end: a Chrome title ends with the
        # profile, which is what profile_projects reads.
        m = re.search(r" - Google Chrome( - .+)?$", t, re.I)
        tail = m.group(0) if m else ""
        t = t[: max(40, 120 - len(tail))].rstrip() + tail
    return normalize_key(t)


def profile_mapping(key: str, config: dict) -> dict:
    """The mapping for the Chrome profile named at the end of a title key, from
    `profile_projects` in config.json, or {}. A Chrome title ends with its
    profile ("- matt (evensfoundation.eu)"), and the profile says whose work
    it was even when the rest of the title says nothing."""
    if TITLE_SEP not in key or not key.startswith("chrome:"):
        return {}
    title = key.split(TITLE_SEP, 1)[1]
    for rule in (config or {}).get("profile_projects", []):
        if title.endswith(normalize_key(rule["suffix"])):
            return rule
    return {}


# A title that names CTFG work never files under Evens, whatever folder, domain,
# profile, saved mapping or model answer says otherwise. Evens hours feed the
# Evens weekly report, and CTFG time there misreports the week to Evens.
# `ctfg_title_rule` in config.json names the CTFG project to use when the
# title's own mapping is not already a CTFG project:
# {"project": 63, "activity": 76}. Without that key the rule is off.
CTFG_TITLE = re.compile(
    r"\bdaniel\b|\bctfg\b|civic\s*tech\s*field\s*guide|\bfield\s*guide\b"
    r"|\bsuperbloom\b|\bsimply\s*secure\b", re.I)


def title_rule(key: str, config: dict):
    """The `ctfg_title_rule` from config when `key` is a title key whose title
    names CTFG work, else None."""
    rule = (config or {}).get("ctfg_title_rule")
    if not rule or TITLE_SEP not in key:
        return None
    return rule if CTFG_TITLE.search(key.split(TITLE_SEP, 1)[1]) else None


def _same_customer(project, other, project_customer: dict) -> bool:
    if project == other:
        return True
    customer = (project_customer or {}).get(other)
    return customer is not None and project_customer.get(project) == customer


def needs_project_customer(config: dict) -> bool:
    """Whether resolving keys needs Kimai's project-to-customer table."""
    return bool(config.get("context_keys") or config.get("profile_projects")
                or config.get("ctfg_title_rule"))


def rule_activity(key: str, project, config: dict):
    """The rule's activity when the CTFG rule put its own project on `key`,
    so a saved mapping carries the activity the rule names. None otherwise."""
    rule = title_rule(key, config)
    if rule and project == rule["project"]:
        return rule.get("activity")
    return None


def enforce_title_rule(key: str, project, config: dict, project_customer: dict = None):
    """The project to save for `key`: `project` itself, unless the title names
    CTFG work and `project` is not a CTFG project, in which case the rule's."""
    rule = title_rule(key, config)
    if not rule or (project and _same_customer(project, rule["project"], project_customer)):
        return project
    return rule["project"]


def resolve_mapping(key: str, mappings: dict, default_project, default_activity,
                    config: dict = None, project_customer: dict = None) -> tuple:
    """(project, activity) for a Mac key. A title key with no answer of its
    own falls back to its Chrome profile, then to the key it was split from,
    then to the default: a title that names nothing must not undo what the
    profile or the base key already says.

    The profile also settles the organization. A title's own mapping is kept
    when it is a project of the profile's customer (an Evens window on the
    Digital citizenry doc stays on Digital citizenry), and overruled when the
    model put a work-profile window under another customer.

    The CTFG title rule comes before all of that, except for a mapping set
    by hand."""
    own = mappings.get(key) or {}
    # A mapping Matt set by hand ("manual": true, written by --link and the
    # interactive matcher) is the way to correct the rule when it misfires.
    if own.get("manual") and own.get("project"):
        return own["project"], own.get("activity") or default_activity
    rule = title_rule(key, config)
    if rule:
        # A specific CTFG project wins over the rule's general one: first the
        # title's own, then its profile's or its folder's. The title's own
        # mapping is often just the rule's project, saved when the rule
        # overruled the model, so it ranks last.
        base = mappings.get(key.split(TITLE_SEP, 1)[0]) or {}
        candidates = [own, profile_mapping(key, config) or {}, base]
        for mapping in candidates:
            p = mapping.get("project")
            if p and p != rule["project"] and _same_customer(p, rule["project"], project_customer):
                return p, mapping.get("activity") or default_activity
        return rule["project"], (own.get("activity") if own.get("project") == rule["project"] else None) \
            or rule.get("activity") or default_activity
    base = mappings.get(key.split(TITLE_SEP, 1)[0]) or {}
    profile = profile_mapping(key, config)
    if (profile and own.get("project") and project_customer
            and project_customer.get(own["project"]) != project_customer.get(profile["project"])):
        own = {}
    for mapping in (own, profile, base):
        if mapping.get("project") and mapping["project"] != default_project:
            return mapping["project"], mapping.get("activity") or default_activity
    # A key deliberately pinned to the default project keeps its own activity.
    return default_project, own.get("activity") or base.get("activity") or default_activity


def _split_lists(config: dict) -> tuple:
    keys = frozenset(normalize_key(k) for k in config.get("title_split_keys", DEFAULT_TITLE_SPLIT_KEYS))
    prefixes = tuple(normalize_key(p) for p in config.get("title_split_prefixes", DEFAULT_TITLE_SPLIT_PREFIXES))
    return keys, prefixes


def assign_context(entries: list, config: dict, resolve, project_customer: dict) -> tuple:
    """File time in a `context_keys` app (Granola) under the work going on
    around it.

    Granola's window says "Granola" and nothing else, but it is only ever open
    for a meeting, and the meeting belongs to whichever organization the
    nearest work in time belongs to. `context_keys` maps a key to the customer
    ids it may land on, plus a fallback project for a day with no such work:
    {"app:granola": {"customers": [4, 2], "fallback": {"project": 23, "activity": 22}}}.

    Returns (entries, extra_mappings). Matching entries are re-keyed to
    "<key> | context <project>" and extra_mappings resolves each new key; it is
    meant for this send only and is never saved."""
    rules = {normalize_key(k): v for k, v in (config.get("context_keys") or {}).items()}
    if not rules:
        return entries, {}
    work = []
    for e in entries:
        k = entry_key(e)
        if k.split(TITLE_SEP, 1)[0] in rules:
            continue
        project, activity = resolve(k)
        work.append((e["start"], e["end"], project, activity))
    out, extra = [], {}
    for e in entries:
        base = entry_key(e).split(TITLE_SEP, 1)[0]
        rule = rules.get(base)
        if not rule or title_rule(entry_key(e), config):
            # A title naming CTFG work goes where the CTFG rule sends it, not
            # to the organization of the nearest work.
            out.append(e)
            continue
        customers = set(rule.get("customers", []))
        best = None
        for start, end, project, activity in work:
            if project_customer.get(project) not in customers:
                continue
            if end <= e["start"]:
                gap = _seconds_between(end, e["start"])
            elif start >= e["end"]:
                gap = _seconds_between(e["end"], start)
            else:
                gap = 0
            if best is None or gap < best[0]:
                best = (gap, project, activity)
        if best:
            target = {"project": best[1], "activity": best[2]}
        else:
            target = rule.get("fallback") or {}
        if target.get("project"):
            key = f"{base}{TITLE_SEP}context {target['project']}"
            extra[key] = target
            e = {**e, "key": key}
        out.append(e)
    return out, extra


def parse_z(ts: str) -> datetime.datetime:
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def _seconds_between(a: str, b: str) -> float:
    return (parse_z(b) - parse_z(a)).total_seconds()


def is_title_split(base_key: str, config: dict, mappings: dict) -> bool:
    """Whether time under `base_key` should be filed per window title.

    True for the configured mixed keys, and for any key mapped straight to the
    default project: a key that could only be sent to Misc as a whole is a key
    whose titles are worth reading one by one."""
    keys, prefixes = _split_lists(config)
    if base_key in keys or base_key.startswith(prefixes):
        return True
    default = config.get("default_project_id")
    mapping = mappings.get(base_key) or {}
    return default is not None and mapping.get("project") == default


def split_by_title(entries: list, config: dict, mappings: dict) -> list:
    """Re-key Mac entries under a title-split key to `<key> | <title>`. Entries
    whose title names nothing keep their base key. Returns new dicts; the log
    entries themselves are not touched.

    A title naming CTFG work is split out of any key, split or not, so the
    CTFG title rule can see it: otherwise it would be summed into its folder's
    or domain's one timesheet and filed wherever that goes."""
    out = []
    decided = {}
    titles = {}
    ctfg_rule = bool((config or {}).get("ctfg_title_rule"))
    for e in entries:
        base = entry_key(e)
        if base not in decided:
            eligible = TITLE_SEP not in base and not base.startswith("android:")
            decided[base] = (eligible, eligible and is_title_split(base, config, mappings))
        eligible, split = decided[base]
        if eligible and e.get("source") != "android":
            window = e.get("window", "")
            if window not in titles:
                t = normalize_title(window)
                titles[window] = (t, bool(t and ctfg_rule and CTFG_TITLE.search(t)))
            title, names_ctfg = titles[window]
            if title and (split or names_ctfg):
                e = {**e, "key": base + TITLE_SEP + title}
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Blocklist / video-context checks
# ---------------------------------------------------------------------------

def _domain_matches(domain: str, blocklist: list) -> bool:
    for d in blocklist:
        d = d.lower()
        if domain == d or domain.endswith("." + d):
            return True
    return False


def is_blocked(source: str, app: str, url: str, config: dict) -> bool:
    if app in config.get("blocked_apps", []):
        return True
    if source == "chrome" and _domain_matches(chrome_domain(url), config.get("blocked_domains", [])):
        return True
    return False


def is_video_context(source: str, app: str, url: str, config: dict) -> bool:
    if app in config.get("video_apps", DEFAULT_VIDEO_APPS):
        return True
    if source == "chrome" and _domain_matches(chrome_domain(url), config.get("video_domains", DEFAULT_VIDEO_DOMAINS)):
        return True
    return False


def is_generic_domain(domain: str, config: dict) -> bool:
    """
    True for domains that are infrastructure (sign-in flows, bare search
    homepages) rather than a destination — deliberately an EXACT match, not a
    suffix match like _domain_matches: "google.com" must not swallow
    "mail.google.com"/"calendar.google.com"/"docs.google.com", which carry
    real project signal.
    """
    generic = {d.lower() for d in config.get("generic_domains", DEFAULT_GENERIC_DOMAINS)}
    return domain in generic


# ---------------------------------------------------------------------------
# Idle detection
# ---------------------------------------------------------------------------

def get_idle_seconds():
    """Seconds since the last HID (mouse/keyboard) event, or None if unreadable.

    None means "can't tell", not "not idle" — returning 0.0 on failure used to
    read as active input and kept billing time to the frontmost app while ioreg
    was timing out.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if not m:
            log.debug("ioreg returned no HIDIdleTime")
            return None
        return int(m.group(1)) / 1_000_000_000
    except Exception as e:
        log.debug(f"ioreg idle-time read failed: {e}")
        return None


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


def strip_markdown_fences(raw: str) -> str:
    """Strip ```json ... ``` or ``` ... ``` code fences, if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()
    return raw
