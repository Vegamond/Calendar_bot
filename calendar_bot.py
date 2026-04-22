#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import argparse
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from zoneinfo import ZoneInfo

import requests

KYIV_TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"
CONFIGS_FILE = "configs.json"

# ----------------------------
# URL regex — strict (no Cyrillic/spaces)
# ----------------------------
URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE
)

# ----------------------------
# Pair number by start time
# ----------------------------
PAIR_BY_START = {
    "09:00": 1,
    "10:40": 2,
    "12:30": 3,
    "14:10": 4,
    "15:40": 5,
}


def pair_no(t: dt.datetime) -> Optional[int]:
    s = t.astimezone(KYIV_TZ).strftime("%H:%M")
    return PAIR_BY_START.get(s)


# ----------------------------
# Moodle links — всі групи в одному місці
# Щоб додати нове посилання — просто додай рядок сюди
# ----------------------------
MOODLE_LINKS: Dict[str, str] = {
    # 1 курс (ГРС-15Д, ГРС-15/25Дмб)
    "Туристичне країнознавство":            "https://distance.kuk.edu.ua/course/view.php?id=8560",
    "Готельна справа":                      "https://distance.kuk.edu.ua/course/view.php?id=8064",
    "Історія України":                      "https://distance.kuk.edu.ua/mod/attendance/view.php?id=175144",
    "Ділова українська мова":               "https://distance.kuk.edu.ua/mod/attendance/view.php?id=113738",
    "Культурні та креативні індустрії":     "https://distance.kuk.edu.ua/course/view.php?id=2067",
    "Барна справа":                         "https://distance.kuk.edu.ua/course/view.php?id=5472",
    "Ресторанна справа":                    "https://distance.kuk.edu.ua/course/view.php?id=8561",
    "Емоційний інтелект":                   "https://distance.kuk.edu.ua/course/view.php?id=8549",

    # 2 курс (ГРС-14Дмб)
    "Безпека життєдіяльності та охорона праці":                         "https://distance.kuk.edu.ua/mod/attendance/view.php?id=179093",
    "Основи правознавства":                                             "https://distance.kuk.edu.ua/mod/attendance/view.php?id=179091",
    "Устаткування готельно-ресторанних комплексів":                     "https://distance.kuk.edu.ua/mod/attendance/view.php?id=178888",
    "Маркетинг, реклама та PR готельно-ресторанного і туристичного бізнесу": "https://distance.kuk.edu.ua/course/view.php?id=8577",
    "Енологія і еногастрономія":                                        "https://distance.kuk.edu.ua/course/view.php?id=8576",
    "Ресторанне обслуговування: організація і технології":               "https://distance.kuk.edu.ua/mod/attendance/view.php?id=147491",
    "Готельне обслуговування: організація і технології":                 "https://distance.kuk.edu.ua/course/view.php?id=7724",
    "Економіка підприємства":                                           "https://distance.kuk.edu.ua/course/view.php?id=5778",
    "Психологія":                                                       "https://distance.kuk.edu.ua/mod/attendance/view.php?id=179013",
    "Документаційне забезпечення управління":                           "https://distance.kuk.edu.ua/mod/attendance/view.php?id=179019",

    # Порожні — додай посилання коли будуть
    # "Товарознавство": "",
}


def normalize_discipline(name: str) -> str:
    return " ".join((name or "").replace("'", "'").split()).casefold()


MOODLE_LINKS_NORM = {
    normalize_discipline(k): v
    for k, v in MOODLE_LINKS.items()
    if v
}


# ----------------------------
# iCal unescape (RFC5545)
# ----------------------------
def ics_unescape(s: str) -> str:
    if not s:
        return ""
    return (s
            .replace(r"\n", "\n")
            .replace(r"\N", "\n")
            .replace(r"\,", ",")
            .replace(r"\;", ";")
            .replace(r"\\", "\\"))


# ----------------------------
# Models
# ----------------------------
@dataclass
class Event:
    start: dt.datetime
    end: dt.datetime
    summary: str
    description: str
    location: str


# ----------------------------
# Group config
# ----------------------------
@dataclass
class GroupConfig:
    group_name: str
    bot_token: str
    chat_id: str
    ics_url: str
    schedule_thread_id: Optional[int] = None
    main_thread_id: Optional[int] = None


def _resolve_env(val: str, group_name: str) -> str:
    """Якщо значення починається з '$' — читає з env змінної."""
    if val and val.startswith("$"):
        env_name = val[1:]
        resolved = os.getenv(env_name, "").strip()
        if not resolved:
            raise RuntimeError(f"Missing env var: {env_name} (group: {group_name})")
        return resolved
    return val


