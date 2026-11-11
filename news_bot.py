#!/usr/bin/env python3
"""tom-news v2.0.0 — בוט חדשות אישי לטלגרם, עם סיכום AI.

כל ריצה: אוסף ידיעות חדשות מכלי תקשורת (RSS) ומערוצי טלגרם ציבוריים,
מסנן זבל וכפילויות מקומית (בחינם), ושולח את מה שנשאר לסיכום אחד ב-Claude Haiku.
Claude מאחד ידיעות על אותו אירוע להודעה אחת בעברית, עם סטטוס מאומת/שמועה.
פעם בשבוע: דוח עלות + דוח רעש לכל מקור, כדי לזהות ערוצים שמכניסים זבל.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

VERSION = "2.2.0"
IL = ZoneInfo("Asia/Jerusalem")
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
SOURCES_FILE = os.path.join(BASE, "sources.json")

# ---- הגנות עלות ----
MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10, 0.50   # דולר למיליון טוקנים (Haiku 5.5, עד 100K)
MAX_CALLS_PER_DAY = 90             # תקרת קריאות AI ביום (כולל כושלות)
MAX_COST_PER_DAY = 0.15            # תקרת עלות יומית בדולרים (כולל כושלות)
MIN_AI_INTERVAL_MIN = 20           # לכל היותר סיכום אחד כל 20 דקות
MAX_ITEMS_PER_CALL = 25            # לכל היותר ידיעות בקריאה אחת
MAX_ITEM_CHARS = 300               # חיתוך כל ידיעה לפני השליחה ל-AI
MAX_OUT_TOKENS = 4000
FAIL_PAUSE_AFTER = 3               # אחרי 3 כישלונות ברצף – הפסקה
FAIL_PAUSE_MIN = 60
# ---- כללי ----
MAX_AGE = timedelta(hours=3)
MAX_MSGS_PER_RUN = 12
MIN_IMPORTANCE = 2                 # אירועים שוליים (1) לא נשלחים
RECENT_EVENTS_KEEP = 25            # אירועים אחרונים שנשלחים ל-AI לצורך איחוד ואימות
RECENT_EVENTS_HOURS = 6
DUP_KEEP = 400                     # טביעות טקסט לזיהוי כפילויות
DUP_THRESHOLD = 0.6
KEEP_SEEN = 6000
UA = "Mozilla/5.0 (compatible; tom-news/2.0)"

# שורות "חתימה" של ערוצים שאין בהן תוכן
BOILER_RE = re.compile(
    r"(t\.me/|https?://|^@\w+|בטלגרם|הצטרפו|הצטרפות|עקבו אחרינו|לפרסום|פרסמו|ווטסאפ|וואטסאפ|"
    r"אינסטגרם|טיקטוק|לשליחת תיעודים|להצטרפות)", re.I)
# פרסומות מובהקות
AD_RE = re.compile(
    r"(קוד קופון|קוד הנחה|% הנחה|הנחה של|להזמנות|להזמנה|לפרטים נוספים חייגו|בית הימורים|קזינו|"
    r"casino|תשואה מובטחת|לתרומות|לתרומה|הגרלה|מבצע השקה|לרכישה|משלוח חינם)", re.I)

SYSTEM_PROMPT = """אתה עורך חדשות. תקבל רשימת ידיעות גולמיות מכלי תקשורת ומערוצי טלגרם, ורשימת אירועים שכבר פורסמו לאחרונה.
המשימה: להפיק עדכוני חדשות קצרים בעברית, אירוע אחד לכל הודעה.

