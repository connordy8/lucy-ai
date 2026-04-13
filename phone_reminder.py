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
# Note: get_upcoming_events() queries up to 90 min ahead to batch
# overlapping classes. The actual trigger window (40-50 min) is
# enforced by reminder_check.py which gates workflow dispatch.

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
PHONE_NUMBER_ID = "4563a603-3710-4e71-8a55-72b28b1d5413"


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


def _query_events(service, calendar_id, time_min, time_max, max_results=10):
    """Low-level calendar query helper."""
    return service.events().list(
        calendarId=calendar_id,
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute().get("items", [])


def get_upcoming_events(service, calendar_id):
    """Get events that need reminders: normal window + follow-ups.

    Two queries:
    1. Normal: classes starting in 40-90 min (for batching overlapping classes)
    2. Follow-ups: ALL events today that have followUpRemindAt set and due now
       (these may start in < 40 min, so the normal window would miss them)
    """
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
    now = datetime.now(timezone.utc)
    now_pt = now.astimezone(PACIFIC)

    # --- Query 1: Normal window (40-90 min out) ---
    window_start = now + timedelta(minutes=REMINDER_WINDOW_MIN)
    window_end = now + timedelta(minutes=90)

    events = _query_events(service, calendar_id, window_start, window_end)
    seen_ids = {e.get("id") for e in events}

    # --- Query 2: Follow-up reminders due now (all events today) ---
    today_start = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now_pt.replace(hour=23, minute=59, second=59, microsecond=0)

    all_today = _query_events(
        service, calendar_id, today_start, today_end, max_results=30)

    for ev in all_today:
        if ev.get("id") in seen_ids:
            continue
        private = ev.get("extendedProperties", {}).get("private", {})
        follow_up = private.get("followUpRemindAt", "")
        if not follow_up:
            continue
        try:
            follow_up_dt = datetime.fromisoformat(follow_up).astimezone(PACIFIC)
            if now_pt <= follow_up_dt <= now_pt + timedelta(minutes=6):
                log.info("  Adding follow-up event: {}".format(
                    ev.get("summary", "")))
                events.append(ev)
                seen_ids.add(ev.get("id"))
        except ValueError:
            pass

    return events


def should_call(event):
    """Check if this event needs a reminder call.

    Two paths:
    1. Follow-up: Beth requested a callback (followUpRemindAt is due now)
    2. Normal: first reminder, class in 40-90 min, not yet reminded
    """
    if event.get("status") == "cancelled":
        return False

    summary = event.get("summary", "").lower()

    if not any(cls in summary for cls in REMINDER_CLASSES):
        log.info("  Skipping (not a reminder class): {}".format(
            event.get("summary", "")))
        return False

    ext_props = event.get("extendedProperties", {})
    private = ext_props.get("private", {})

    # Skip if Beth declined this reminder via Lucy
    if private.get("bethSkipReminder"):
        log.info("  Beth declined reminder: {}".format(
            event.get("summary", "")))
        return False

    start = event.get("start", {})
    if "dateTime" not in start:
        return False

    # Check for follow-up reminder due now
    from zoneinfo import ZoneInfo
    now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    follow_up = private.get("followUpRemindAt", "")
    if follow_up:
        try:
            follow_up_dt = datetime.fromisoformat(follow_up).astimezone(
                ZoneInfo("America/Los_Angeles"))
            if now_pt <= follow_up_dt <= now_pt + timedelta(minutes=6):
                log.info("  Follow-up reminder due NOW for: {}".format(
                    event.get("summary", "")))
                return True
        except ValueError:
            pass

    # Normal path: skip if already reminded
    if private.get(REMINDED_KEY):
        log.info("  Already reminded: {}".format(
            event.get("summary", "")))
        return False

    # Only call for classes at 10:30 AM or later
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

    location = event.get("location", "")
    # Simplify long addresses to just the venue name
    if location and "," in location:
        location = location.split(",")[0]

    return {"name": clean_name, "time": time_str, "start_dt": start_dt,
            "location": location}


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
        timeout=30,
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
    "subscriber you have dialed",
    "number is not in service",
    "mailbox is full",
    "cannot be completed as dialed",
    "the person you are calling",
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
        try:
            resp = requests.get(
                "{}/call/{}".format(VAPI_API, call_id),
                headers=vapi_headers(),
                timeout=15,
            )
        except requests.exceptions.Timeout:
            continue
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


def _build_class_reminder_prompt(classes):
    """Build a proper system prompt for class reminder calls.

    Args:
        classes: list of dicts with 'name' and 'time' keys.
                 Can be a single class or multiple overlapping classes.
    """
    from pathlib import Path
    from zoneinfo import ZoneInfo

    PACIFIC = ZoneInfo("America/Los_Angeles")
    template_path = Path(__file__).parent / "lucy" / "system_prompt.txt"
    template = template_path.read_text()

    current_time = datetime.now(PACIFIC).strftime(
        "%A, %B %-d, %Y at %-I:%M %p PT")

    prompt = template.replace("{current_time}", current_time)

    # Build class details with locations
    def _class_detail(c):
        s = "{} at {}".format(c["name"], c["time"])
        if c.get("location"):
            s += " at {}".format(c["location"])
        return s

    if len(classes) == 1:
        detail = _class_detail(classes[0])
        cal_ctx = "Beth has {} today.".format(detail)
        class_list = detail
        conversation_flow = (
            "STEP 1: Remind Beth about {} and ask if she's planning to go.\n"
            "STEP 2: Wait for her response. Then ask: "
            "\"Would you like me to call you again closer to class time "
            "as a reminder to get ready?\"\n"
            "STEP 3: If she says yes, ask WHEN she'd like the call "
            "(e.g., \"How about 15 minutes before?\"). "
            "If no, wrap up warmly.\n"
            "STEP 4: Once she gives a time, use the "
            "scheduleFollowUpReminder tool, confirm it's set, "
            "and say goodbye.\n"
        ).format(class_list)
    else:
        details = [_class_detail(c) for c in classes]
        cal_ctx = "Beth has these classes today: " + ", ".join(details)
        class_list = ", ".join(details[:-1]) + " and " + details[-1]
        conversation_flow = (
            "STEP 1: Tell Beth she has {} classes coming up: {}. "
            "Ask which ones she's planning to go to.\n"
            "STEP 2: Wait for her response. For EACH class she wants "
            "to attend, ask (one at a time): \"Would you like me to "
            "call you again before [class name] as a reminder to get "
            "ready?\"\n"
            "STEP 3: If she says yes for a class, ask WHEN she'd like "
            "the call (e.g., \"How about 15 minutes before?\"). Use "
            "the scheduleFollowUpReminder tool for that class.\n"
            "STEP 4: Repeat steps 2-3 for each class she mentioned. "
            "Once done with all classes, wrap up warmly and say "
            "goodbye.\n"
        ).format(len(classes), ", ".join(details))

    prompt = prompt.replace("{calendar_context}", cal_ctx)
    prompt = prompt.replace("{memory_context}", "(class reminder call)")

    prompt += (
        "\n\n## THIS CALL — CLASS REMINDER\n"
        "You are calling to remind Beth about {}.\n\n"
        "IMPORTANT: Only ask ONE question at a time. Wait for Beth to "
        "respond before moving to the next step. Never combine multiple "
        "questions in a single message.\n\n"
        "If Beth asks where a class is, tell her the location. You have "
        "this information — never say you don't know.\n\n"
        "Follow this conversation flow:\n"
        "{}"
        "\nDo NOT ask about bedtime, CPAP, or sleeping. "
        "This is a daytime class reminder.\n"
    ).format(class_list, conversation_flow)

    return prompt


def make_reminder_call(all_classes):
    """Use Lucy via Vapi to call Beth with a class reminder.

    Args:
        all_classes: list of dicts with 'name', 'time', 'start_dt' keys.
                     Can be one class or multiple overlapping classes.

    Tries home phone then cell phone, repeating twice:
    home -> cell -> home -> cell, stopping as soon as Beth answers.
    """
    beth_home = os.environ.get("BETH_PHONE_NUMBER")
    beth_cell = os.environ.get("BETH_CELL_NUMBER")
    if not beth_home or not beth_cell:
        log.error("BETH_PHONE_NUMBER and BETH_CELL_NUMBER env vars required")
        return False

    # Calculate minutes until first class
    now = datetime.now(timezone.utc)
    first = all_classes[0]
    if first.get("start_dt"):
        mins = int((first["start_dt"] - now).total_seconds() / 60)
    else:
        mins = 45

    if len(all_classes) == 1:
        first_msg = (
            "Hi Beth! It's Lucy. Just a quick reminder — you've got "
            "{} coming up at {}. Are you planning to go?"
        ).format(first["name"], first["time"])
        voicemail_msg = (
            "Hi Beth, it's your assistant Lucy. "
            "This is a reminder that you have {} in {} minutes. "
            "Have a good day!"
        ).format(first["name"], mins)
    else:
        names = " and ".join(c["name"] for c in all_classes)
        times = ", ".join("{} at {}".format(c["name"], c["time"])
                          for c in all_classes)
        first_msg = (
            "Hi Beth! It's Lucy. Just a quick reminder — you've got "
            "a few classes coming up today: {}. "
            "Which ones are you planning to go to?"
        ).format(times)
        voicemail_msg = (
            "Hi Beth, it's your assistant Lucy. "
            "This is a reminder that you have {} coming up. "
            "Have a good day!"
        ).format(names)

    # Build per-call overrides so we never mutate the shared assistant
    prompt = _build_class_reminder_prompt(all_classes)

    tool_server = os.environ.get(
        "LUCY_API_BASE", "https://lucy-ai-eight.vercel.app")

    overrides = {
        "model": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": prompt}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "scheduleFollowUpReminder",
                        "description": (
                            "Schedule a follow-up reminder call for Beth. "
                            "Use when Beth asks to be reminded again before "
                            "a class."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "class_name": {
                                    "type": "string",
                                    "description": (
                                        "Name of the class, e.g. 'Zumba'"
                                    ),
                                },
                                "minutes_before": {
                                    "type": "integer",
                                    "description": (
                                        "Minutes before class to call back, "
                                        "e.g. 15"
                                    ),
                                },
                                "remind_at": {
                                    "type": "string",
                                    "description": (
                                        "Specific time to call back, e.g. "
                                        "'9:45' or '10:15'. Use this OR "
                                        "minutes_before, not both."
                                    ),
                                },
                            },
                            "required": ["class_name"],
                        },
                    },
                    "server": {
                        "url": "{}/api/vapi_tools".format(tool_server)
                    },
                },
            ],
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
    _notify_failure(all_classes)
    return False


