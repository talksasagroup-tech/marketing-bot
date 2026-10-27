"""Runs LeadBot unattended. Leave `python main.py auto` running.

Weekdays from 9am Nairobi time: discover, verify, personalise, then send (up to the daily
limit), once a day. If the bot wasn't running at 9 (a laptop asleep, a restart) the day's run
happens as soon as it is, later that day. The rest of the time: check the inbox every POLL_MINUTES.
"""
import time
from datetime import datetime
from pathlib import Path

import config
import db
import inbox

NAIROBI = config.NAIROBI
RUN_HOUR = 9
# Remembers the last daily run so a restart doesn't repeat it
MARKER = Path(__file__).parent / ".auto_last_run"


def log(msg):
    print(f"{datetime.now(NAIROBI):%Y-%m-%d %H:%M} {msg}", flush=True)


def daily_due(now):
    if now.weekday() >= 5 or now.hour < RUN_HOUR:
        return False
    done = MARKER.read_text().strip() if MARKER.exists() else ""
    return done != now.date().isoformat()


def step(name, fn):
    try:
        log(f"{name}: {fn()}")
    except Exception as e:
        log(f"{name}: FAILED - {e}")


def _discover():
    import discover
    return f"{len(discover.run())} new leads"


def _verify():
    import verify
    verify.run()
    return f"{len(db.leads_by_status('verified'))} verified leads waiting"


def _personalise():
    import personalise
    return f"{personalise.run()} leads given a personal opening"


def _send():
    import sender
    sender.run()
    return f"{db.sent_today()} of {config.daily_limit()} sent today"


def _inbox():
    return f"{inbox.checked(inbox.check_once)} replies handled"


def daily_run(now):
    MARKER.write_text(now.date().isoformat())
    step("discover", _discover)
    step("verify", _verify)
    step("personalise", _personalise)
    step("send", _send)


def run():
    log(f"auto started: daily run on weekdays from {RUN_HOUR}:00 Nairobi time, "
        f"inbox every {config.POLL_MINUTES} min. Ctrl+C to stop.")
    while True:
        now = datetime.now(NAIROBI)
        if daily_due(now):
            daily_run(now)
        step("inbox", _inbox)
        time.sleep(config.POLL_MINUTES * 60)
