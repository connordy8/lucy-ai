"""Vercel endpoint — checks Beth's calendar and triggers the phone-reminder
workflow ONLY when a Zumba/Aquacise/Functional Strength class starts in
40-50 minutes.

Called every 5 minutes by cron-job.org. This replaces the unreliable
GitHub Actions cron schedule with a reliable external trigger.
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler


REMINDER_WINDOW_MIN = 40
REMINDER_WINDOW_MAX = 50
REMINDER_CLASSES = [
    "zumba", "aquacise", "functional strength", "ujam",
    "posture", "mat yoga", "foreverfit", "forever fit",
    "pickleball", "let's stretch", "lets stretch", "tai chi",
]
REMINDED_KEY = "bethReminded"


def _get_calendar_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY", "")
    if not creds_json:
        return None, None

    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    return service, calendar_id


def _has_upcoming_class():
    """Check if any reminder-eligible class needs a call right now.

    Two triggers:
    1. Normal: a class starts in 40-50 min (first reminder)
    2. Follow-up: Beth asked for a callback at a specific time
       (followUpRemindAt is within the current 5-min window)
    """
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")

    service, calendar_id = _get_calendar_service()
    if not service or not calendar_id:
        return False, "Calendar not configured"

    now = datetime.now(timezone.utc)
    now_pt = now.astimezone(PACIFIC)

    # --- Check 1: Follow-up reminders due NOW ---
    # Scan all events today for followUpRemindAt in the current window
    today_start = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now_pt.replace(hour=23, minute=59, second=59, microsecond=0)

    all_today = service.events().list(
        calendarId=calendar_id,
        timeMin=today_start.isoformat(),
        timeMax=today_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=30,
    ).execute().get("items", [])

    for ev in all_today:
        if ev.get("status") == "cancelled":
            continue
        private = ev.get("extendedProperties", {}).get("private", {})
        follow_up = private.get("followUpRemindAt", "")
        if not follow_up:
            continue
        try:
            follow_up_dt = datetime.fromisoformat(follow_up).astimezone(PACIFIC)
        except ValueError:
            continue
        # Is the follow-up time within now and now+6 min? (5-min cron + 1 min buffer)
        if now_pt <= follow_up_dt <= now_pt + timedelta(minutes=6):
            return True, (
                f"Follow-up reminder due: {ev.get('summary', '')} "
                f"(requested at {follow_up})"
            )

    # --- Check 2: Normal first reminder (class in 40-50 min) ---
    window_start = now + timedelta(minutes=REMINDER_WINDOW_MIN)
    window_end = now + timedelta(minutes=REMINDER_WINDOW_MAX)

    resp = service.events().list(
        calendarId=calendar_id,
        timeMin=window_start.isoformat(),
        timeMax=window_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=10,
    ).execute()

    events = resp.get("items", [])
    for ev in events:
        if ev.get("status") == "cancelled":
            continue

        summary = ev.get("summary", "").lower()
        if not any(cls in summary for cls in REMINDER_CLASSES):
            continue

        ext_props = ev.get("extendedProperties", {})
        private = ext_props.get("private", {})
        if private.get(REMINDED_KEY) or private.get("bethSkipReminder"):
            continue

        start = ev.get("start", {})
        if "dateTime" not in start:
            continue
        start_dt = datetime.fromisoformat(
            start["dateTime"]).astimezone(PACIFIC)
        if start_dt.hour < 10 or (start_dt.hour == 10 and start_dt.minute < 30):
            continue

        return True, f"Found: {ev.get('summary', '')} at {start['dateTime']}"

    return False, f"No reminders needed ({len(events)} events in normal window, {len(all_today)} events today)"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Auth check
        cron_secret = os.environ.get("CRON_SECRET", "").strip()
        auth_header = self.headers.get("Authorization", "")
        if not cron_secret or auth_header != f"Bearer {cron_secret}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        # Check calendar
        try:
            has_class, detail = _has_upcoming_class()
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Calendar check failed: {e}".encode())
            return

        if not has_class:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"No reminder needed. {detail}".encode())
            return

        # Trigger the phone-reminder workflow
        gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not gh_token:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"GITHUB_TOKEN not configured")
            return

        url = ("https://api.github.com/repos/connordy8/lucy-ai"
               "/actions/workflows/phone-reminder.yml/dispatches")
        payload = json.dumps({"ref": "main"}).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                f"Triggered reminder workflow. {detail} (status {resp.status})".encode())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Failed to trigger workflow: {e}".encode())
