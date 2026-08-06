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
DEFAULT_MIN_DURATION_SECONDS = {"terminal": 1, "chrome": 60, "app": 60}

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


def kimai_active_timesheet(config: dict) -> list:
    """Return the user's currently-running timesheets (empty if none)."""
    return kimai_request(config, "GET", "/timesheets/active") or []


# ---------------------------------------------------------------------------
# Kimai timestamps
# ---------------------------------------------------------------------------

def kimai_timezone(config: dict) -> str:
    return config.get("kimai_timezone") or DEFAULT_KIMAI_TIMEZONE


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
    """Coerce a stored mapping value into {"project": id, "activity": id|None}.
    Accepts the legacy Clockify form (a bare project-id string) too."""
    if isinstance(val, dict):
        return {"project": val.get("project"), "activity": val.get("activity")}
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


def activity_key(source: str, window: str, url: str = None, app: str = None) -> str:
    """Compute the mapping/aggregation key for a captured activity."""
    if source == "terminal":
        key = window_dir(window)
    elif source == "chrome":
        key = f"chrome:{chrome_domain(url)}"
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

def get_idle_seconds() -> float:
    """Seconds since the last HID (mouse/keyboard) event. Fails open (0.0) on error."""
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1_000_000_000 if m else 0.0
    except Exception as e:
        log.debug(f"ioreg idle-time read failed: {e}")
        return 0.0


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
