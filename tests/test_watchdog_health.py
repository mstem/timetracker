"""Tests for watchdog_health.sh.

The script reads the clock, HID idle time and the power-management log from
the machine; each has an env override (WATCHDOG_NOW, WATCHDOG_IDLE_SEC,
WATCHDOG_PMSET_LOG, WATCHDOG_LOG_DIR, WATCHDOG_NOTIFY) so these tests can
drive it without touching the real Mac or Telegram.

Run: python3 -m unittest discover tests
"""
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.environ.get("TEST_WATCHDOG_SCRIPT", os.path.join(ROOT, "watchdog_health.sh"))
DAY = "2026-11-23"


def ts(hhmm: str) -> int:
    """Local epoch seconds for HH:MM on the test day."""
    return int(time.mktime(datetime.strptime(f"{DAY} {hhmm}", "%Y-%m-%d %H:%M").timetuple()))


def pmset_line(hhmm: str, event: str) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(ts(hhmm)))
    return f"{stamp} Notification        \t{event}\n"


class WatchdogHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.logs)
        self.sent = os.path.join(self.tmp.name, "sent.txt")
        self.notify = os.path.join(self.tmp.name, "notify.sh")
        with open(self.notify, "w") as f:
            f.write(f'#!/bin/bash\necho "$1" >> "{self.sent}"\n')
        os.chmod(self.notify, 0o755)
        self.pmset = os.path.join(self.tmp.name, "pmset.log")
        self.state = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def run_watchdog(self, now, idle_sec, pmset_events, log_mtime=None):
        with open(self.pmset, "w") as f:
            for hhmm, event in pmset_events:
                f.write(pmset_line(hhmm, event))
        extra = getattr(self, "_extra", None)
        if extra:
            with open(self.pmset, "ab") as f, open(extra, "rb") as g:
                f.write(g.read())
        if log_mtime is not None:
            path = os.path.join(self.logs, f"{DAY}.json")
            with open(path, "w") as f:
                f.write("[]")
            os.utime(path, (ts(log_mtime), ts(log_mtime)))
        env = dict(
            os.environ,
            WATCHDOG_NOW=str(ts(now)),
            WATCHDOG_IDLE_SEC=str(idle_sec),
            WATCHDOG_PMSET_LOG=self.pmset,
            WATCHDOG_LOG_DIR=self.logs,
            WATCHDOG_NOTIFY=self.notify,
            WATCHDOG_STATE_FILE=self.state,
            LANG="en_US.UTF-8",
        )
        subprocess.run(["/bin/bash", SCRIPT], env=env, check=True)
        if not os.path.exists(self.sent):
            return []
        with open(self.sent) as f:
            return [line for line in f.read().splitlines() if line]

    # --- false alarms the watchdog must not send ---------------------------

    def test_no_alert_when_missing_log_and_display_off(self):
        # Matt on his phone all morning: display asleep, no log file.
        alerts = self.run_watchdog("12:42", idle_sec=4 * 3600, pmset_events=[
            ("08:30", "Display is turned on"),
            ("08:30", "Display is turned off"),
        ])
        self.assertEqual(alerts, [])

    def test_no_alert_when_missing_log_and_user_idle(self):
        # Display still on (e.g. a video wake lock) but nobody touching the Mac.
        alerts = self.run_watchdog("12:42", idle_sec=40 * 60, pmset_events=[
            ("09:00", "Display is turned on"),
        ])
        self.assertEqual(alerts, [])

    def test_no_alert_when_user_just_sat_down(self):
        # Display came on 5 minutes ago after a morning away: no log yet is normal.
        alerts = self.run_watchdog("12:42", idle_sec=0, pmset_events=[
            ("08:30", "Display is turned on"),
            ("08:30", "Display is turned off"),
            ("12:37", "Display is turned on"),
        ])
        self.assertEqual(alerts, [])

    def test_no_alert_when_stale_log_but_user_just_returned(self):
        # Log last written 09:00; away until 12:30 (display off), back now.
        alerts = self.run_watchdog("13:13", idle_sec=30, log_mtime="09:00", pmset_events=[
            ("08:00", "Display is turned on"),
            ("09:10", "Display is turned off"),
            ("12:30", "Display is turned on"),
        ])
        self.assertEqual(alerts, [])

    # --- genuine failures it must still catch ------------------------------

    def test_alerts_when_in_use_for_hours_with_no_log(self):
        alerts = self.run_watchdog("12:42", idle_sec=20, pmset_events=[
            ("09:30", "Display is turned on"),
        ])
        self.assertEqual(len(alerts), 1)
        self.assertIn("no log file for today", alerts[0])

    def test_alerts_when_in_use_and_log_stale(self):
        alerts = self.run_watchdog("13:00", idle_sec=60, log_mtime="10:00", pmset_events=[
            ("09:00", "Display is turned on"),
        ])
        self.assertEqual(len(alerts), 1)
        self.assertIn("hasn't updated", alerts[0])

    def test_no_alert_when_log_fresh(self):
        alerts = self.run_watchdog("13:00", idle_sec=60, log_mtime="12:50", pmset_events=[
            ("09:00", "Display is turned on"),
        ])
        self.assertEqual(alerts, [])

    def test_no_alert_when_no_display_event_today_and_user_just_sat_down(self):
        # Display stayed on overnight (or the pmset log rotated): no display-on
        # or wake event today. Midnight must not count as time at the Mac.
        alerts = self.run_watchdog("09:05", idle_sec=10, pmset_events=[])
        self.assertEqual(alerts, [])

    def test_no_alert_when_no_event_today_and_stale_log_from_earlier(self):
        alerts = self.run_watchdog("11:05", idle_sec=10, log_mtime="07:00", pmset_events=[])
        self.assertEqual(alerts, [])

    def test_no_event_today_alerts_once_in_use_long_enough(self):
        # With no display-on event, consecutive hourly checks that each find
        # Matt at the Mac build up in-use time until a genuine failure shows.
        self.assertEqual(self.run_watchdog("10:00", idle_sec=10, pmset_events=[]), [])
        self.assertEqual(self.run_watchdog("11:00", idle_sec=10, pmset_events=[]), [])
        alerts = self.run_watchdog("12:00", idle_sec=10, pmset_events=[])
        self.assertEqual(len(alerts), 1)
        self.assertIn("no log file for today", alerts[0])

    def test_no_event_today_idle_check_resets_stretch(self):
        self.run_watchdog("10:00", idle_sec=10, pmset_events=[])
        self.run_watchdog("11:00", idle_sec=30 * 60, pmset_events=[])  # away
        self.assertEqual(self.run_watchdog("12:00", idle_sec=10, pmset_events=[]), [])

    def test_non_utf8_bytes_in_pmset_log_do_not_hide_display_state(self):
        # Real pmset logs carry device names with stray non-UTF-8 bytes. The
        # display-on at 09:00 must still be read, so a genuine failure alerts.
        bad = b"2026-11-23 09:10:00 +0000 HID  \t\xe2\x80s Magic Trackpad eventType:11\n"
        with open(self.pmset + ".extra", "wb") as f:
            f.write(bad)
        self._extra = self.pmset + ".extra"
        alerts = self.run_watchdog("12:42", idle_sec=10, pmset_events=[
            ("09:00", "Display is turned on"),
        ])
        self.assertEqual(len(alerts), 1)

    def test_no_alert_outside_working_hours(self):
        alerts = self.run_watchdog("21:00", idle_sec=0, pmset_events=[
            ("09:00", "Display is turned on"),
        ])
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