כללים:
1. אחד ידיעות שעוסקות באותו אירוע לאירוע אחד, עם כל המקורות שלו.
2. התעלם מ: פרסומות, קידום ערוצים, ברכות, תפילות, ציטוטים רגשיים בלי מידע, סרטונים בלי הקשר, דעות בלבד, ותוכן שאין בו חדשה. סמן אותם כ-junk.
3. אם ידיעה עוסקת באירוע שכבר פורסם (ברשימת האירועים האחרונים) ואין בה מידע חדש מהותי – סמן אותה כ-dup ואל תיצור אירוע.
4. אם יש מידע חדש מהותי על אירוע קודם – status "update" עם ref של האירוע הקודם.
5. אם אירוע קודם היה שמועה (verified=false) ועכשיו כלי תקשורת (kind=media) מדווח עליו – status "confirmed" עם ref.
6. verified=true רק אם לפחות ידיעה אחת מסוג media תומכת באירוע. ידיעות telegram בלבד = שמועה.
7. headline: עד 10 מילים. summary: משפט אחד, עד 25 מילים. ניסוח ניטרלי ועובדתי, בלי להוסיף עובדות שלא מופיעות בידיעות, בלי פרטים גרפיים.
8. importance: 3 = דחוף/ביטחוני מהותי, 2 = חשוב, 1 = שולי.
9. ידיעות יכולות להיות בכל שפה (עברית, אנגלית, ערבית, פרסית). תמיד כתוב בעברית.
10. כלכלה ושווקים: דווח רק על מה שמזיז שווקים – החלטות בנקים מרכזיים, נתוני מאקרו מרכזיים, תנועות חדות במדדים/נפט/מטבעות, אירועים מהותיים בחברות גדולות. נתוני מסחר שגרתיים (imbalance, זרימות, דוחות קטנים) = importance 1.
11. ידיעה ממקור בפרסית/ערבית שמוסיפה זווית שלא מופיעה במקורות אחרים – ציין זאת בסיכום ("לפי תקשורת איראנית/ערבית")

החזר JSON בלבד, קומפקטי, בלי רווחים מיותרים, בלי טקסט נוסף ובלי ```. אל תכלול ב-junk/dup ידיעות שכבר שויכו לאירוע:
{"events":[{"status":"new|update|confirmed","ref":null,"headline":"...","summary":"...","verified":true,"importance":2,"item_ids":[1,2]}],"junk":[3,4],"dup":[5]}"""


# ------------------------------------------------------------ איסוף

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "he,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(data, source):
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for c in el:
            ct = _local(c.tag)
            if ct == "link":
                val = (c.text or "").strip() or c.get("href", "")
                if val and "link" not in f:
                    f["link"] = val
            elif ct in ("title", "guid", "id", "pubDate", "published", "updated"):
                f.setdefault(ct, (c.text or "").strip())
        title = strip_tags(f.get("title", ""))
        if not title:
            continue
        link = f.get("link", "")
        uid = f.get("guid") or f.get("id") or link or title
        items.append({"id": f"rss:{source}:{uid}", "kind": "media", "source": source,
                      "text": title, "link": link,
                      "when": parse_date(f.get("pubDate") or f.get("published") or f.get("updated"))})
    return items


_POST_RE = re.compile(r'data-post="([^"]+)"')
_TEXT_RE = re.compile(r'class="tgme_widget_message_text js-message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')


def parse_tg(page, channel):
    items = []
    for chunk in page.split('class="tgme_widget_message_wrap')[1:]:
        m, t = _POST_RE.search(chunk), _TEXT_RE.search(chunk)
        if not m or not t:
            continue
        text = strip_tags(t.group(1))
        if not text:
            continue
        d = _TIME_RE.search(chunk)
        post = m.group(1)
        items.append({"id": f"tg:{post}", "kind": "telegram", "source": channel, "text": text,
                      "link": f"https://t.me/{post}", "when": parse_date(d.group(1)) if d else None})
    return items


def collect(sources):
    items, failed = [], []
    for s in sources.get("media", []):
        try:
            got = parse_feed(fetch(s["url"]), s["name"])
            items += got
            print(f"✓ {s['name']}: {len(got)}")
        except Exception as e:
            failed.append(s["name"])
            print(f"✗ {s['name']}: {type(e).__name__}: {e}")
    for ch in sources.get("telegram", []):
        try:
            got = parse_tg(fetch(f"https://t.me/s/{ch}").decode("utf-8", "ignore"), ch)
            items += got
            if not got:
                failed.append(ch)
            print(f"{'✓' if got else '✗'} tg/{ch}: {len(got)}")
        except Exception as e:
            failed.append(ch)
            print(f"✗ tg/{ch}: {type(e).__name__}: {e}")
    return items, failed


# ------------------------------------------------------------ סינון מקומי (חינם)

def clean_text(text):
    lines = [ln.strip() for ln in text.split("\n")]
    keep = [ln for ln in lines if ln and not BOILER_RE.search(ln)]
    return re.sub(r"\s+", " ", " ".join(keep)).strip()


def fingerprint(text):
    t = re.sub(r"[^\w\s]", " ", text.lower())
    words = [w for w in t.split() if len(w) > 1]
    return set(words)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def local_filter(items, state):
    """מחזיר (טובים, זבל, כפולים). מעדכן את מאגר טביעות הטקסט."""
    recent = [set(x) for x in state.get("fps", [])]
    good, junk, dups = [], [], []
    for it in items:
        txt = clean_text(it["text"]) if it["kind"] == "telegram" else it["text"]
        if len(txt) < 25 or (it["kind"] == "telegram" and AD_RE.search(txt)):
            junk.append(it)
            continue
        fp = fingerprint(txt)
        if any(jaccard(fp, r) >= DUP_THRESHOLD for r in recent):
            dups.append(it)
            continue
        recent.append(fp)
        it["clean"] = txt[:MAX_ITEM_CHARS]
        good.append(it)
    state["fps"] = [sorted(x) for x in recent[-DUP_KEEP:]]
    return good, junk, dups


# ------------------------------------------------------------ Claude

def call_claude(api_key, items, recent_events):
    payload_items = [{"id": i + 1, "kind": it["kind"], "source": it["source"], "text": it["clean"]}
                     for i, it in enumerate(items)]
    user = json.dumps({"recent_events": recent_events, "items": payload_items}, ensure_ascii=False)
    body = json.dumps({
        "model": MODEL, "max_tokens": MAX_OUT_TOKENS, "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage", {})
    tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    stop = data.get("stop_reason")
    try:
        return extract_json(text, stop), tin, tout, stop
    except ValueError:
        return None, tin, tout, stop


def extract_json(text, stop_reason=None):
    """מחלץ את אובייקט ה-JSON גם אם יש טקסט או ``` מסביבו."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        print(f"תשובת AI ללא JSON (stop={stop_reason}): {text[:300]!r}")
        raise ValueError(f"no JSON in reply (stop={stop_reason})")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        print(f"JSON שבור (stop={stop_reason}): {text[:300]!r}")
        raise ValueError(f"bad JSON: {e} (stop={stop_reason})")


