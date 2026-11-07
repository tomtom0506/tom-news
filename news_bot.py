#!/usr/bin/env python3
"""tom-news — בוט חדשות אישי לטלגרם.

מושך עדכונים מכלי תקשורת (RSS) ומערוצי טלגרם ציבוריים,
ומפרסם בערוץ הפרטי עם תווית: ✅ כלי תקשורת / ⚠️ לא מאומת.
רץ ב-GitHub Actions כל 5 דקות. רשימת המקורות: sources.json
"""
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

VERSION = "1.0.0"
IL = ZoneInfo("Asia/Jerusalem")
BASE = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(BASE, "seen.json")
SOURCES_FILE = os.path.join(BASE, "sources.json")
MAX_AGE = timedelta(hours=3)   # לא לפרסם ידיעות ישנות מ-3 שעות
MAX_PER_RUN = 25               # תקרת הודעות לריצה, כדי לא להציף
MAX_TEXT = 700                 # אורך מקסימלי לטקסט הודעה
KEEP_SEEN = 5000               # כמה מזהים לזכור למניעת כפילויות
UA = "Mozilla/5.0 (compatible; tom-news/1.0)"


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "he,en;q=0.8"})
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
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(data, source_name):
    """RSS 2.0 או Atom -> רשימת ידיעות."""
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
        when = parse_date(f.get("pubDate") or f.get("published") or f.get("updated"))
        items.append({"id": f"rss:{source_name}:{uid}", "kind": "verified",
                      "source": source_name, "text": title, "link": link, "when": when})
    return items


_POST_RE = re.compile(r'data-post="([^"]+)"')
_TEXT_RE = re.compile(r'class="tgme_widget_message_text js-message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')


def parse_tg(page, channel):
    """דף התצוגה הציבורי t.me/s/<ערוץ> -> רשימת ידיעות."""
    items = []
    for chunk in page.split('class="tgme_widget_message_wrap')[1:]:
        m = _POST_RE.search(chunk)
        t = _TEXT_RE.search(chunk)
        if not m or not t:
            continue  # הודעה בלי טקסט (תמונה/וידאו בלבד)
        text = strip_tags(t.group(1))
        if not text:
            continue
        d = _TIME_RE.search(chunk)
        post = m.group(1)
        items.append({"id": f"tg:{post}", "kind": "unverified", "source": channel,
                      "text": text, "link": f"https://t.me/{post}",
                      "when": parse_date(d.group(1)) if d else None})
    return items


def fmt(it, now):
    when = it["when"] or now
    hhmm = when.astimezone(IL).strftime("%H:%M")
    text = it["text"]
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rsplit(" ", 1)[0] + "…"
    src = html.escape(it["source"])
    if it["kind"] == "verified":
        head = f"✅ <b>{src}</b> · {hhmm}"
    else:
        head = f"⚠️ <b>לא מאומת</b> · {src} · {hhmm}"
    link = ""
    if it["link"]:
        link = f'\n<a href="{html.escape(it["link"], quote=True)}">למקור</a>'
    return f"{head}\n{html.escape(text)}{link}"


def tg_send(token, chat, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"}).encode()
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20):
                return True
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code == 429:  # הגבלת קצב של טלגרם
                m = re.search(r'"retry_after":\s*(\d+)', body)
                time.sleep((int(m.group(1)) if m else 5) + 1)
                continue
            print(f"שליחה נכשלה ({e.code}): {body[:200]}")
            return False
        except Exception as e:  # לא מדפיסים את ה-URL כדי לא לחשוף את הטוקן
            print(f"שגיאת רשת בשליחה: {type(e).__name__}")
            time.sleep(3)
    return False


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return None
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    if len(seen) > KEEP_SEEN:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-KEEP_SEEN:])
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)


def collect(sources):
    items, ok, failed = [], [], []
    for s in sources.get("verified", []):
        try:
            got = parse_feed(fetch(s["url"]), s["name"])
            items += got
            ok.append(s["name"])
            print(f"✓ {s['name']}: {len(got)}")
        except Exception as e:
            failed.append(s["name"])
            print(f"✗ {s['name']}: {type(e).__name__}: {e}")
    for ch in sources.get("unverified_telegram", []):
        try:
            got = parse_tg(fetch(f"https://t.me/s/{ch}").decode("utf-8", "ignore"), ch)
            items += got
            (ok if got else failed).append(ch)
            print(f"{'✓' if got else '✗'} tg/{ch}: {len(got)}")
        except Exception as e:
            failed.append(ch)
            print(f"✗ tg/{ch}: {type(e).__name__}: {e}")
    return items, ok, failed


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("חסרים הסודות TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        sys.exit(1)

    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}

    items, ok, failed = collect(sources)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()

    new, ids = [], set()
    for it in items:
        if it["id"] in seen or it["id"] in ids:
            continue
        ids.add(it["id"])
        new.append(it)
    for it in new:
        seen[it["id"]] = stamp

    if first_run:
        # ריצה ראשונה: לא מציפים בעבר, רק מסמנים הכל כנראה ומודיעים שהבוט חי
        msg = (f"🟢 <b>הבוט פעיל</b> · גרסה {VERSION}\n"
               f"מעכשיו יגיעו לכאן עדכונים חדשים, כל כ-5 דקות.\n\n"
               f"✅ = כלי תקשורת\n⚠️ = רשתות, לא מאומת\n\n"
               f"מקורות תקינים ({len(ok)}): {html.escape(', '.join(ok)) or '—'}")
        if failed:
            msg += f"\nמקורות שנכשלו ({len(failed)}): {html.escape(', '.join(failed))}"
        tg_send(token, chat, msg)
    else:
        fresh = [i for i in new if i["when"] is None or now - i["when"] <= MAX_AGE]
        fresh.sort(key=lambda i: i["when"] or now)
        skipped = max(0, len(fresh) - MAX_PER_RUN)
        if skipped:
            fresh = fresh[-MAX_PER_RUN:]
        sent = 0
        for it in fresh:
            if tg_send(token, chat, fmt(it, now)):
                sent += 1
            time.sleep(3.2)  # טלגרם מגביל בערך 20 הודעות לדקה לערוץ
        if skipped:
            tg_send(token, chat, f"ℹ️ דילגתי על {skipped} עדכונים ישנים יותר בגלל עומס.")
        print(f"נשלחו {sent}, דולגו {skipped}, חדשים {len(new)}")

    save_seen(seen)


if __name__ == "__main__":
    main()
