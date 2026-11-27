"""בדיקות tom-news v3 – הכל עם נתוני דמה. אין שום גישה לרשת: urlopen חסום בכל הבדיקות,
ה-API של Anthropic והשליחה לטלגרם מוחלפים בפונקציות דמה.
הרצה: python3 -m unittest discover -s tests -v"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import news_bot as nb  # noqa: E402
import csp_hash  # noqa: E402

with open(os.path.join(ROOT, "sources.json"), encoding="utf-8") as _f:
    REAL_SOURCES = json.load(_f)
SRC_BY_URL = {s["url"]: s for s in nb.normalize_sources(REAL_SOURCES)}


def il(y, m, d, h, mi=0):
    """זמן בשעון ישראל → UTC."""
    return datetime(y, m, d, h, mi, tzinfo=nb.IL).astimezone(timezone.utc)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def words(seed):
    """6 מילים ייחודיות – כדי שהסינון המקומי לא יסמן כפילות."""
    h = hashlib.md5(seed.encode()).hexdigest()
    return " ".join(f"מילה{h[i:i + 5]}" for i in range(0, 30, 5))


class Bot:
    """סביבת הרצה מבודדת: תיקייה זמנית, זמן קבוע, fetch/AI/טלגרם מזויפים."""

    def __init__(self, tc, now, settings=None, settings_raw=None, state=None, ai=None, api_key="test-key"):
        self.tc, self.now = tc, now
        self.dir = tempfile.mkdtemp()
        tc.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copy(os.path.join(ROOT, "sources.json"), self.dir)
        if settings_raw is not None:
            with open(os.path.join(self.dir, "settings.json"), "w", encoding="utf-8") as f:
                f.write(settings_raw)
        elif settings is not None:
            write_json(os.path.join(self.dir, "settings.json"), settings)
        if state is None:   # מצב "ותיק" שקט: בלי דוח בריאות / הודעת גרסה / QA
            state = {"seen": {}, "usage": {"version": nb.VERSION, "day": self.il_day(),
                                           "health_day": self.il_day(),
                                           "week": now.astimezone(nb.IL).strftime("%G-W%V"),
                                           "w3": {"start": now.isoformat()}}}
        if state is not False:
            write_json(os.path.join(self.dir, "state.json"), state)
        self.ai = ai or (lambda req: {"events": []})
        self.api_key = api_key
        self.fetched, self.ai_calls, self.sent = [], [], []

    def il_day(self):
        return self.now.astimezone(nb.IL).strftime("%Y-%m-%d")

    # ---- דמה ----
    def fetch(self, url, timeout=20):
        self.fetched.append(url)
        s = SRC_BY_URL[url]
        text = f"{s['id']} מדווח {words(url + str(self.now))}"
        when = self.now - timedelta(minutes=5)
        if s["kind"] == "media":
            return (f'<?xml version="1.0"?><rss><channel><item><title>{text}</title>'
                    f'<link>https://example.com/{abs(hash(url))}</link><guid>{url}{self.now}</guid>'
                    f'<pubDate>{format_datetime(when)}</pubDate></item></channel></rss>').encode()
        post = f"{s['id']}/{abs(hash(str(self.now))) % 99999}"
        return (f'<div class="tgme_widget_message_wrap"><div data-post="{post}">'
                f'<div class="tgme_widget_message_text js-message_text">{text}</div>'
                f'<time datetime="{when.isoformat()}"></time></div>').encode()

    def claude(self, api_key, system, user, max_tokens):
        req = {"system": system, "user": json.loads(user), "max_tokens": max_tokens}
        self.ai_calls.append(req)
        out = self.ai(req)
        if isinstance(out, tuple):
            return out
        return json.dumps(out, ensure_ascii=False), 1000, 500, "end_turn"

    def send(self, token, chat, text):
        self.sent.append(text)
        return True

    def run(self):
        now = self.now

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        def no_net(*a, **k):
            raise AssertionError("ניסיון גישה לרשת בבדיקה!")

        env = {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y", "ANTHROPIC_API_KEY": self.api_key}
        d = self.dir
        with mock.patch.multiple(nb, STATE_FILE=os.path.join(d, "state.json"),
                                 SOURCES_FILE=os.path.join(d, "sources.json"),
                                 SETTINGS_FILE=os.path.join(d, "settings.json"),
                                 DATA_DIR=os.path.join(d, "docs", "data"),
                                 datetime=FakeDT, fetch=self.fetch, claude_request=self.claude,
                                 tg_send=self.send), \
                mock.patch("urllib.request.urlopen", no_net), \
                mock.patch.object(nb.time, "sleep", lambda s: None), \
                mock.patch.dict(os.environ, env):
            nb.main()
        return self

    # ---- קריאת תוצאות ----
    def data(self, name):
        p = os.path.join(self.dir, "docs", "data", name)
        return read_json(p) if os.path.exists(p) else None

    def state(self):
        return read_json(os.path.join(self.dir, "state.json"))

    def summary_calls(self):
        return [c for c in self.ai_calls if c["system"] == nb.SYSTEM_PROMPT]

    def daily_calls(self):
        return [c for c in self.ai_calls if c["system"] == nb.DAILY_PROMPT]


def ai_events(*specs):
    """בונה פונקציית AI: כל spec = (tp, importance, ec). כל אירוע מקבל ידיעה אחת."""
    def f(req):
        items = req["user"]["items"]
        evs = []
        for i, (tp, imp, ec) in enumerate(specs):
            evs.append({"status": "new", "ref": None, "headline": f"כותרת {tp} {i}", "summary": "סיכום",
                        "verified": True, "importance": imp, "tp": tp, "ec": ec,
                        "item_ids": [min(i + 1, len(items))]})
        return {"events": evs, "junk": [], "dup": []}
    return f


NOON = il(2026, 11, 27, 11, 0)   # מחוץ לחלונות הסיכום היומי


class TestAiSwitch(unittest.TestCase):
    def test_ai_disabled_zero_api_calls_and_items_marked_seen(self):
        b = Bot(self, NOON, settings={"ai_enabled": False}).run()
        self.assertEqual(b.ai_calls, [])
        self.assertGreater(len(b.state()["seen"]), 0, "הידיעות צריכות להיות מסומנות כנראו")
        self.assertGreater(len(b.fetched), 0)

    def test_ai_disabled_no_daily_even_in_window(self):
        b = Bot(self, il(2026, 11, 27, 8, 10), settings={"ai_enabled": False})
        b.run()
        self.assertEqual(b.ai_calls, [])
        self.assertIsNone(b.data("daily.json"))

    def test_real_claude_request_blocked_in_tests(self):
        """שומר: גם קריאה ישירה ל-API האמיתי לא יוצאת לרשת בבדיקות."""
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("net")):
            with self.assertRaises(AssertionError):
                nb.claude_request("k", "s", "u", 10)


class TestTopics(unittest.TestCase):
    def test_topic_off_sources_not_read_and_events_not_saved(self):
        topics = {t: True for t in nb.TOPICS}
        topics["ביטחון"] = False
        b = Bot(self, NOON, settings={"topics": topics},
                ai=ai_events(("sec", 3, "IL"), ("eco", 2, "US"))).run()
        fetched_ids = {SRC_BY_URL[u]["id"] for u in b.fetched}
        self.assertNotIn("New_security8200", fetched_ids, "מקור שכל הנושאים שלו כבויים לא אמור להיקרא")
        self.assertIn("ynet", fetched_ids, "מקור עם נושא פעיל נוסף ממשיך להיקרא")
        self.assertEqual(len(b.summary_calls()), 1)
        self.assertNotIn("sec", b.summary_calls()[0]["user"]["topics"])
        evs = b.data("events.json")["events"]
        self.assertEqual([e["topic"] for e in evs], ["כלכלה"])
        self.assertEqual(b.sent, [], "אירוע ביטחון דחוף סונן – לא נשלח")

    def test_unknown_topic_kept_only_when_all_topics_on(self):
        b = Bot(self, NOON, ai=ai_events(("zzz", 2, "IL"))).run()
        self.assertEqual(b.data("events.json")["events"][0]["topic"], nb.OTHER_TOPIC)
        topics = {t: True for t in nb.TOPICS}
        topics["עולם"] = False
        b2 = Bot(self, NOON, settings={"topics": topics}, ai=ai_events(("zzz", 2, "IL"))).run()
        self.assertEqual(b2.data("events.json")["events"], [])

    def test_all_topics_off_no_sources_no_ai(self):
        b = Bot(self, NOON, settings={"topics": {t: False for t in nb.TOPICS}}).run()
        self.assertEqual(b.fetched, [])
        self.assertEqual(b.ai_calls, [])

    def test_country_filters(self):
        b = Bot(self, NOON, settings={"source_countries": ["ארה\"ב"], "event_countries": ["ארה\"ב"]},
                ai=ai_events(("eco", 2, "US"), ("sec", 2, "IR"))).run()
        self.assertEqual({SRC_BY_URL[u]["country"] for u in b.fetched}, {"ארה\"ב"})
        evs = b.data("events.json")["events"]
        self.assertEqual([e["ec"] for e in evs], ["ארה\"ב"])

    def test_source_switch_off(self):
        b = Bot(self, NOON, settings={"sources": {"ynet": False, "amitsegal": False}}).run()
        ids = {SRC_BY_URL[u]["id"] for u in b.fetched}
        self.assertFalse(ids & {"ynet", "amitsegal"})
        self.assertIn("BBC", ids)


class TestTelegram(unittest.TestCase):
    def test_only_urgent_sent(self):
        b = Bot(self, NOON, ai=ai_events(("sec", 3, "IL"), ("pol", 2, "IL"), ("eco", 1, "US"))).run()
        self.assertEqual(len(b.sent), 1)
        self.assertIn("🔴", b.sent[0])
        self.assertIn("כותרת sec 0", b.sent[0])
        self.assertEqual(len(b.data("events.json")["events"]), 3, "כל האירועים נשמרים לאתר")

    def test_telegram_disabled_only_system_alerts(self):
        st = {"seen": {}, "usage": {"version": nb.VERSION, "day": "2026-11-27", "health_day": "2026-11-27",
                                    "week": NOON.astimezone(nb.IL).strftime("%G-W%V"),
                                    "w3": {"start": NOON.isoformat()}, "day_calls": 0, "day_cost": 0.0}}
        b = Bot(self, NOON, settings={"telegram_enabled": False}, state=st,
                ai=ai_events(("sec", 3, "IL"))).run()
        self.assertEqual(b.sent, [], "דחוף לא נשלח כשהטלגרם כבוי")
        self.assertEqual(len(b.data("events.json")["events"]), 1, "אבל נשמר לאתר")
        # התראת מערכת (תקרה) – נשלחת גם כשהטלגרם כבוי
        st["usage"]["day_calls"] = nb.MAX_CALLS_PER_DAY
        b2 = Bot(self, NOON, settings={"telegram_enabled": False}, state=st).run()
        self.assertEqual(b2.ai_calls, [])
        self.assertEqual(len(b2.sent), 1)
        self.assertIn("תקרה", b2.sent[0])

    def test_version_notice_is_system_message(self):
        st = {"seen": {}, "usage": {"day": "2026-11-27", "health_day": "2026-11-27",
                                    "week": NOON.astimezone(nb.IL).strftime("%G-W%V"),
                                    "w3": {"start": NOON.isoformat()}}}
        b = Bot(self, NOON, settings={"telegram_enabled": False}, state=st).run()
        self.assertEqual(len(b.sent), 1)
        self.assertIn(nb.SITE_URL, b.sent[0])
        b.now = NOON + timedelta(minutes=30)
        b.sent.clear()
        b.run()
        self.assertEqual(b.sent, [], "הודעת הגרסה נשלחת פעם אחת בלבד")


class TestDaily(unittest.TestCase):
    def daily_ai(self, req):
        if req["system"] == nb.DAILY_PROMPT:
            return {"s": "יום עמוס. שני משפטים.", "top": [1, 2, 99, "x"]}
        return ai_events(("sec", 2, "IL"), ("pol", 3, "IL"))(req)

    def test_only_in_windows_once_per_window(self):
        b = Bot(self, il(2026, 11, 27, 7, 30), ai=self.daily_ai)
        b.run()                                   # לפני 8:00 – אין סיכום יומי (אבל נוצרים אירועים)
        self.assertEqual(b.daily_calls(), [])
        self.assertGreater(len(b.data("events.json")["events"]), 0)
        for t, expect in [((8, 5), 1), ((8, 25), 1), ((9, 50), 1), ((10, 5), 1), ((12, 59), 1),
                          ((13, 0), 2), ((14, 40), 2), ((19, 10), 3), ((20, 59), 3), ((21, 30), 3)]:
            b.now = il(2026, 11, 27, *t)
            b.run()
            self.assertEqual(len(b.daily_calls()), expect, f"בשעה {t}")
        d = b.data("daily.json")
        self.assertEqual(d["slot"], "19:00")
        self.assertEqual(d["summary"], "יום עמוס. שני משפטים.")
        self.assertTrue(1 <= len(d["top"]) <= 4)
        self.assertTrue(all("h" in e for e in d["top"]))
        b.now = il(2026, 11, 28, 7, 0)            # יום חדש: אירועים מהבוקר
        b.run()
        b.now = il(2026, 11, 28, 8, 1)            # חלון חדש
        b.run()
        self.assertEqual(len(b.daily_calls()), 4)

    def test_daily_counted_in_caps(self):
        b = Bot(self, il(2026, 11, 27, 7, 30), ai=self.daily_ai).run()
        calls_before = b.state()["usage"]["day_calls"]
        b.now = il(2026, 11, 27, 8, 5)
        b.run()
        u = b.state()["usage"]
        self.assertEqual(u["day_calls"], calls_before + 2, "סיכום רגיל + סיכום יומי")

    def test_daily_skipped_when_cap_reached(self):
        st = {"seen": {}, "usage": {"version": nb.VERSION, "day": "2026-11-27", "health_day": "2026-11-27",
                                    "week": "2026-W48", "w3": {"start": il(2026, 11, 27, 8).isoformat()},
                                    "day_cost": nb.MAX_COST_PER_DAY}}
        b = Bot(self, il(2026, 11, 27, 8, 5), state=st, ai=self.daily_ai).run()
        self.assertEqual(b.ai_calls, [])

    def test_daily_empty_day_no_ai(self):
        b = Bot(self, il(2026, 11, 27, 8, 5), settings={"ai_enabled": True},
                ai=lambda r: {"events": []} if r["system"] == nb.SYSTEM_PROMPT else self.fail("לא אמור לקרוא AI"))
        b.run()
        self.assertEqual(b.daily_calls(), [])
        d = b.data("daily.json")
        self.assertEqual(d["top"], [])
        self.assertEqual(d["count"], 0)

    def test_daily_bad_reply_not_retried(self):
        def ai(req):
            if req["system"] == nb.DAILY_PROMPT:
                return ("לא JSON", 100, 50, "end_turn")
            return ai_events(("sec", 2, "IL"))(req)
        b = Bot(self, il(2026, 11, 27, 7, 30), ai=ai).run()
        b.now = il(2026, 11, 27, 8, 5)
        b.run()
        b.now = il(2026, 11, 27, 8, 30)
        b.run()
        self.assertEqual(len(b.daily_calls()), 1, "כישלון לא גורר ניסיון חוזר באותו חלון")
        self.assertIsNone(b.data("daily.json"))
        # 3 סיכומים רגילים (7:30, 8:05, 8:30) + קריאה יומית כושלת אחת – גם היא נספרת
        self.assertEqual(b.state()["usage"]["day_calls"], 4, "גם הקריאה הכושלת נספרה")


class TestSettingsFile(unittest.TestCase):
    def test_missing(self):
        s, ok = nb.load_settings("/nonexistent/settings.json")
        self.assertTrue(ok)
        self.assertEqual(s, nb.DEFAULT_SETTINGS)

    def test_broken_and_wrong_types(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "s.json")
        for raw in ["{not json", "[]", "null", '"x"']:
            with open(p, "w") as f:
                f.write(raw)
            s, ok = nb.load_settings(p)
            self.assertFalse(ok, raw)
            self.assertEqual(s, nb.DEFAULT_SETTINGS)
        write_json(p, {
            "ai_enabled": "no", "telegram_enabled": False, "topics": {"ביטחון": 0, "כלכלה": False, "zzz": False},
            "source_countries": "ישראל", "event_countries": ["ישראל", 5, ""], "sources": {"ynet": False, "x": 1}})
        s, ok = nb.load_settings(p)
        self.assertFalse(ok)
        self.assertTrue(s["ai_enabled"])
        self.assertFalse(s["telegram_enabled"])
        self.assertTrue(s["topics"]["ביטחון"])
        self.assertFalse(s["topics"]["כלכלה"])
        self.assertNotIn("zzz", s["topics"])
        self.assertEqual(s["source_countries"], [])
        self.assertEqual(s["event_countries"], ["ישראל"])
        self.assertEqual(s["sources"], {"ynet": False})

    def test_full_run_with_broken_settings(self):
        b = Bot(self, NOON, settings_raw="{{{ broken", ai=ai_events(("sec", 3, "IL"))).run()
        self.assertEqual(len(b.summary_calls()), 1)
        self.assertEqual(len(b.sent), 1)
        self.assertFalse(b.data("status.json")["settings_ok"])

    def test_full_run_without_settings(self):
        b = Bot(self, NOON, ai=ai_events(("sec", 2, "IL"))).run()
        self.assertTrue(b.data("status.json")["settings_ok"])


class TestDataAndCost(unittest.TestCase):
    def test_changelog_in_status(self):
        b = Bot(self, NOON).run()
        cl = b.data("status.json")["changelog"]
        self.assertEqual(cl[0]["v"], nb.VERSION, "רשומת CHANGELOG לגרסה הנוכחית חסרה")
        self.assertTrue(cl[0]["date"] and cl[0]["items"])
        self.assertLessEqual(len(cl), nb.CHANGELOG_KEEP)
        self.assertFalse(any("**" in i for c in cl for i in c["items"]))

    def test_changelog_missing_file(self):
        self.assertEqual(nb.read_changelog("/nonexistent.md"), [])

    def test_status_json(self):
        b = Bot(self, NOON, settings={"sources": {"ynet": False}}, ai=ai_events(("sec", 2, "IL"))).run()
        s = b.data("status.json")
        self.assertEqual(s["version"], nb.VERSION)
        self.assertGreater(s["day_cost"], 0)
        self.assertGreater(s["month_estimate"], 0)
        self.assertEqual(len(s["sources"]), len(SRC_BY_URL))
        self.assertFalse(next(x for x in s["sources"] if x["id"] == "ynet")["active"])
        self.assertIn("ישראל", s["source_countries"])

    def test_failed_sources_in_status(self):
        b = Bot(self, NOON)
        orig = b.fetch

        def fetch(url, timeout=20):
            if SRC_BY_URL[url]["id"] == "CNBC":
                raise OSError("down")
            return orig(url, timeout)
        b.fetch = fetch
        b.run()
        self.assertIn("CNBC", b.data("status.json")["failed_sources"])

    def test_event_record_and_prune_and_confirm(self):
        old = {"ref": "aaa111", "t": (NOON - timedelta(days=8)).isoformat(), "h": "ישן", "v": "rumor"}
        rumor = {"ref": "bbb222", "t": (NOON - timedelta(hours=1)).isoformat(), "h": "שמועה", "v": "rumor",
                 "imp": 2, "topic": "ביטחון", "ec": "ישראל"}
        b = Bot(self, NOON, ai=lambda r: {"events": [{"status": "confirmed", "ref": "bbb222", "headline": "אושר",
                                                       "summary": "ס", "verified": True, "importance": "2",
                                                       "tp": "sec", "ec": "il", "item_ids": [1, 999, "x"]}]})
        os.makedirs(os.path.join(b.dir, "docs", "data"))
        write_json(os.path.join(b.dir, "docs", "data", "events.json"), {"events": [old, rumor]})
        b.run()
        evs = b.data("events.json")["events"]
        self.assertNotIn("aaa111", [e["ref"] for e in evs], "אירוע מעל 7 ימים נמחק")
        self.assertEqual(evs[0]["v"], "confirmed")
        self.assertEqual(evs[0]["of"], "bbb222")
        self.assertEqual(evs[0]["imp"], 2)
        self.assertEqual(evs[0]["ec"], "ישראל")
        self.assertEqual(len(evs[0]["src"]), 1)
        self.assertTrue(evs[0]["src"][0]["u"].startswith("https://"))
        self.assertEqual(next(e for e in evs if e["ref"] == "bbb222")["v"], "confirmed")

    def test_unsafe_links_dropped(self):
        rec = nb.build_record({"headline": "h", "importance": 2, "item_ids": [1]},
                              [{"source": "ynet", "kind": "media", "link": "javascript:alert(1)"}], {}, "t")
        self.assertEqual(rec["src"][0]["u"], "")

    def test_garbage_ai_reply_counted_and_pause(self):
        st = {"seen": {}, "usage": {"version": nb.VERSION, "day": "2026-11-27", "health_day": "2026-11-27",
                                    "week": "2026-W48", "w3": {"start": NOON.isoformat()}, "fails": 2}}
        b = Bot(self, NOON, state=st, ai=lambda r: ("בלי JSON", 1000, 4000, "max_tokens")).run()
        u = b.state()["usage"]
        self.assertEqual(len(b.summary_calls()), 2, "ניסיון + ניסיון עם חצי מהידיעות")
        self.assertEqual(u["day_calls"], 2)
        self.assertIn("paused_until", u)
        self.assertEqual(len(b.sent), 1)
        self.assertIn("משהה", b.sent[0])

    def test_events_wrong_shape_no_crash(self):
        for reply in [{"events": "x"}, {"events": [1, None, {"importance": "abc", "tp": "sec"}]}, [1, 2]]:
            b = Bot(self, NOON, ai=lambda r, reply=reply: reply).run()
            self.assertIsNotNone(b.data("events.json"))

    def test_sent_counter_saved(self):
        """באג מ-v2: מונה ההודעות שנשלחו עודכן אחרי השמירה ולכן אבד."""
        b = Bot(self, NOON, ai=ai_events(("sec", 3, "IL"))).run()
        self.assertEqual(b.state()["usage"]["w24"]["sent"], 1)

    def test_interval_respected(self):
        b = Bot(self, NOON, ai=ai_events(("sec", 2, "IL"))).run()
        b.now = NOON + timedelta(minutes=10)
        b.run()
        self.assertEqual(len(b.summary_calls()), 1)
        b.now = NOON + timedelta(minutes=21)
        b.run()
        self.assertEqual(len(b.summary_calls()), 2)

    def test_first_run(self):
        b = Bot(self, NOON, state=False).run()
        self.assertEqual(b.ai_calls, [])
        self.assertEqual(len(b.sent), 1)
        self.assertIsNotNone(b.data("status.json"))


class TestSite(unittest.TestCase):
    PAGE = os.path.join(ROOT, "docs", "index.html")

    def page(self):
        with open(self.PAGE, encoding="utf-8") as f:
            return f.read()

    def test_csp_hash_matches_script(self):
        p = self.page()
        self.assertIn(f"'sha256-{csp_hash.script_hash(p)}'", p,
                      "ה-hash ב-CSP לא תואם לסקריפט – להריץ python3 tests/csp_hash.py")

    def test_site_version_matches_bot(self):
        m = re.search(r'var SITE_VERSION = "([^"]+)"', self.page())
        self.assertEqual(m.group(1), nb.VERSION, "לעדכן SITE_VERSION ב-docs/index.html")

    def test_token_never_stored(self):
        """החלטת תומר (v3.1): הטוקן לא נשמר בטלפון. מותר רק למחוק טוקן ישן."""
        p = self.page()
        self.assertIsNone(re.search(r'store\("gh_token",(?!\s*null\))', p), "הטוקן נשמר בטלפון")
        self.assertIsNone(re.search(r'(localStorage|sessionStorage)\.setItem\([^)]*tok', p), "הטוקן נשמר בטלפון")
        self.assertIn('store("gh_token", null)', p)

    def test_hidden_attribute_wins_over_css(self):
        """באג שנתפס ב-QA של 3.1: display:flex דרס את hidden – פס "גרסה חדשה" הופיע תמיד."""
        self.assertIn("[hidden]{display:none !important}", self.page())

    def test_token_field_uses_keychain_autofill(self):
        """v3.2.1: טופס עם username + current-password כדי שמחזיק המפתחות יציע לשמור/למלא."""
        p = self.page()
        self.assertIn('<form id="saveform" autocomplete="on">', p)
        self.assertRegex(p, r'<input id="user" name="username" type="text" autocomplete="username"')
        self.assertRegex(p, r'<input id="tok" name="password" type="password" autocomplete="current-password"')
        self.assertIn('id="save" type="submit"', p)
        self.assertIn("ev.preventDefault()", p)

    def test_noindex_and_no_secrets(self):
        p = self.page()
        self.assertIn('<meta name="robots" content="noindex, nofollow">', p)
        self.assertNotRegex(p, r"(github_pat_[A-Za-z0-9]|ghp_[A-Za-z0-9]|sk-ant-)")
        self.assertNotIn("innerHTML", p)

    @unittest.skipUnless(shutil.which("node"), "node לא מותקן")
    def test_js_syntax(self):
        js = re.search(r"<script>(.*?)</script>", self.page(), re.S).group(1)
        f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
        f.write(js)
        f.close()
        self.addCleanup(os.unlink, f.name)
        r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestIcons(unittest.TestCase):
    DOCS = os.path.join(ROOT, "docs")

    @staticmethod
    def png_size(path):
        with open(path, "rb") as f:
            head = f.read(24)
        assert head[:8] == b"\x89PNG\r\n\x1a\n", path
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")

    def test_png_icons_exist_with_right_size(self):
        for name, size in [("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512),
                           ("favicon-32.png", 32)]:
            self.assertEqual(self.png_size(os.path.join(self.DOCS, "icons", name)), (size, size), name)

    def test_manifest_valid_and_files_exist(self):
        with open(os.path.join(self.DOCS, "manifest.webmanifest"), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["display"], "standalone")
        self.assertTrue(m["short_name"])
        for icon in m["icons"]:
            self.assertTrue(os.path.exists(os.path.join(self.DOCS, icon["src"])), icon["src"])

    def test_page_links_icons_and_csp_allows_manifest(self):
        with open(os.path.join(self.DOCS, "index.html"), encoding="utf-8") as f:
            page = f.read()
        for href in re.findall(r'<link rel="(?:icon|apple-touch-icon|manifest)" href="([^"]+)"', page):
            self.assertTrue(os.path.exists(os.path.join(self.DOCS, href)), href)
        self.assertIn('rel="apple-touch-icon"', page)
        self.assertIn("manifest-src 'self'", page)


if __name__ == "__main__":
    unittest.main()
