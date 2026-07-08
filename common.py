"""
Shared helpers for tracker.py and ai_matcher.py: config loading, the Clockify
and Anthropic HTTP clients, project-mapping storage, activity-key derivation,
blocklist/video-context checks, and idle-time detection.
"""

import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
MAPPINGS_PATH = os.path.join(BASE_DIR, "project_mappings.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

CLOCKIFY_API_BASE = "https://api.clockify.me/api/v1"
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

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: config.json not found at {CONFIG_PATH}")
        print("Run setup.sh first and fill in your API key and workspace ID.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for key in ("api_key", "workspace_id"):
        if not cfg.get(key) or cfg[key].startswith("<"):
            print(f"ERROR: '{key}' not set in config.json")
            sys.exit(1)
    return cfg


def require_anthropic_key(config: dict):
    """Return the Anthropic API key, or None if not configured (AI features are opt-in)."""
    key = config.get("anthropic_api_key", "")
    if not key or key.startswith("<"):
        return None
    return key


# ---------------------------------------------------------------------------
# Clockify
# ---------------------------------------------------------------------------

def clockify_request(method: str, path: str, api_key: str, body: dict = None):
    url = f"{CLOCKIFY_API_BASE}{path}"
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def list_clockify_projects(config: dict) -> list:
    """Return all non-archived projects in the workspace."""
    return clockify_request(
        "GET",
        f"/workspaces/{config['workspace_id']}/projects?archived=false&page-size=500",
        config["api_key"],
    )


def create_clockify_project(config: dict, name: str) -> dict:
    """Create a new Clockify project and return it."""
    return clockify_request(
        "POST",
        f"/workspaces/{config['workspace_id']}/projects",
        config["api_key"],
        {"name": name, "billable": False},
    )


# ---------------------------------------------------------------------------
# Project mappings  (shared format between tracker.py and ai_matcher.py)
# ---------------------------------------------------------------------------

def load_project_mappings() -> dict:
    if os.path.exists(MAPPINGS_PATH):
        with open(MAPPINGS_PATH) as f:
            raw = json.load(f)
        # Normalize on load too, in case the file was hand-edited or predates
        # normalize_key — later duplicate wins, but keys are normalized at
        # write time so collisions shouldn't occur in practice.
        return {normalize_key(k): v for k, v in raw.items()}
    return {}


def save_project_mappings(mappings: dict):
    """Write atomically to avoid corruption if the tracker daemon is running."""
    mappings = {normalize_key(k): v for k, v in mappings.items()}
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
