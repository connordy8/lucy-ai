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
REMINDER_CLASSES = ["zumba", "aquacise", "functional strength"]
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
    """Check if any reminder-eligible class starts in the 40-50 min window."""
    service, calendar_id = _get_calendar_service()
    if not service or not calendar_id:
        return False, "Calendar not configured"

    now = datetime.now(timezone.utc)
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

        # Skip already-reminded events
        ext_props = ev.get("extendedProperties", {})
        private = ext_props.get("private", {})
        if private.get(REMINDED_KEY) or private.get("bethSkipReminder"):
            continue

        # Skip classes before 10:30 AM PT
        start = ev.get("start", {})
        if "dateTime" not in start:
            continue
        from zoneinfo import ZoneInfo
        start_dt = datetime.fromisoformat(
            start["dateTime"]).astimezone(ZoneInfo("America/Los_Angeles"))
        if start_dt.hour < 10 or (start_dt.hour == 10 and start_dt.minute < 30):
            continue

        return True, f"Found: {ev.get('summary', '')} at {start['dateTime']}"

    return False, f"No classes in window ({len(events)} events checked)"


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
