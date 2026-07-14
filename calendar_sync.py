#!/usr/bin/env python3
"""
Google Calendar -> Kimai meeting sync.

Logs "external" calendar meetings to Kimai: any timed event that has at least
one guest whose email is NOT one of your own addresses (config: owner_emails).
Runs from the tracker's normal send path (midnight rollover / --send / startup
catch-up), so it obeys the same "catch up when the Mac next wakes" behavior.

Stdlib only, matching the rest of the project — Google OAuth (installed-app
loopback flow), token refresh, and the Calendar API are all plain urllib.

One-time setup per Google account:
    python3 calendar_sync.py --auth        # opens a browser, stores a refresh token
    python3 calendar_sync.py --list        # show authorized accounts
    python3 calendar_sync.py --test DATE   # dry-run: print qualifying meetings for a day

Requires in config.json:
    google_oauth_client_id / google_oauth_client_secret   (Google Cloud "Desktop app" OAuth client)
    owner_emails                                           (your addresses; guests in this set don't count)
    meetings_project_id / meetings_activity_id             (Kimai fallback when Claude can't classify)
    calendar_sync_enabled: true

Refresh tokens live in google_tokens.json (gitignored, chmod 600).
Already-synced meetings are tracked in synced_meetings.json so re-sends don't duplicate.
"""

import datetime
import http.server
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import common

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

log = logging.getLogger(__name__)

TOKENS_PATH = os.path.join(common.BASE_DIR, "google_tokens.json")
SYNCED_PATH = os.path.join(common.BASE_DIR, "synced_meetings.json")

OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

def _load_tokens() -> list:
    if os.path.exists(TOKENS_PATH):
        with open(TOKENS_PATH) as f:
            return json.load(f)
    return []


def _save_tokens(tokens: list):
    tmp = TOKENS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, TOKENS_PATH)
    os.chmod(TOKENS_PATH, 0o600)   # holds long-lived refresh tokens


def _load_synced() -> dict:
    if os.path.exists(SYNCED_PATH):
        with open(SYNCED_PATH) as f:
            return json.load(f)
    return {}