def _notify_failure(all_classes):
    """Send SMS to Connor when all call attempts to Beth fail."""
    try:
        twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        twilio_from = os.environ.get("TWILIO_PHONE_NUMBER", "").strip()
        connor_phone = os.environ.get("CONNOR_PHONE_NUMBER", "").strip()

        if not all([twilio_sid, twilio_token, twilio_from, connor_phone]):
            log.warning("  Twilio/Connor phone not configured — "
                        "cannot send failure alert")
            return

        classes_str = ", ".join(c["name"] for c in all_classes)
        msg = (
            "⚠️ Lucy couldn't reach Beth for her class reminder "
            "({classes}). All 4 call attempts failed (home & cell, "
            "twice each)."
        ).format(classes=classes_str)

        requests.post(
            "https://api.twilio.com/2010-04-01/Accounts/{}/Messages.json"
            .format(twilio_sid),
            auth=(twilio_sid, twilio_token),
            data={"From": twilio_from, "To": connor_phone, "Body": msg},
            timeout=15,
        )
        log.info("  Failure alert sent to Connor")
    except Exception as e:
        log.warning("  Failed to send failure alert: {}".format(e))


def mark_as_reminded(service, calendar_id, event_id):
    """Mark event as reminded, but preserve any follow-up Beth scheduled.

    During the call, Beth may have asked Lucy to call back at a specific
    time. The scheduleFollowUpReminder tool writes followUpRemindAt to the
    event. We must NOT overwrite that here — only set bethReminded.
    """
    try:
        # Read current extended properties first
        ev = service.events().get(
            calendarId=calendar_id, eventId=event_id
        ).execute()
        private = ev.get("extendedProperties", {}).get("private", {})

        # Merge into existing private properties (Google Calendar patch
        # replaces the entire private dict, so we must include all keys)
        private[REMINDED_KEY] = datetime.now(timezone.utc).isoformat()
        # Only clear followUpRemindAt if there ISN'T one set
        if not private.get("followUpRemindAt"):
            private["followUpRemindAt"] = ""

        service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={"extendedProperties": {"private": dict(private)}},
        ).execute()
        log.info("  Marked event as reminded: {}".format(event_id[:16]))
        if private.get("followUpRemindAt"):
            log.info("  Preserved follow-up at: {}".format(
                private["followUpRemindAt"]))
    except Exception as e:
        log.warning("  Failed to mark event: {}".format(e))


