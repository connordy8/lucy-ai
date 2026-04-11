"""One-off script: dump upcoming Zumba/Aquacise/Functional Strength classes."""
import json, os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build

PACIFIC = ZoneInfo("America/Los_Angeles")
REMINDER_CLASSES = ["zumba", "aquacise", "functional strength"]

creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"]
calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
creds = service_account.Credentials.from_service_account_info(
    json.loads(creds_json),
    scopes=["https://www.googleapis.com/auth/calendar.readonly"])
service = build("calendar", "v3", credentials=creds, cache_discovery=False)

now = datetime.now(PACIFIC)
end = now + timedelta(days=21)

resp = service.events().list(
    calendarId=calendar_id,
    timeMin=now.isoformat(),
    timeMax=end.isoformat(),
    singleEvents=True,
    orderBy="startTime",
    maxResults=100,
).execute()

for ev in resp.get("items", []):
    if ev.get("status") == "cancelled":
        continue
    summary = ev.get("summary", "")
    if not any(cls in summary.lower() for cls in REMINDER_CLASSES):
        continue
    start = ev.get("start", {}).get("dateTime", "")
    dt = datetime.fromisoformat(start).astimezone(PACIFIC)
    print(f"{dt.strftime('%a %b %-d')}  {dt.strftime('%-I:%M %p PT')}  {summary}")