def load_configs() -> List[GroupConfig]:
    """
    Читає configs.json. Секрети вказуються як "$ENV_VAR_NAME".

    Приклад configs.json:
    [
      {
        "group_name": "ГРС-15Д",
        "bot_token": "$BOT_TOKEN_GRS15D",
        "chat_id": "$CHAT_ID_GRS15D",
        "ics_url": "$ICS_URL_GRS15D",
        "schedule_thread_id": 123,
        "main_thread_id": null
      }
    ]
    """
    with open(CONFIGS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    configs = []
    for item in raw:
        g = item.get("group_name", "?")
        configs.append(GroupConfig(
            group_name=g,
            bot_token=_resolve_env(item["bot_token"], g),
            chat_id=_resolve_env(item["chat_id"], g),
            ics_url=_resolve_env(item["ics_url"], g),
            schedule_thread_id=item.get("schedule_thread_id"),
            main_thread_id=item.get("main_thread_id"),
        ))
    return configs


# ----------------------------
# State (git-committed, як в оригіналі)
# ----------------------------
def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_post(state: Dict, key: str, stamp: str) -> bool:
    return state.get(key) != stamp


def mark_posted(state: Dict, key: str, stamp: str) -> None:
    state[key] = stamp


# ----------------------------
# Utils
# ----------------------------
def now_kyiv() -> dt.datetime:
    return dt.datetime.now(tz=KYIV_TZ)


def iso_date(d: dt.date) -> str:
    return d.isoformat()


def env_optional_int(name: str) -> Optional[int]:
    v = os.getenv(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def is_saturday(day: dt.date) -> bool:
    return day.weekday() == 5


def is_sunday(day: dt.date) -> bool:
    return day.weekday() == 6


# ----------------------------
# ICS parsing
# ----------------------------
def fetch_ics(url: str, timeout_s: int = 30) -> str:
    resp = requests.get(url, timeout=timeout_s)
    resp.raise_for_status()
    return resp.text


def _unfold_ics_lines(ics_text: str) -> List[str]:
    raw = ics_text.splitlines()
    out = []
    for line in raw:
        if not line:
            out.append(line)
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if out:
                out[-1] += line[1:]
            else:
                out.append(line.lstrip())
        else:
            out.append(line)
    return out


def _parse_dt(value: str, tzid: Optional[str]) -> dt.datetime:
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        d = dt.datetime.strptime(value, "%Y%m%d").date()
        return dt.datetime(d.year, d.month, d.day, 0, 0,
                           tzinfo=ZoneInfo(tzid) if tzid else KYIV_TZ)
    if value.endswith("Z"):
        base = dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        return base.astimezone(KYIV_TZ)
    naive = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    tz = ZoneInfo(tzid) if tzid else KYIV_TZ
    return naive.replace(tzinfo=tz).astimezone(KYIV_TZ)


def parse_ics_events(ics_text: str) -> List[Event]:
    lines = _unfold_ics_lines(ics_text)
    events: List[Event] = []
    in_event = False
    cur: Dict[str, Tuple[Optional[str], str]] = {}

    def flush():
        nonlocal cur
        if not cur:
            return
        dtstart_tz, dtstart_val = cur.get("DTSTART", (None, ""))
        dtend_tz, dtend_val = cur.get("DTEND", (None, ""))
        if not dtstart_val or not dtend_val:
            cur = {}
            return
        events.append(Event(
            start=_parse_dt(dtstart_val, dtstart_tz),
            end=_parse_dt(dtend_val, dtend_tz),
            summary=ics_unescape(cur.get("SUMMARY", (None, ""))[1]).strip(),
            description=ics_unescape(cur.get("DESCRIPTION", (None, ""))[1]).strip(),
            location=ics_unescape(cur.get("LOCATION", (None, ""))[1]).strip(),
        ))
        cur = {}

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
            continue
        if line == "END:VEVENT":
            if in_event:
                flush()
            in_event = False
            continue
        if not in_event or ":" not in line:
            continue
        left, value = line.split(":", 1)
        key = left
        tzid = None
        if ";" in left:
            key, params = left.split(";", 1)
            m = re.search(r"TZID=([^;]+)", params)
            if m:
                tzid = m.group(1)
        key = key.strip().upper()
        if key in {"DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "LOCATION"}:
            cur[key] = (tzid, value.strip())

    events.sort(key=lambda e: e.start)
    return events


def events_in_range(events: List[Event], start_date: dt.date, end_date: dt.date) -> List[Event]:
    return [ev for ev in events
            if start_date <= ev.start.astimezone(KYIV_TZ).date() <= end_date]


# ----------------------------
# Extractors
# ----------------------------
UA_DOW = {
    0: "Понеділок", 1: "Вівторок", 2: "Середа",
    3: "Четвер", 4: "Пʼятниця", 5: "Субота", 6: "Неділя",
}


def detect_type(tail: str) -> Optional[str]:
    """З ГРС-14Дмб: розпізнає тип і через дужки, і через тире."""
    t = tail.strip().lower()
    if "лекц" in t:
        return "Лекція"
    if "практ" in t or t == "пр." or t == "пр":
        return "Практичне"
    if "лаб" in t:
        return "Лабораторна"
    if "семінар" in t:
        return "Семінар"
    return None


def split_summary(summary: str) -> Tuple[str, Optional[str]]:
    """З ГРС-14Дмб: підтримує формати '— Лекція', ' - Практичне', '(Лекція)'."""
    s = summary.strip()

    if "—" in s:
        left, right = s.rsplit("—", 1)
        etype = detect_type(right)
        if etype:
            return left.strip(), etype

    if " - " in s:
        left, right = s.rsplit(" - ", 1)
        etype = detect_type(right)
        if etype:
            return left.strip(), etype

    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", s)
    if m:
        etype = detect_type(m.group(2))
        if etype:
            return m.group(1).strip(), etype

    return s, None


def _normalize_for_links(text: str) -> str:
    if not text:
        return ""
    return text.replace("\\n", "\n").replace("\u200b", "")


def extract_zoom_links(text: str) -> List[str]:
    t = _normalize_for_links(text)
    links = URL_RE.findall(t)
    zoom = [l for l in links if "zoom.us" in l.lower()]
    rest = [l for l in links if l not in zoom]
    return zoom + rest


def extract_teacher(description: str) -> Optional[str]:
    if not description:
        return None
    patterns = [
        r"^(?:доц\.?|доцент)\s*[:\-]?\s*(.+)$",
        r"^(?:викл\.?|викладач)\s*[:\-]?\s*(.+)$",
        r"^(?:проф\.?|професор)\s*[:\-]?\s*(.+)$",
        r"^(?:асист\.?|асистент)\s*[:\-]?\s*(.+)$",
        r"^(?:Доц\.?|Доцент)\s*[:\-]?\s*(.+)$",
        r"^(?:Викл\.?|Викладач)\s*[:\-]?\s*(.+)$",
        r"^(?:Проф\.?|Професор)\s*[:\-]?\s*(.+)$",
    ]
    for line in [l.strip() for l in description.splitlines() if l.strip()]:
        for pat in patterns:
            m = re.match(pat, line, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None


def extract_passcode(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.replace("\\n", "\n")
    patterns = [
        r"(?:Код\s*доступу|Код\s*доступа|Passcode|Пароль)\s*[:=\-]\s*([^\s,;]+)",
        r"(?:^|\n)\s*Код\s*[:=\-]\s*([^\s,;]+)",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def classify_place(location: str, description: str) -> str:
    blob = f"{location}\n{description}".lower()
    if "online" in blob or "zoom" in blob:
        m = re.search(r"(ауд\.?\s*\d+)", blob, flags=re.IGNORECASE)
        if m:
            return f"🌐 Online (Zoom) • 🏫 {m.group(1).replace('ауд', 'ауд.').strip()}"
        return "🌐 Online (Zoom)"
    m2 = re.search(r"(ауд\.?\s*\d+)", blob, flags=re.IGNORECASE)
    if m2:
        return f"🏫 {m2.group(1).replace('ауд', 'ауд.').strip()}"
    if location.strip():
        return f"📍 {location.strip()}"
    return "📍 (місце не вказано)"


# ----------------------------
# Weather — Open-Meteo, без API ключа
# ----------------------------
def get_weather_dnipro(day: dt.date) -> Optional[Dict]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=48.45&longitude=34.98"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Europe%2FKyiv"
    )
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        dates = data.get("daily", {}).get("time", [])
        if not dates or day.isoformat() not in dates:
            return None
        idx = dates.index(day.isoformat())
        p = data["daily"]["precipitation_probability_max"][idx]
        return {
            "desc": weathercode_ua(data["daily"]["weathercode"][idx]),
            "tmin": int(round(data["daily"]["temperature_2m_min"][idx])),
            "tmax": int(round(data["daily"]["temperature_2m_max"][idx])),
            "p": int(p) if p is not None else None,
        }
    except Exception:
        return None


def weathercode_ua(code: int) -> str:
    mapping = {
        0: "ясно", 1: "переважно ясно", 2: "мінлива хмарність", 3: "хмарно",
        45: "туман", 48: "паморозь / туман",
        51: "мряка", 53: "мряка", 55: "мряка",
        61: "дощ", 63: "дощ", 65: "сильний дощ",
        66: "крижаний дощ", 67: "крижаний дощ",
        71: "сніг", 73: "сніг", 75: "сильний сніг", 77: "снігова крупа",
        80: "зливи", 81: "зливи", 82: "сильні зливи",
        85: "снігопад", 86: "сильний снігопад",
        95: "гроза", 96: "гроза з градом", 99: "гроза з градом",
    }
    return mapping.get(code, f"погода (код: {code})")


def format_weather_block(day: dt.date, label: str) -> str:
    w = get_weather_dnipro(day)
    if not w:
        return ""
    lines = [
        f"⛅ Погода в Дніпрі на {label}:",
        f"• {w['desc']}",
        f"• 🌡️ Мін/Макс: {w['tmin']}°C / {w['tmax']}°C",
    ]
    if w.get("p") is not None:
        lines.append(f"• ☔ Ймовірність опадів: {w['p']}%")
    return "\n".join(lines) + "\n\n"


# ----------------------------
# HTML helpers
# ----------------------------
def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_html_attr(s: str) -> str:
    return escape_html(s).replace('"', "&quot;")


def hhmm(t: dt.datetime) -> str:
    return t.astimezone(KYIV_TZ).strftime("%H:%M")


def fmt_date_short(d: dt.date) -> str:
    return d.strftime("%d.%m")


def separator() -> str:
    return "━━━━━━━━━━━━━━━━━━━━"


# ----------------------------
# Formatting
# ----------------------------
def format_day(events: List[Event], day: dt.date) -> str:
    dow = UA_DOW[day.weekday()]
    lines = [f"📅 <b>{dow}</b> • <b>{fmt_date_short(day)}</b>", ""]

    if not events:
        lines.append("— (пар немає)")
        return "\n".join(lines)

    for ev in events:
        discipline, etype = split_summary(ev.summary)
        teacher = extract_teacher(ev.description)
        passcode = extract_passcode(ev.description + "\n" + ev.location + "\n" + ev.summary)
        place = classify_place(ev.location, ev.description)
        links = extract_zoom_links(ev.description + "\n" + ev.location)
        link = links[0] if links else None
        moodle_url = MOODLE_LINKS_NORM.get(normalize_discipline(discipline))
        pno = pair_no(ev.start)
        pfx = f"{pno} пара " if pno else ""

        lines.append(f"🕒 <b>{pfx}{hhmm(ev.start)}–{hhmm(ev.end)}</b>")
        lines.append(f"📚 <b>{escape_html(discipline)}</b>")

        if moodle_url:
            lines.append(f'📘 <a href="{escape_html_attr(moodle_url)}">Відкрити Moodle</a>')

        if etype:
            lines.append(f"🎓 {etype}")
        if teacher:
            lines.append(f"👩‍🏫 {escape_html(teacher)}")
        lines.append(escape_html(place))

        if link:
            lines.append(f'🔗 <a href="{escape_html_attr(link)}">Відкрити Zoom</a>')

        if passcode:
            lines.append("🔑 Код доступу:")
            lines.append(f"📎 <code>{escape_html(passcode)}</code>")

        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def format_week_message(events: List[Event], start_day: dt.date, end_day: dt.date) -> str:
    header = (
        f"🗓️ <b>Розклад на тиждень</b>\n"
        f"<b>{fmt_date_short(start_day)} – {fmt_date_short(end_day)}</b>\n\n"
    )
    by_day: Dict[dt.date, List[Event]] = {
        start_day + dt.timedelta(days=i): []
        for i in range((end_day - start_day).days + 1)
    }
    for ev in events:
        by_day[ev.start.astimezone(KYIV_TZ).date()].append(ev)
    blocks = []
    for d in by_day:
        blocks.append(separator())
        blocks.append(format_day(by_day[d], d))
    blocks.append(separator())
    return header + "\n".join(blocks) + f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"


# ----------------------------
# Telegram
# ----------------------------
def tg_send_message(token: str, chat_id: str, text: str,
                    message_thread_id: Optional[int] = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")


# ----------------------------
# Per-group runner
# ----------------------------
def run_group(cfg: GroupConfig, mode: str, force: bool,
              state: Dict, today: dt.date) -> bool:
    g = cfg.group_name
    print(f"[{g}] mode={mode}")

    try:
        ics = fetch_ics(cfg.ics_url)
        all_events = parse_ics_events(ics)
    except Exception as e:
        print(f"[{g}] ERROR fetching ICS: {e}")
        return False

    if mode == "today":
        if is_saturday(today) or is_sunday(today):
            print(f"[{g}] Skip: weekend.")
            return False
        target = today
        stamp = f"today:{g}:{iso_date(target)}"
        if not should_post(state, f"last_today_{g}", stamp):
            print(f"[{g}] Already posted today.")
            return False
        day_events = events_in_range(all_events, target, target)
        weather = format_weather_block(target, "сьогодні")
        msg = "<b>Доброго ранку шановні студенти!</b> ☀️\n\n" + weather
        msg += f"🗓️ <b>Розклад на сьогодні ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
        tg_send_message(cfg.bot_token, cfg.chat_id, msg, cfg.main_thread_id)
        mark_posted(state, f"last_today_{g}", stamp)
        print(f"[{g}] Posted today.")
        return True

    elif mode == "tomorrow":
        if is_saturday(today):
            print(f"[{g}] Skip: Saturday.")
            return False
        target = today + dt.timedelta(days=1)
        stamp = f"tomorrow:{g}:{iso_date(target)}"
        if not should_post(state, f"last_tomorrow_{g}", stamp):
            print(f"[{g}] Already posted tomorrow.")
            return False
        day_events = events_in_range(all_events, target, target)
        weather = format_weather_block(target, "завтра")
        msg = "<b>Добрий вечір шановні студенти!</b> 🌙\n\n" + weather
        msg += f"🗓️ <b>Розклад на завтра ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
        tg_send_message(cfg.bot_token, cfg.chat_id, msg, cfg.main_thread_id)
        mark_posted(state, f"last_tomorrow_{g}", stamp)
        print(f"[{g}] Posted tomorrow.")
        return True

    elif mode == "week":
        if not force and not is_sunday(today):
            print(f"[{g}] Skip: not Sunday (use --force to override).")
            return False
        this_monday = today - dt.timedelta(days=today.weekday())
        if force:
            week_start = this_monday
            print(f"[{g}] Force mode: posting current week.")
        else:
            week_start = this_monday + dt.timedelta(days=7)
            print(f"[{g}] Sunday mode: posting next week.")
        week_end = week_start + dt.timedelta(days=6)
        stamp = f"week:{g}:{iso_date(week_start)}:{iso_date(week_end)}"
        if not should_post(state, f"last_week_{g}", stamp):
            print(f"[{g}] Already posted week.")
            return False
        week_events = events_in_range(all_events, week_start, week_end)
        msg = format_week_message(week_events, week_start, week_end)
        if cfg.schedule_thread_id is None:
            print(f"[{g}] WARNING: schedule_thread_id not set, posting to general chat.")
        tg_send_message(cfg.bot_token, cfg.chat_id, msg, cfg.schedule_thread_id)
        mark_posted(state, f"last_week_{g}", stamp)
        print(f"[{g}] Posted week {iso_date(week_start)}–{iso_date(week_end)}.")
        return True

    return False


# ----------------------------
# Main
# ----------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["today", "tomorrow", "week"])
    parser.add_argument("--group", default=None,
                        help="Запустити тільки для однієї групи (необов'язково)")
    parser.add_argument("--force", action="store_true",
                        help="Примусово запустити тижневий розклад в будь-який день")
    args = parser.parse_args()

    configs = load_configs()
    if args.group:
        configs = [c for c in configs if c.group_name == args.group]
        if not configs:
            raise RuntimeError(f"Group '{args.group}' not found in configs.json")

    state = load_state()
    today = now_kyiv().date()
    any_posted = False

    for cfg in configs:
        try:
            posted = run_group(cfg, args.mode, args.force, state, today)
            if posted:
                any_posted = True
        except Exception as e:
            print(f"[{cfg.group_name}] FATAL: {e}")

    if any_posted:
        save_state(state)
        print("State saved.")
    else:
        print("Nothing posted — state unchanged.")


if __name__ == "__main__":
    main()
