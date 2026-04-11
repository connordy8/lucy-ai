"""Call Beth 45 minutes before her fitness classes.

Uses Lucy (via Vapi) to make a friendly conversational reminder
call. Triggers for all of Beth's tracked classes (Zumba, Aquacise,
Functional Strength, UJAM, Posture Balance, Mat Yoga, ForeverFit,
Pickleball, Let's Stretch, Tai Chi).

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
REMINDER_CLASSES = [
    "zumba", "aquacise", "functional strength", "ujam",
    "posture", "mat yoga", "foreverfit", "forever fit",
    "pickleball", "let's stretch", "lets stretch", "tai chi",
]

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
    start_dt = None
    try:
        start_dt = datetime.fromisoformat(start_str)
        time_str = start_dt.strftime("%-I:%M %p")
    except (ValueError, AttributeError):
        time_str = "soon"

    return {"name": clean_name, "time": time_str, "start_dt": start_dt}


def _place_call(number, overrides=None):
    """Place a single call and return the call ID, or None on failure.

    Uses assistantOverrides to set prompt per-call — never mutates the
    shared assistant, preventing race conditions with evening calls.
    """
    payload = {
        "assistantId": ASSISTANT_ID,
        "phoneNumberId": PHONE_NUMBER_ID,
        "customer": {"number": number},
    }
    if overrides:
        payload["assistantOverrides"] = overrides

    resp = requests.post(
        "{}/call".format(VAPI_API),
        headers=vapi_headers(),
        json=payload,
    )
    if resp.status_code in (200, 201):
        call_id = resp.json().get("id", "")
        log.info("  Call placed! ID: {}".format(call_id))
        return call_id
    log.warning("  Call API failed: {}".format(resp.text[:200]))
    return None


# Phrases that strongly indicate voicemail/answering machine
VOICEMAIL_PHRASES = [
    "leave a message",
    "at the tone",
    "press 1",
    "not available",
    "after the beep",
    "record your message",
    "voice mail",
    "voicemail",
    "is unavailable",
    "please record",
    "if you are satisfied with your message",
    "please try again later",
]


def _looks_like_voicemail(call_data):
    """Heuristic check: did this call hit a voicemail?"""
    msgs = call_data.get("artifact", {}).get("messages", [])
    user_text = " ".join(
        m.get("message", m.get("content", "")).lower()
        for m in msgs
        if m.get("role") == "user"
    )
    for phrase in VOICEMAIL_PHRASES:
        if phrase in user_text:
            return True
    return False


def _wait_and_check(call_id, timeout=300):
    """Wait for a call to end and return True if Beth (a human) answered.

    Aggressive detection: checks endedReason, call duration, AND
    looks for voicemail phrases in the transcript.
    """
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
        if data.get("status", "") != "ended":
            continue

        reason = data.get("endedReason", "")
        log.info("  Call ended. Reason: {}".format(reason))

        # Hard no-answer reasons
        if reason in ("no-answer", "busy", "failed",
                      "machine-detected", "voicemail",
                      "silence-timed-out", "twilio-failed-to-connect-call"):
            return False

        # Transcript heuristic — catches voicemails Twilio missed
        if _looks_like_voicemail(data):
            log.info("  Transcript looks like voicemail — treating as no answer")
            return False

        # Duration check — if call was very short, likely not answered
        started = data.get("startedAt", "")
        ended = data.get("endedAt", "")
        if started and ended:
            try:
                s = datetime.fromisoformat(started.replace("Z", "+00:00"))
                e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
                duration = (e - s).total_seconds()
                if duration < 15:
                    log.info("  Call too short ({}s) — treating as no answer".format(int(duration)))
                    return False
            except (ValueError, TypeError):
                pass

        return True
    log.warning("  Call timed out waiting for result")
    return False


def _build_class_reminder_prompt(class_name, class_time):
    """Build a proper system prompt for class reminder calls.

    Loads the full system_prompt.txt template so Lucy has her full
    personality, then adds class-specific context.
    """
    from pathlib import Path
    from zoneinfo import ZoneInfo

    PACIFIC = ZoneInfo("America/Los_Angeles")
    template_path = Path(__file__).parent / "lucy" / "system_prompt.txt"
    template = template_path.read_text()

    current_time = datetime.now(PACIFIC).strftime(
        "%A, %B %-d, %Y at %-I:%M %p PT")

    prompt = template.replace("{current_time}", current_time)
    prompt = prompt.replace("{calendar_context}",
                            "Beth has {} at {} today.".format(class_name, class_time))
    prompt = prompt.replace("{memory_context}", "(class reminder call)")

    # Add explicit class-reminder instructions so Lucy stays on topic
    prompt += (
        "\n\n## THIS CALL — CLASS REMINDER\n"
        "You are calling to remind Beth about {} at {}.\n"
        "1. Keep it brief — remind her what class is coming up and when.\n"
        "2. If she acknowledges, say goodbye and end the call.\n"
        "3. Do NOT ask about bedtime, CPAP, or sleeping. "
        "This is a daytime class reminder.\n"
        "4. Do NOT try to extend the conversation. Just deliver the "
        "reminder warmly and wrap up.\n"
    ).format(class_name, class_time)

    return prompt


def make_reminder_call(event_info):
    """Use Lucy via Vapi to call Beth with a class reminder.

    Tries home phone then cell phone, repeating twice:
    home -> cell -> home -> cell, stopping as soon as Beth answers.
    """
    beth_home = os.environ.get("BETH_PHONE_NUMBER")
    beth_cell = os.environ.get("BETH_CELL_NUMBER")
    if not beth_home or not beth_cell:
        log.error("BETH_PHONE_NUMBER and BETH_CELL_NUMBER env vars required")
        return False

    name = event_info["name"]
    time_str = event_info["time"]

    # Calculate minutes until class
    now = datetime.now(timezone.utc)
    start_dt = event_info.get("start_dt")
    if start_dt:
        mins = int((start_dt - now).total_seconds() / 60)
    else:
        mins = 45

    # Update Lucy's first message and voicemail message
    first_msg = (
        "Hi Beth! It's Lucy. Just a quick reminder — you've got "
        "{} coming up at {}. You'll want to start getting ready soon!"
    ).format(name, time_str)

    voicemail_msg = (
        "Hi Beth, it's your assistant Lucy. "
        "This is a reminder that you have {} in {} minutes. "
        "Have a good day!"
    ).format(name, mins)

    # Build per-call overrides so we never mutate the shared assistant
    prompt = _build_class_reminder_prompt(name, time_str)

    overrides = {
        "model": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": prompt}],
        },
        "firstMessage": first_msg,
        "voicemailMessage": voicemail_msg,
    }

    # Try home -> cell -> home -> cell
    numbers = [
        ("home", beth_home),
        ("cell", beth_cell),
        ("home", beth_home),
        ("cell", beth_cell),
    ]

    for label, number in numbers:
        log.info("Attempt: calling {} ({})".format(label, number))
        call_id = _place_call(number, overrides=overrides)
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

    log.info("Checking for fitness classes starting in {}-{} minutes...".format(
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