# ------------------------------------------------------------ טלגרם

def tg_send(token, chat, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20):
                return True
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code == 429:
                m = re.search(r'"retry_after":\s*(\d+)', body)
                time.sleep((int(m.group(1)) if m else 5) + 1)
                continue
            print(f"שליחה נכשלה ({e.code}): {body[:200]}")
            return False
        except Exception as e:
            print(f"שגיאת רשת בשליחה: {type(e).__name__}")
            time.sleep(3)
    return False


def fmt_event(ev, items, src_names, now):
    hhmm = now.astimezone(IL).strftime("%H:%M")
    srcs, links = [], []
    for iid in ev.get("item_ids", []):
        if 1 <= iid <= len(items):
            it = items[iid - 1]
            name = src_names.get(it["source"], it["source"])
            if name not in srcs:
                srcs.append(name)
            if it["link"] and len(links) < 2 and (it["kind"] == "media" or not links):
                links.append(it["link"])
    st = ev.get("status", "new")
    if st == "confirmed":
        head = "🔁 <b>אושר ✅</b>"
    elif ev.get("verified"):
        head = "✅ <b>מאומת</b>"
    else:
        head = "⚠️ <b>שמועה</b>"
    if st == "update":
        head += " · עדכון"
    if ev.get("importance") == 3:
        head = "🔴 " + head
    n = len(srcs)
    head += f" · {n} {'מקור' if n == 1 else 'מקורות'} · {hhmm}"
    out = (f"{head}\n<b>{html.escape(ev.get('headline', ''))}</b>\n"
           f"{html.escape(ev.get('summary', ''))}")
    if srcs:
        out += f"\n<i>{html.escape(', '.join(srcs))}</i>"
    for i, ln in enumerate(links):
        out += f'\n<a href="{html.escape(ln, quote=True)}">מקור {i + 1}</a>'
    return out


# ------------------------------------------------------------ מצב ודוחות

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    seen = st.get("seen", {})
    if len(seen) > KEEP_SEEN:
        st["seen"] = dict(sorted(seen.items(), key=lambda kv: kv[1])[-KEEP_SEEN:])
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))


def bump(st, source, key, n=1):
    s = st.setdefault("stats", {}).setdefault(source, {"in": 0, "junk": 0, "dup": 0, "used": 0})
    s[key] = s.get(key, 0) + n