def _save_synced(synced: dict):
    tmp = SYNCED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(synced, f, indent=2)
    os.replace(tmp, SYNCED_PATH)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get_json(url: str, access_token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"}, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _access_token(config: dict, refresh_token: str) -> str:
    resp = _post_form(OAUTH_TOKEN_URL, {
        "client_id": config["google_oauth_client_id"],
        "client_secret": config["google_oauth_client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    return resp["access_token"]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_rfc3339(ts: str) -> datetime.datetime:
    """Parse a Google RFC3339 timestamp to a naive-UTC datetime."""
    ts = ts.replace("Z", "+00:00")           # fromisoformat handles 'Z' only in 3.11+
    dt = datetime.datetime.fromisoformat(ts)
    if dt.tzinfo is None:                    # shouldn't happen for timed events
        return dt
    return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _day_window_utc(date_str: str, config: dict):
    """Return (timeMin, timeMax) RFC3339 strings bounding the local calendar day."""
    day = datetime.date.fromisoformat(date_str)
    tzname = common.kimai_timezone(config)
    tz = ZoneInfo(tzname) if ZoneInfo is not None else datetime.timezone.utc
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + datetime.timedelta(days=1)
    return start.astimezone(datetime.timezone.utc).isoformat(), end.astimezone(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Event fetching + filtering
# ---------------------------------------------------------------------------

def _list_events(access_token: str, time_min: str, time_max: str, calendar_id: str = "primary") -> list:
    events, page_token = [], None
    while True:
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
            "showDeleted": "false",
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id)}/events?" + urllib.parse.urlencode(params)
        data = _get_json(url, access_token)
        events.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return events


def _external_guests(event: dict, owner_emails: set) -> list:
    """Return attendee emails that aren't the user's own (and aren't rooms/resources)."""
    out = []
    for a in event.get("attendees", []) or []:
        email = (a.get("email") or "").lower()
        if not email or a.get("resource"):
            continue
        if email not in owner_emails:
            out.append(email)
    return out


def _self_declined(event: dict) -> bool:
    for a in event.get("attendees", []) or []:
        if a.get("self") and a.get("responseStatus") == "declined":
            return True
    return False


def qualifying_meetings(config: dict, date_str: str) -> list:
    """Return de-duplicated external meetings for the day, each as a dict with
    start/end (naive UTC datetimes), summary, uid, and external guest emails."""
    owner_emails = {e.lower() for e in config.get("owner_emails", [])}
    time_min, time_max = _day_window_utc(date_str, config)
    tokens = _load_tokens()
    if not tokens:
        log.warning("Calendar sync enabled but no authorized accounts (run: calendar_sync.py --auth)")
        return []

    # Besides each account's own primary calendar, optionally read specific
    # shared calendars (config: extra_calendars, e.g. a personal Gmail whose
    # calendar is shared into a work account). Fetched through every account's
    # token; a calendar an account can't see just 404s and is skipped. Dedup by
    # UID keeps a calendar visible to two accounts from being counted twice.
    extra_calendars = config.get("extra_calendars") or []
    by_uid = {}
    for acct in tokens:
        try:
            token = _access_token(config, acct["refresh_token"])
        except Exception as e:
            log.error(f"Failed to authorize {acct.get('email','?')}: {e}")
            continue

        for calendar_id in ("primary", *extra_calendars):
            try:
                events = _list_events(token, time_min, time_max, calendar_id)
            except Exception as e:
                if calendar_id == "primary":
                    log.error(f"Failed to fetch events for {acct.get('email','?')}: {e}")
                else:
                    log.debug(f"{acct.get('email','?')} can't read calendar {calendar_id}: {e}")
                continue

            for ev in events:
                if ev.get("status") == "cancelled":
                    continue
                start, end = ev.get("start", {}), ev.get("end", {})
                if "dateTime" not in start or "dateTime" not in end:
                    continue    # all-day / date-only event
                if _self_declined(ev):
                    continue
                guests = _external_guests(ev, owner_emails)
                if not guests:
                    continue    # no external guest -> not in scope

                uid = ev.get("iCalUID") or ev.get("id")
                if uid in by_uid:
                    by_uid[uid]["guests"] = sorted(set(by_uid[uid]["guests"]) | set(guests))
                    continue
                by_uid[uid] = {
                    "uid": uid,
                    "summary": ev.get("summary", "(no title)"),
                    "start": _parse_rfc3339(start["dateTime"]),
                    "end": _parse_rfc3339(end["dateTime"]),
                    "guests": guests,
                }

    meetings = [m for m in by_uid.values() if m["end"] > m["start"]]
    meetings.sort(key=lambda m: m["start"])
    return meetings


# ---------------------------------------------------------------------------
# Classification -> Kimai project/activity
# ---------------------------------------------------------------------------

def _classify_meeting(config: dict, meeting: dict):
    """Return (project_id, activity_id) for a meeting. Tries the Claude
    classifier on the title + guest domains; falls back to the configured
    Meetings project/activity. Returns (None, None) if nothing is available."""
    fallback = (config.get("meetings_project_id"), config.get("meetings_activity_id"))
    anthropic_key = common.require_anthropic_key(config)
    if not anthropic_key:
        return fallback

    try:
        import ai_matcher
        projects = common.list_kimai_projects(config)
        resolve_activity = ai_matcher.build_activity_resolver(config, common.list_kimai_activities(config))
        domains = sorted({g.split("@", 1)[-1] for g in meeting["guests"]})
        context = {meeting["summary"]: [meeting["summary"], "guests from: " + ", ".join(domains)]}
        matches = ai_matcher.classify_keys_with_claude(anthropic_key, context, projects, common.load_project_mappings())
        pid = matches.get(meeting["summary"])
        if pid:
            activity_id = resolve_activity(pid)
            if activity_id:
                return pid, activity_id
    except Exception as e:
        log.error(f"Meeting classification failed for {meeting['summary']!r}: {e}")

    return fallback


# ---------------------------------------------------------------------------
# Public entrypoint (called by tracker.send_to_kimai)
# ---------------------------------------------------------------------------

def sync_day(config: dict, date_str: str) -> list:
    """Create Kimai timesheets for any not-yet-synced external meetings on
    `date_str`, and return the (start_utc, end_utc) intervals of ALL qualifying
    meetings that day (synced or not) so the tracker can suppress overlapping
    activity time. Never raises for a per-meeting failure."""
    meetings = qualifying_meetings(config, date_str)
    if not meetings:
        return []

    synced = _load_synced()
    done = set(synced.get(date_str, []))
    intervals = []

    for m in meetings:
        intervals.append((m["start"], m["end"]))
        if m["uid"] in done:
            continue

        project_id, activity_id = _classify_meeting(config, m)
        if not project_id or not activity_id:
            log.warning(f"No Kimai project/activity for meeting {m['summary']!r} — set meetings_project_id/meetings_activity_id")
            continue

        body = {
            "begin": common.kimai_local_time(m["start"], config),
            "end": common.kimai_local_time(m["end"], config),
            "project": project_id,
            "activity": activity_id,
            "description": m["summary"],
            "tags": "meeting,calendar",
            "billable": False,
            "exported": False,
        }
        try:
            common.kimai_request(config, "POST", "/timesheets", body)
            done.add(m["uid"])
            mins = int((m["end"] - m["start"]).total_seconds()) // 60
            log.info(f"Synced meeting [{date_str}] {m['summary']!r} → {mins}m (project {project_id}, activity {activity_id})")
            print(f"  ✓ meeting '{m['summary']}' — {mins} min")
        except urllib.error.HTTPError as e:
            log.error(f"Kimai HTTP {e.code} for meeting {m['summary']!r}: {e.read().decode()}")
        except Exception as e:
            log.error(f"Kimai error for meeting {m['summary']!r}: {e}")

    synced[date_str] = sorted(done)
    _save_synced(synced)
    return intervals


# ---------------------------------------------------------------------------
# OAuth (installed-app loopback flow) — one-time per account
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        _CodeHandler.code = params.get("code", [None])[0]
        _CodeHandler.error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorization complete — you can close this tab." if _CodeHandler.code else "Authorization failed."
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *args):
        pass   # silence the default stderr logging


def cmd_auth(config: dict):
    client_id = config.get("google_oauth_client_id")
    client_secret = config.get("google_oauth_client_secret")
    if common._is_unset(client_id) or common._is_unset(client_secret):
        print("Set google_oauth_client_id and google_oauth_client_secret in config.json first.")
        print("Create a 'Desktop app' OAuth client at https://console.cloud.google.com/apis/credentials")
        sys.exit(1)

    port = _free_port()
    redirect_uri = f"http://localhost:{port}/"
    auth_url = OAUTH_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",           # force a refresh_token even on re-auth
    })

    print(f"Opening browser to authorize a Google account...\nIf it doesn't open, visit:\n{auth_url}\n")
    _CodeHandler.code = _CodeHandler.error = None
    server = http.server.HTTPServer(("127.0.0.1", port), _CodeHandler)
    webbrowser.open(auth_url)
    server.handle_request()   # blocks until Google redirects back once

    if _CodeHandler.error or not _CodeHandler.code:
        print(f"Authorization failed: {_CodeHandler.error or 'no code returned'}")
        sys.exit(1)

    tok = _post_form(OAUTH_TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _CodeHandler.code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        print("No refresh token returned. Revoke prior access at "
              "https://myaccount.google.com/permissions and retry.")
        sys.exit(1)

    # The primary calendar's id is the account's email address.
    email = "unknown"
    try:
        primary = _get_json(f"{CALENDAR_API}/calendars/primary", tok["access_token"])
        email = primary.get("id", "unknown")
    except Exception:
        pass

    tokens = [t for t in _load_tokens() if t.get("email") != email]
    tokens.append({"email": email, "refresh_token": refresh_token})
    _save_tokens(tokens)
    print(f"Authorized {email}. {len(tokens)} account(s) stored.")


def cmd_list():
    tokens = _load_tokens()
    if not tokens:
        print("No authorized accounts. Run: python3 calendar_sync.py --auth")
        return
    print("Authorized Google accounts:")
    for t in tokens:
        print(f"  {t.get('email', '?')}")


def cmd_test(date_str: str):
    config = common.load_config()
    meetings = qualifying_meetings(config, date_str)
    if not meetings:
        print(f"No qualifying external meetings on {date_str}.")
        return
    print(f"{len(meetings)} qualifying meeting(s) on {date_str}:")
    for m in meetings:
        mins = int((m["end"] - m["start"]).total_seconds()) // 60
        print(f"  {m['start']:%H:%M}-{m['end']:%H:%M} ({mins}m)  {m['summary']}  ← {', '.join(m['guests'])}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    if "--auth" in args:
        cmd_auth(common.load_config())
    elif "--list" in args:
        cmd_list()
    elif "--test" in args:
        idx = args.index("--test")
        date = args[idx + 1] if idx + 1 < len(args) else datetime.date.today().isoformat()
        cmd_test(date)
    else:
        print(__doc__)
