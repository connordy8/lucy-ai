"""Call Beth 45 minutes before Zumba, Aquacise, and Functional Strength classes.

Uses Lucy (via Vapi) to make a friendly conversational reminder
call. Only triggers for Zumba, Aquacise, and Functional Strength.

Runs every 5 minutes via GitHub Actions. Uses Google Calendar
extended properties to track which events already got a call,
so Beth never gets duplicate reminders.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("phone_reminder")

# How far ahead to look for upcoming events (minutes)
REMINDER_WINDOW_MIN = 40  # Don't call if less than 40 min away
REMINDER_WINDOW_MAX = 50  # Don't call if more than 50 min away
# (targets ~45 min before class, with 5-min cron tolerance)

# Only remind for these classes (case-insensitive partial match)
REMINDER_CLASSES = ["zumba", "aquacise", "functional strength"]

# Extended property key used to mark events we've already called about
REMINDED_KEY = "bethReminded"

# Vapi config
VAPI_API = "https://api.vapi.ai"
ASSISTANT_ID = "3c6d4439-1323-4d76-ba0d-548f9854f570"
PHONE_NUMBER_ID = "e3894fb6-4ab9-4d49-a418-ea03a09b371a"


def vapi_headers():
    key = os.environ.get("VAPI_API_KEY")
    if not key:
        raise RuntimeError("VAPI_API_KEY not set")
    return {
        "Authorization": "Bearer {}".format(key),
        "Content-Type": "application/json",
    }


def get_calendar_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_KEY not set")

    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_upcoming_events(service, calendar_id):
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

    return resp.get("items", [])


def should_call(event):
    if event.get("status") == "cancelled":
        return False

    summary = event.get("summary", "").lower()

    # Only remind for Zumba and Aquacise
    if not any(cls in summary for cls in REMINDER_CLASSES):
        log.info("  Skipping (not a reminder class): {}".format(
            event.get("summary", "")))
        return False

    ext_props = event.get("extendedProperties", {})
    private = ext_props.get("private", {})
    if private.get(REMINDED_KEY):
        log.info("  Already reminded: {}".format(
            event.get("summary", "")))
        return False

    # Skip if Beth declined this reminder via Lucy
    if private.get("bethSkipReminder"):
        log.info("  Beth declined reminder: {}".format(
            event.get("summary", "")))
        return False

    start = event.get("start", {})
    if "dateTime" not in start:
        return False

    # Only call for classes at 10:30 AM or later
    from zoneinfo import ZoneInfo
    start_dt = datetime.fromisoformat(start["dateTime"]).astimezone(
        ZoneInfo("America/Los_Angeles"))
    if start_dt.hour < 10 or (start_dt.hour == 10 and start_dt.minute < 30):
        log.info("  Skipping (before 10:30 AM PT): {}".format(
            event.get("summary", "")))
        return False

    return True


def extract_class_info(event):
    summary = event.get("summary", "Unknown class")
    clean_name = summary
    for ch in ["\u2705", "\u23f3", "\U0001f3cb\ufe0f",
               "\U0001f3ca", "\U0001f3ac", "\U0001f3b5"]:
        clean_name = clean_name.replace(ch, "")
    for suffix in ["(waitlist)", "(drop-in)", "(club)"]:
        clean_name = clean_name.replace(suffix, "")
    clean_name = clean_name.strip()

    start_str = event.get("start", {}).get("dateTime", "")
    try:
        start_dt = datetime.fromisoformat(start_str)
        time_str = start_dt.strftime("%-I:%M %p")
    except (ValueError, AttributeError):
        time_str = "soon"

    return {"name": clean_name, "time": time_str}


def _place_call(number):
    """Place a single call and return the call ID, or None on failure."""
    resp = requests.post(
        "{}/call".format(VAPI_API),
        headers=vapi_headers(),
        json={
            "assistantId": ASSISTANT_ID,
            "phoneNumberId": PHONE_NUMBER_ID,
            "customer": {"number": number},
        },
    )
    if resp.status_code in (200, 201):
        call_id = resp.json().get("id", "")
        log.info("  Call placed! ID: {}".format(call_id))
        return call_id
    log.warning("  Call API failed: {}".format(resp.text[:200]))
    return None


def _wait_and_check(call_id, timeout=120):
    """Wait for a call to end and return True if Beth answered."""
    import time
    for _ in range(timeout // 5):
        time.sleep(5)
        resp = requests.get(
            "{}/call/{}".format(VAPI_API, call_id),
            headers=vapi_headers(),
        )
        if resp.status_code != 200:
            continue
        data = resp.json()
        status = data.get("status", "")
        if status == "ended":
            reason = data.get("endedReason", "")
            log.info("  Call ended. Reason: {}".format(reason))
            # These reasons mean Beth did NOT answer
            if reason in ("no-answer", "busy", "failed",
                          "machine-detected", "voicemail",
                          "silence-timed-out"):
                return False
            return True
    log.warning("  Call timed out waiting for result")
    return False


def make_reminder_call(event_info):
    """Use Lucy via Vapi to call Beth with a class reminder.

    Tries home phone then cell phone, repeating twice:
    home -> cell -> home -> cell, stopping as soon as Beth answers.
    """
    beth_home = os.environ.get("BETH_PHONE_NUMBER", "+19252781199")
    beth_cell = os.environ.get("BETH_CELL_NUMBER", "+14403211704")

    name = event_info["name"]
    time_str = event_info["time"]

    # Update Lucy's first message for the reminder
    first_msg = (
        "Hi Beth! It's Lucy. Just a quick reminder — you've got "
        "{} coming up at {}. You'll want to start getting ready soon!"
    ).format(name, time_str)

    requests.patch(
        "{}/assistant/{}".format(VAPI_API, ASSISTANT_ID),
        headers=vapi_headers(),
        json={"firstMessage": first_msg},
    )

    # Try home -> cell -> home -> cell
    numbers = [
        ("home", beth_home),
        ("cell", beth_cell),
        ("home", beth_home),
        ("cell", beth_cell),
    ]

    for label, number in numbers:
        log.info("Attempt: calling {} ({})".format(label, number))
        call_id = _place_call(number)
        if not call_id:
            continue

        if _wait_and_check(call_id):
            log.info("Beth answered on {}!".format(label))
            return True

        log.info("No answer on {}, trying next...".format(label))

    log.warning("All 4 call attempts failed — Beth did not answer")
    return False


def mark_as_reminded(service, calendar_id, event_id):
    try:
        service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={
                "extendedProperties": {
                    "private": {
                        REMINDED_KEY: datetime.now(
                            timezone.utc).isoformat(),
                    }
                }
            },
        ).execute()
        log.info("  Marked event as reminded: {}".format(event_id[:16]))
    except Exception as e:
        log.warning("  Failed to mark event: {}".format(e))


def run():
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        log.error("GOOGLE_CALENDAR_ID not set")
        sys.exit(1)

    service = get_calendar_service()

    log.info("Checking for Zumba/Aquacise/Functional Strength starting in {}-{} minutes...".format(
        REMINDER_WINDOW_MIN, REMINDER_WINDOW_MAX))

    events = get_upcoming_events(service, calendar_id)
    log.info("Found {} events in the reminder window".format(len(events)))

    calls_made = 0
    for event in events:
        summary = event.get("summary", "")
        log.info("Checking: {}".format(summary))

        if not should_call(event):
            continue

        info = extract_class_info(event)
        log.info("  Calling about: {} at {}".format(
            info["name"], info["time"]))

        success = make_reminder_call(info)
        if success:
            mark_as_reminded(service, calendar_id, event.get("id", ""))
            calls_made += 1

    log.info("Done — {} reminder call(s) placed".format(calls_made))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    run()