def weekly_report(st, src_names):
    u = st.get("usage", {})
    week_cost = u.get("week_cost", 0.0)
    lines = [f"📊 <b>דוח שבועי</b> · גרסה {VERSION}",
             f"עלות השבוע: ${week_cost:.3f} · מצטבר: ${u.get('total_cost', 0.0):.3f}",
             f"קריאות AI השבוע: {u.get('week_calls', 0)}", "", "<b>מקורות</b> (נקלטו / זבל / כפול / שימושי):"]
    rows = []
    for src, s in st.get("stats", {}).items():
        tot = s.get("in", 0)
        used = s.get("used", 0)
        ratio = used / tot if tot else 0
        flag = " ⚠️ רעש גבוה" if tot >= 30 and ratio < 0.15 else ""
        rows.append((ratio, f"{html.escape(src_names.get(src, src))}: {tot} / {s.get('junk', 0)} / "
                            f"{s.get('dup', 0)} / {used} ({ratio:.0%}){flag}"))
    lines += [r for _, r in sorted(rows)]
    lines.append("\nמקור עם ⚠️ מבזבז טוקנים בלי לתרום – כדאי להוריד אותו.")
    return "\n".join(lines)


# ------------------------------------------------------------ ריצה

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not token or not chat:
        print("חסרים הסודות TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        sys.exit(1)

    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)
    src_names = {s["name"]: s["name"] for s in sources.get("media", [])}
    src_names.update(sources.get("telegram_names", {}))

    st = load_state()
    first_run = st is None
    st = st or {}
    st.setdefault("seen", {})
    now = datetime.now(timezone.utc)
    today = now.astimezone(IL).strftime("%Y-%m-%d")
    week = now.astimezone(IL).strftime("%G-W%V")
    u = st.setdefault("usage", {})
    if u.get("day") != today:
        u.update(day=today, day_calls=0, day_cost=0.0, cap_notified=False)
    if u.get("week") != week:
        if u.get("week") and st.get("stats"):
            tg_send(token, chat, weekly_report(st, src_names))
            st["stats"] = {}
        u.update(week=week, week_cost=0.0, week_calls=0)

    last_ai = parse_date(u.get("last_ai", ""))
    if not first_run and api_key and last_ai and now - last_ai < timedelta(minutes=MIN_AI_INTERVAL_MIN):
        print(f"סיכום אחרון לפני פחות מ-{MIN_AI_INTERVAL_MIN} דק' – מחכה לריצה הבאה")
        save_state(st)
        return

    items, failed = collect(sources)
    new, ids = [], set()
    for it in items:
        if it["id"] in st["seen"] or it["id"] in ids:
            continue
        ids.add(it["id"])
        new.append(it)
    stamp = now.isoformat()
    for it in new:
        st["seen"][it["id"]] = stamp

    if first_run:
        local_filter(new, st)  # בונה מאגר כפילויות מהקיים, בלי לשלוח
        msg = (f"🟢 <b>גרסה {VERSION} פעילה</b> – סיכום AI\n"
               f"מעכשיו כל אירוע מגיע פעם אחת, מסוכם בעברית, עם כל המקורות שדיווחו.\n\n"
               f"✅ מאומת = כלי תקשורת דיווח\n⚠️ שמועה = רק ערוצי טלגרם\n🔁 אושר = שמועה שאומתה\n"
               f"🔴 = דחוף\n\nמקורות: {len(sources.get('media', [])) + len(sources.get('telegram', []))}")
        if failed:
            msg += f"\nנכשלו: {html.escape(', '.join(failed))}"
        msg += "\n📊 דוח עלות ורעש לכל מקור – פעם בשבוע."
        tg_send(token, chat, msg)
        save_state(st)
        return

    fresh = [i for i in new if i["when"] is None or now - i["when"] <= MAX_AGE]
    for it in fresh:
        bump(st, it["source"], "in")
    good, junk, dups = local_filter(fresh, st)
    for it in junk:
        bump(st, it["source"], "junk")
    for it in dups:
        bump(st, it["source"], "dup")
    print(f"חדשים {len(new)}, טריים {len(fresh)}, אחרי סינון מקומי {len(good)} "
          f"(זבל {len(junk)}, כפול {len(dups)})")

    if not good:
        save_state(st)
        return
    if not api_key:
        print("אין ANTHROPIC_API_KEY – מדלג על סיכום")
        save_state(st)
        return
    if u.get("day_calls", 0) >= MAX_CALLS_PER_DAY or u.get("day_cost", 0.0) >= MAX_COST_PER_DAY:
        if not u.get("cap_notified"):
            tg_send(token, chat, f"⏸️ הגעתי לתקרה היומית (${u.get('day_cost', 0):.2f}, "
                                 f"{u.get('day_calls', 0)} קריאות). ממשיך מחר.")
            u["cap_notified"] = True
        save_state(st)
        return
    paused = parse_date(u.get("paused_until", ""))
    if paused and now < paused:
        print(f"מושהה עד {paused}")
        save_state(st)
        return

    good.sort(key=lambda i: i["when"] or now)
    batch = good[-MAX_ITEMS_PER_CALL:]
    cutoff = now - timedelta(hours=RECENT_EVENTS_HOURS)
    recent = [e for e in st.get("events", []) if parse_date(e["t"]) and parse_date(e["t"]) >= cutoff]
    recent = recent[-RECENT_EVENTS_KEEP:]
    recent_for_ai = [{"ref": e["ref"], "headline": e["h"], "verified": e["v"]} for e in recent]

    result, err = None, None
    for attempt in range(2):
        try:
            result, tin, tout, stop = call_claude(api_key, batch, recent_for_ai)
        except Exception as e:          # שגיאת API/רשת – לא מנסים שוב
            err = e
            break
        cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
        u["day_calls"] = u.get("day_calls", 0) + 1
        u["week_calls"] = u.get("week_calls", 0) + 1
        u["day_cost"] = u.get("day_cost", 0.0) + cost
        u["week_cost"] = u.get("week_cost", 0.0) + cost
        u["total_cost"] = u.get("total_cost", 0.0) + cost
        print(f"AI ניסיון {attempt + 1}: {tin} in / {tout} out = ${cost:.4f} (stop={stop}, {len(batch)} ידיעות)")
        if result is not None:
            break
        err = ValueError(f"תשובה לא תקינה (stop={stop})")
        if stop == "max_tokens" and len(batch) > 5:
            batch = batch[-(len(batch) // 2):]   # נחתך – מנסים עם חצי מהידיעות
    if result is None:
        u["fails"] = u.get("fails", 0) + 1
        print(f"שגיאת AI ({u['fails']} ברצף): {err}")
        hint = "\n(נגמר הקרדיט – צריך לטעון)" if re.search(r"credit|balance", str(err), re.I) else ""
        if u["fails"] >= FAIL_PAUSE_AFTER:
            u["paused_until"] = (now + timedelta(minutes=FAIL_PAUSE_MIN)).isoformat()
            u["fails"] = 0
            tg_send(token, chat, f"⚠️ {FAIL_PAUSE_AFTER} כישלונות ברצף – משהה את הסיכום לשעה.\n"
                                 f"{html.escape(str(err)[:200])}{hint}")
        elif u.get("fails") == 1 and hint:
            tg_send(token, chat, f"⚠️ הסיכום נכשל: {html.escape(str(err)[:200])}{hint}")
        save_state(st)
        return
    u["fails"] = 0
    u["last_ai"] = stamp

    for iid in result.get("junk", []):
        if isinstance(iid, int) and 1 <= iid <= len(batch):
            bump(st, batch[iid - 1]["source"], "junk")
    for iid in result.get("dup", []):
        if isinstance(iid, int) and 1 <= iid <= len(batch):
            bump(st, batch[iid - 1]["source"], "dup")

    events = sorted(result.get("events", []), key=lambda e: -int(e.get("importance") or 1))
    events = [e for e in events if int(e.get("importance") or 1) >= MIN_IMPORTANCE][:MAX_MSGS_PER_RUN]
    stored = st.setdefault("events", [])
    sent_ok = 0
    for ev in events:
        for iid in ev.get("item_ids", []):
            if isinstance(iid, int) and 1 <= iid <= len(batch):
                bump(st, batch[iid - 1]["source"], "used")
        if tg_send(token, chat, fmt_event(ev, batch, src_names, now)):
            sent_ok += 1
        ref = hashlib.md5((ev.get("headline", "") + stamp).encode()).hexdigest()[:6]
        stored.append({"ref": ref, "h": ev.get("headline", ""), "v": bool(ev.get("verified")), "t": stamp})
        time.sleep(3.2)
    st["events"] = stored[-60:]
    save_state(st)
    print(f"נשלחו {sent_ok}/{len(events)} אירועים")


if __name__ == "__main__":
    main()