def _chain_nearby_classes(service, calendar_id, eligible, eligible_events,
                          seen_ids, gap_minutes=90):
    """Chain forward: find more classes within gap_minutes of the latest.

    If Beth has Mat Yoga at 12:30 and UJAM at 1:30 (60 min gap), we
    bundle them into one call instead of calling twice. Keeps chaining
    transitively — A→B→C all get bundled if each pair is ≤ gap_minutes.
    """
    while True:
        # Find the latest start time among eligible events
        latest_dt = None
        for info in eligible:
            if info.get("start_dt") and (
                    latest_dt is None or info["start_dt"] > latest_dt):
                latest_dt = info["start_dt"]
        if not latest_dt:
            break

        # Look for events starting between latest and latest + gap
        chain_end = latest_dt + timedelta(minutes=gap_minutes)
        more = _query_events(service, calendar_id, latest_dt, chain_end)

        new_found = False
        for event in more:
            eid = event.get("id", "")
            if eid in seen_ids:
                continue
            if not should_call(event):
                seen_ids.add(eid)
                continue
            info = extract_class_info(event)
            log.info("  Chaining nearby class: {} at {}".format(
                info["name"], info["time"]))
            eligible.append(info)
            eligible_events.append(event)
            seen_ids.add(eid)
            new_found = True

        if not new_found:
            break


