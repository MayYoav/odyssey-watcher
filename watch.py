#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch.py - מנטר הקרנות IMAX של "האודיסאה" בפלאנט ראשון לציון בסופ"ש.

מיועד לריצה ב-GitHub Actions (ראה .github/workflows/watch.yml).
המצב נשמר ב-state.json שנדחף חזרה לריפו אחרי כל ריצה.

הרצות ידניות:
    python3 watch.py --discover     # מה יש כרגע + אימות שה-API עובד
    python3 watch.py --test-alert   # בדיקת ערוצי ההתראה
    python3 watch.py --reset        # איפוס המצב השמור
"""

import argparse
import datetime as dt
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

# ============================================================
# CONFIG
# ============================================================

SITE = "https://www.planetcinema.co.il"
GROUP_ID = "10100"
LANG = "iw_IL"

CINEMA_NAME_CONTAINS = "ראשון"
FILM_NAME_CONTAINS = ["אודיס", "odyssey"]
IMAX_MARKERS = ["imax"]

# weekday(): שני=0, שלישי=1, רביעי=2, חמישי=3, שישי=4, שבת=5, ראשון=6
WANTED_WEEKDAYS = {4, 5}      # שישי + שבת. להוסיף חמישי בערב: {3, 4, 5}
DAYS_AHEAD = 120

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
REQUEST_DELAY = 1.0
TIMEOUT = 25

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
)

# --- ערוצי התראה (מוגדרים כ-GitHub Secrets) ---
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

API_PREFIXES = [
    "/il/data-api-service/v1/quickbook/{gid}",
    "/data-api-service/v1/quickbook/{gid}",
    "/he/data-api-service/v1/quickbook/{gid}",
]

HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


# ============================================================
# תשתית
# ============================================================

def log(msg):
    print(f"[{dt.datetime.utcnow():%H:%M:%S}Z] {msg}", flush=True)


def http_json(url, data=None, headers=None):
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        "Referer": SITE + "/",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


_api_base = None


def api_base():
    global _api_base
    if _api_base:
        return _api_base
    until = (dt.date.today() + dt.timedelta(days=DAYS_AHEAD)).isoformat()
    errors = []
    for prefix in API_PREFIXES:
        base = SITE + prefix.format(gid=GROUP_ID)
        try:
            data = http_json(f"{base}/cinemas/with-event/until/{until}?attr=&lang={LANG}")
            if isinstance(data, dict) and "body" in data:
                _api_base = base
                log(f"API base: {base}")
                return base
            errors.append(f"{prefix}: JSON ללא body")
        except Exception as e:
            errors.append(f"{prefix}: {e}")
        time.sleep(REQUEST_DELAY)
    raise SystemExit(
        "לא זוהה מסלול API תקין:\n  " + "\n  ".join(errors) +
        "\n\nתיקון: כרום -> F12 -> Network -> סנני 'quickbook' באתר פלאנט,\n"
        "והעתיקי את המסלול האמיתי ל-API_PREFIXES."
    )


# ============================================================
# שליפה
# ============================================================

def find_cinema_id():
    if os.environ.get("CINEMA_ID"):
        return os.environ["CINEMA_ID"]
    until = (dt.date.today() + dt.timedelta(days=DAYS_AHEAD)).isoformat()
    data = http_json(f"{api_base()}/cinemas/with-event/until/{until}?attr=&lang={LANG}")
    cinemas = data.get("body", {}).get("cinemas", [])
    for c in cinemas:
        if CINEMA_NAME_CONTAINS in (c.get("displayName") or c.get("name") or ""):
            log(f"סניף: {c.get('displayName')} (id={c.get('id')})")
            return str(c["id"])
    log("סניפים זמינים: " + ", ".join(
        f"{c.get('displayName')}={c.get('id')}" for c in cinemas))
    raise SystemExit(f"לא נמצא סניף שמכיל '{CINEMA_NAME_CONTAINS}'")


def dates_with_events(cinema_id):
    until = (dt.date.today() + dt.timedelta(days=DAYS_AHEAD)).isoformat()
    data = http_json(
        f"{api_base()}/dates/in-cinema/{cinema_id}/until/{until}?attr=&lang={LANG}")
    return data.get("body", {}).get("dates", [])


def events_at_date(cinema_id, date_str):
    data = http_json(
        f"{api_base()}/film-events/in-cinema/{cinema_id}/at-date/{date_str}?lang={LANG}")
    body = data.get("body", {})
    films = {str(f["id"]): f for f in body.get("films", [])}
    return films, body.get("events", [])


# ============================================================
# סינון ותיאור
# ============================================================

def is_wanted_film(film):
    name = (film.get("name") or "").lower()
    return any(k.lower() in name for k in FILM_NAME_CONTAINS)


def is_imax(event):
    blob = " ".join([
        " ".join(str(a) for a in (event.get("attributeIds") or [])),
        str(event.get("auditorium") or ""),
        str(event.get("auditoriumTinyName") or ""),
    ]).lower()
    return any(m in blob for m in IMAX_MARKERS)


def booking_url(event):
    link = event.get("bookingLink") or event.get("bookingRouterLaunchLink")
    if link:
        return link if link.startswith("http") else SITE + link
    return f"{SITE}/il/booking-router/launch/{event.get('id')}?lang={LANG}"


def describe(event, film):
    when = event.get("eventDateTime", "")
    try:
        d = dt.datetime.fromisoformat(when)
        when = f"יום {HEB_DAYS[d.weekday()]} {d:%d/%m} בשעה {d:%H:%M}"
    except Exception:
        pass
    ratio = event.get("availabilityRatio")
    seats = ""
    if ratio not in (None, ""):
        try:
            seats = f" | פנוי ~{round(float(ratio) * 100)}%"
        except Exception:
            pass
    aud = event.get("auditorium") or ""
    return f"{when} | {aud}{seats}\n{booking_url(event)}"


# ============================================================
# התראות
# ============================================================

def send_ntfy(title, text, click=None):
    if not NTFY_TOPIC:
        return False
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": text,
        "priority": 5,
        "tags": ["clapper"],
    }
    if click:
        payload["click"] = click
    http_json("https://ntfy.sh/",
              data=json.dumps(payload).encode("utf-8"),
              headers={"Content-Type": "application/json"})
    return True


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        return False
    body = json.dumps({"chat_id": TG_CHAT_ID, "text": text}).encode("utf-8")
    http_json(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
              data=body, headers={"Content-Type": "application/json"})
    return True


def send_email(subject, text):
    if not (SMTP_USER and SMTP_PASS and MAIL_TO):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    msg.set_content(text)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True


def alert(title, text, click=None):
    log(f"ALERT >>> {title}\n{text}")
    sent, failed = [], []
    for name, fn in (
        ("ntfy", lambda: send_ntfy(title, text, click)),
        ("telegram", lambda: send_telegram(f"{title}\n\n{text}")),
        ("email", lambda: send_email(title, text)),
    ):
        try:
            if fn():
                sent.append(name)
        except Exception as e:
            failed.append(f"{name} ({e})")
    log("נשלח: " + (", ".join(sent) or "כלום"))
    if failed:
        log("נכשל: " + ", ".join(failed))
    if not sent:
        log("אזהרה: אף ערוץ התראה לא מוגדר או הצליח.")


# ============================================================
# מצב
# ============================================================

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ============================================================
# ריצה
# ============================================================

def run(discover=False):
    state = load_state()
    seen = state.setdefault("seen", {})
    # דגל נפרד ולא "seen ריק" - אחרת כל עוד אין הקרנות בכלל,
    # כל ריצה תיראה כמו ריצה ראשונה ולעולם לא תתריע.
    initialized = state.get("initialized", False)

    cinema_id = find_cinema_id()
    time.sleep(REQUEST_DELAY)

    all_dates = dates_with_events(cinema_id)
    targets = []
    for ds in all_dates:
        try:
            d = dt.date.fromisoformat(str(ds)[:10])
        except Exception:
            continue
        if d.weekday() in WANTED_WEEKDAYS:
            targets.append(str(ds)[:10])

    log(f"{len(all_dates)} תאריכים עם הקרנות בסניף, {len(targets)} מהם סופ\"ש")

    new_events, returned_events, current = [], [], []
    for ds in targets:
        time.sleep(REQUEST_DELAY)
        try:
            films, events = events_at_date(cinema_id, ds)
        except Exception as e:
            log(f"שגיאה בתאריך {ds}: {e}")
            continue
        for ev in events:
            film = films.get(str(ev.get("filmId")), {})
            if not is_wanted_film(film) or not is_imax(ev):
                continue
            eid = str(ev.get("id"))
            sold_out = bool(ev.get("soldOut"))
            desc = describe(ev, film)
            current.append(desc)
            prev = seen.get(eid)
            if prev is None:
                new_events.append(desc)
            elif prev.get("soldOut") and not sold_out:
                returned_events.append(desc)
            seen[eid] = {"soldOut": sold_out, "date": ds}

    log(f"הקרנות IMAX תואמות כרגע: {len(current)}")
    if discover:
        for c in current:
            print("   - " + c.replace("\n", "\n     "))

    if not initialized:
        log("ריצה ראשונה - שומרת מצב בסיס בלי להתריע.")
        state["initialized"] = True
    else:
        if new_events:
            alert("🎬 נפתחו הקרנות IMAX לאודיסאה!",
                  "פלאנט ראשון לציון, סופ\"ש:\n\n" + "\n\n".join(new_events),
                  click=SITE + "/whatson")
        if returned_events:
            alert("🔁 השתחררו כרטיסים ל-IMAX",
                  "הקרנה שאזלה חזרה להיות זמינה:\n\n" + "\n\n".join(returned_events),
                  click=SITE + "/whatson")
        if not new_events and not returned_events:
            log("אין שינוי.")

    today = dt.date.today().isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v.get("date", "9999") >= today}
    state["lastRun"] = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    state["lastCount"] = len(current)
    save_state(state)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--discover", action="store_true")
    p.add_argument("--test-alert", action="store_true")
    p.add_argument("--reset", action="store_true")
    a = p.parse_args()

    if a.test_alert:
        alert("בדיקה ✅", "אם קיבלת את זה בטלפון - הצינור עובד.")
        return
    if a.reset:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        log("אופס.")
        return
    try:
        run(discover=a.discover)
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code} {e.reason} - ייתכן חסימת Cloudflare מכתובות GitHub.")
        sys.exit(1)


if __name__ == "__main__":
    main()