def run():
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        log.error("GOOGLE_CALENDAR_ID not set")
        sys.exit(1)

    service = get_calendar_service()

    log.info("Checking for fitness classes starting in {}-90 minutes...".format(
        REMINDER_WINDOW_MIN))

    events = get_upcoming_events(service, calendar_id)
    log.info("Found {} events in the reminder window".format(len(events)))

    # Collect all eligible classes into one batch
    eligible = []
    eligible_events = []
    seen_ids = {e.get("id") for e in events}
    for event in events:
        summary = event.get("summary", "")
        log.info("Checking: {}".format(summary))

        if not should_call(event):
            continue

        info = extract_class_info(event)
        log.info("  Eligible: {} at {}".format(info["name"], info["time"]))
        eligible.append(info)
        eligible_events.append(event)

    # Chain forward — bundle classes within 90 min of each other
    # into one call so Beth doesn't get multiple calls in a row
    if eligible:
        _chain_nearby_classes(
            service, calendar_id, eligible, eligible_events, seen_ids)

    if not eligible:
        log.info("No classes to remind about")
        return

    # Sort by start time so the call mentions them in order
    paired = sorted(
        zip(eligible, eligible_events),
        key=lambda p: p[0].get("start_dt") or datetime.max.replace(
            tzinfo=timezone.utc))
    eligible = [p[0] for p in paired]
    eligible_events = [p[1] for p in paired]

    log.info("Calling about {} class(es): {}".format(
        len(eligible),
        ", ".join("{} at {}".format(c["name"], c["time"]) for c in eligible)))

    # Make ONE call mentioning all classes
    success = make_reminder_call(eligible)
    if success:
        for event in eligible_events:
            mark_as_reminded(service, calendar_id, event.get("id", ""))

    log.info("Done — {} class(es) reminded".format(
        len(eligible) if success else 0))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    run()
