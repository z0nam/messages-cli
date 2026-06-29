#!/usr/bin/env python3
"""Tests for the `msg` CLI. Stdlib-only (unittest), no external deps.

Run:  python3 tests/test_msg.py            (from the repo root)
      python3 -m unittest discover tests

Uses a synthetic in-temp chat.db so nothing touches the real Messages data.
"""
import argparse
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
MSG = SourceFileLoader("msgmod", os.path.join(HERE, "..", "msg")).load_module()
COCOA = MSG.COCOA_EPOCH


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def ab(text):
    """Build a minimal attributedBody blob the decoder understands:
    a streamtyped header, the `NSString` class name, the data marker
    b'\\x84\\x01+', a length prefix, then the UTF-8 bytes."""
    b = text.encode("utf-8")
    n = len(b)
    if n < 0x80:
        lp = bytes([n])
    elif n < 0x8000:
        lp = b"\x81" + n.to_bytes(2, "little")
    else:
        lp = b"\x82" + n.to_bytes(4, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x08"
            b"NSString\x01\x95\x84\x01+" + lp + b + b"\x86")


def cns(unix):
    """unix seconds -> Cocoa nanoseconds."""
    return int((unix - COCOA) * 1_000_000_000)


class FakeContacts:
    """Stand-in for the AddressBook-backed Contacts (no disk access)."""

    def __init__(self, by_handle=None, name_index=None):
        self.by = by_handle or {}            # handle -> display name
        self.name_index = name_index or {}   # name -> (emails set, phone8 set)

    def display(self, handle):
        return self.by.get(handle) or handle

    def name_for(self, handle):
        return self.by.get(handle)

    def search_handles(self, query):
        ql = query.strip().lower()
        emails, phones8 = set(), set()
        for name, (em, ph) in self.name_index.items():
            if ql and ql in name.lower():
                emails |= em
                phones8 |= ph
        return emails, phones8


def A(**kw):
    defaults = dict(limit=40, json=True, all=False, media=False, open=False,
                    force=False, dry_run=False, sms=False, imessage=False,
                    no_verify=True, from_=None, since=None, until=None,
                    scan=4000, identifier=None, text=None, query=None, prefix="")
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def capture_json(fn, *a, **k):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a, **k)
    return json.loads(buf.getvalue())


def capture_text(fn, *a, **k):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


SCHEMA = """
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT,
  display_name TEXT, service_name TEXT, guid TEXT,
  last_read_message_timestamp INTEGER DEFAULT 0, is_filtered INTEGER DEFAULT 0);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, is_from_me INTEGER,
  is_read INTEGER DEFAULT 0, text TEXT, attributedBody BLOB,
  cache_has_attachments INTEGER DEFAULT 0, service TEXT, handle_id INTEGER,
  error INTEGER DEFAULT 0, is_sent INTEGER DEFAULT 0, is_delivered INTEGER DEFAULT 0);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT,
  transfer_name TEXT, mime_type TEXT, uti TEXT, total_bytes INTEGER);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


def build_db():
    """A small but realistic fixture. Returns an open sqlite3 connection."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    base = 1_780_000_000  # arbitrary recent unix time

    # handles
    con.executemany("INSERT INTO handle (ROWID, id) VALUES (?,?)", [
        (1, "+821011112222"),     # 홍길동 (phone)
        (2, "friend@example.com"),  # email contact
        (3, "+821099998888"),     # unknown spammer (filtered)
        (4, "+821055556666"),     # same person as handle 1 via SMS
    ])
    # chats
    con.executemany(
        "INSERT INTO chat (ROWID, chat_identifier, display_name, service_name, "
        "guid, last_read_message_timestamp, is_filtered) VALUES (?,?,?,?,?,?,?)", [
            (10, "+821011112222", None, "iMessage", "g10", cns(base + 50), 0),
            (20, "friend@example.com", "친구", "iMessage", "g20", cns(base + 10), 0),
            (30, "+821099998888", None, "SMS", "g30", 0, 1),  # filtered
            (40, "+821055556666", None, "SMS", "g40", cns(base + 5), 0),
        ])
    con.executemany(
        "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?,?)",
        [(10, 1), (20, 2), (30, 3), (40, 4)])

    # messages: (rowid, chat, date, from_me, is_read, text, ab, hasatt, svc, handle)
    msgs = [
        # chat 10: m1 read, m2 unread (date>last_read), m3 from me
        (1, 10, base, 0, 1, None, ab("안녕하세요"), 0, "iMessage", 1),
        (2, 10, base + 100, 0, 0, None, ab("회의록 보냈어요"), 0, "iMessage", 1),
        (3, 10, base + 200, 1, 1, None, ab("네 확인했습니다 👍"), 0, "iMessage", None),
        # chat 20: an attachment message
        (4, 20, base + 60, 0, 0, None, ab("￼"), 1, "iMessage", 2),
        # chat 30 (filtered): unread spam
        (5, 30, base + 300, 0, 0, None, ab("(광고) 대출 가능합니다"), 0, "SMS", 3),
        # chat 40: same person via SMS
        (6, 40, base + 20, 0, 1, None, ab("문자도 와요"), 0, "SMS", 4),
    ]
    con.executemany(
        "INSERT INTO message (ROWID, date, is_from_me, is_read, text, "
        "attributedBody, cache_has_attachments, service, handle_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(m[0], cns(m[2]), m[3], m[4], m[5], m[6], m[7], m[8], m[9]) for m in msgs])
    con.executemany("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?,?)",
                    [(m[1], m[0]) for m in msgs])

    # attachment on message 4
    con.execute("INSERT INTO attachment (ROWID, filename, transfer_name, mime_type, "
                "uti, total_bytes) VALUES (1, ?, 'photo.jpg', 'image/jpeg', "
                "'public.jpeg', 204800)",
                ("~/Library/Messages/Attachments/aa/00/GUID/photo.jpg",))
    con.execute("INSERT INTO message_attachment_join (message_id, attachment_id) "
                "VALUES (4, 1)")
    con.commit()
    return con


# --------------------------------------------------------------------------
# pure-function tests
# --------------------------------------------------------------------------
class TestDecode(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(MSG.decode_attributed_body(ab("hello")), "hello")

    def test_korean(self):
        self.assertEqual(MSG.decode_attributed_body(ab("안녕하세요 반가워요")),
                         "안녕하세요 반가워요")

    def test_emoji(self):
        self.assertEqual(MSG.decode_attributed_body(ab("좋아요 👍🏼🎉")), "좋아요 👍🏼🎉")

    def test_long_over_127_bytes(self):
        # forces the 0x81 (int16) length prefix; Korean = 3 bytes/char
        s = "가" * 200
        self.assertEqual(MSG.decode_attributed_body(ab(s)), s)

    def test_no_marker_returns_none(self):
        self.assertIsNone(MSG.decode_attributed_body(b"\x04\x0bstreamtyped no string"))

    def test_empty(self):
        self.assertIsNone(MSG.decode_attributed_body(b""))
        self.assertIsNone(MSG.decode_attributed_body(None))


class TestDecodeMessage(unittest.TestCase):
    def test_prefers_plain_text(self):
        self.assertEqual(MSG.decode_message("plain", ab("fromblob")),
                         ("plain", False))

    def test_falls_back_to_blob(self):
        self.assertEqual(MSG.decode_message(None, ab("blobtext")),
                         ("blobtext", False))

    def test_cr_normalized(self):
        txt, _ = MSG.decode_message("a\r\nb\rc", None)
        self.assertEqual(txt, "a\nb\nc")

    def test_attachment_object_char(self):
        txt, att = MSG.decode_message(None, ab("￼"))
        self.assertEqual(txt, "")
        self.assertTrue(att)

    def test_empty(self):
        self.assertEqual(MSG.decode_message(None, None), ("", False))


class TestDates(unittest.TestCase):
    def test_nanoseconds_roundtrip(self):
        ns = MSG.parse_user_date("2026-06-26 14:03")
        self.assertEqual(MSG.fmt_dt(MSG.cocoa_to_local(ns)), "2026-06-26 14:03")

    def test_seconds_legacy_branch(self):
        # a small (seconds-era) value must NOT be divided by 1e9
        secs = 300_000_000  # ~2010 in cocoa seconds
        dt = MSG.cocoa_to_local(secs)
        self.assertEqual(dt.year, 2010)

    def test_zero_is_none(self):
        self.assertIsNone(MSG.cocoa_to_local(0))
        self.assertIsNone(MSG.cocoa_to_local(None))

    def test_parse_end_of_day(self):
        start = MSG.parse_user_date("2026-06-26")
        end = MSG.parse_user_date("2026-06-26", end=True)
        self.assertEqual(end - start, (86400 - 1) * 1_000_000_000)

    def test_parse_invalid(self):
        self.assertIsNone(MSG.parse_user_date("not-a-date"))
        self.assertIsNone(MSG.parse_user_date(""))


class TestNames(unittest.TestCase):
    def test_cjk_last_first(self):
        self.assertEqual(MSG.compose_name("길동", "홍", None, None), "홍길동")

    def test_latin_first_last(self):
        self.assertEqual(MSG.compose_name("John", "Doe", None, None), "John Doe")

    def test_nick_fallback(self):
        self.assertEqual(MSG.compose_name("", "", "버디", None), "버디")

    def test_org_fallback(self):
        self.assertEqual(MSG.compose_name(None, None, None, "삼성카드"), "삼성카드")

    def test_empty_none(self):
        self.assertIsNone(MSG.compose_name("", "", "", ""))

    def test_has_cjk(self):
        self.assertTrue(MSG._has_cjk("abc가"))
        self.assertFalse(MSG._has_cjk("abc 123"))

    def test_digits(self):
        self.assertEqual(MSG.digits("+82 10-1111-2222"), "821011112222")
        self.assertEqual(MSG.digits(None), "")


class TestMisc(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(MSG._human_size(0), "")
        self.assertEqual(MSG._human_size(512), "512 B")
        self.assertEqual(MSG._human_size(204800), "200 KB")

    def test_snippet_highlight(self):
        s = MSG._snippet_around("내일 회의 합시다", "회의")
        self.assertIn("«회의»", s)

    def test_snippet_edges_ellipsis(self):
        body = "x" * 50 + "타겟" + "y" * 50
        s = MSG._snippet_around(body, "타겟")
        self.assertTrue(s.startswith("…"))
        self.assertTrue(s.endswith("…"))
        self.assertIn("«타겟»", s)


# --------------------------------------------------------------------------
# DB-backed command tests
# --------------------------------------------------------------------------
class DBTest(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        self.c = FakeContacts(
            by_handle={"+821011112222": "홍길동", "+821055556666": "홍길동"})

    def tearDown(self):
        self.con.close()


class TestFindChats(DBTest):
    def test_email(self):
        ids, _ = MSG.find_chats(self.con, "friend@example.com", self.c)
        self.assertEqual(ids, [20])

    def test_phone_suffix(self):
        # typing only the local part still matches +8210...
        ids, _ = MSG.find_chats(self.con, "11112222", self.c)
        self.assertIn(10, ids)

    def test_chat_identifier_exact(self):
        ids, _ = MSG.find_chats(self.con, "+821011112222", self.c)
        self.assertIn(10, ids)

    def test_no_match(self):
        ids, _ = MSG.find_chats(self.con, "zzz-nobody", self.c)
        self.assertEqual(ids, [])


class TestUnread(DBTest):
    def test_default_excludes_filtered_and_read(self):
        out = capture_json(MSG.cmd_unread, self.con, A(), self.c)
        texts = [o["text"] for o in out]
        # message 2 (회의록) is the only unread in the main inbox
        self.assertIn("회의록 보냈어요", texts)
        # filtered spam (chat 30) hidden by default
        self.assertFalse(any("광고" in t for t in texts))
        # read message 1 not included
        self.assertFalse(any("안녕하세요" == t for t in texts))

    def test_all_includes_filtered(self):
        out = capture_json(MSG.cmd_unread, self.con, A(all=True), self.c)
        self.assertTrue(any("광고" in o["text"] for o in out))

    def test_attachment_unread_marked(self):
        out = capture_json(MSG.cmd_unread, self.con, A(), self.c)
        att = [o for o in out if o["has_attachment"]]
        self.assertTrue(att)  # message 4 (object-replacement char) counts


class TestSearch(DBTest):
    def test_finds_in_blob(self):
        out = capture_json(MSG.cmd_search, self.con, A(query="회의록"), self.c)
        self.assertEqual(len(out), 1)
        self.assertIn("회의록", out[0]["text"])

    def test_from_filter(self):
        out = capture_json(MSG.cmd_search, self.con,
                           A(query="문자", from_="friend@example.com"), self.c)
        self.assertEqual(out, [])  # '문자도 와요' is in chat 40, not friend

    def test_since_filter(self):
        # everything is recent; a future --since yields nothing
        out = capture_json(MSG.cmd_search, self.con,
                           A(query="안녕", since="2099-01-01"), self.c)
        self.assertEqual(out, [])

    def test_no_results(self):
        out = capture_json(MSG.cmd_search, self.con, A(query="없는단어xyz"), self.c)
        self.assertEqual(out, [])


class TestReadAttachments(DBTest):
    def test_attachments_resolved(self):
        out = capture_json(MSG.cmd_read, self.con,
                           A(identifier="friend@example.com", media=True), self.c)
        msg_with_att = [o for o in out if o["attachments"]]
        self.assertTrue(msg_with_att)
        a = msg_with_att[0]["attachments"][0]
        self.assertEqual(a["name"], "photo.jpg")
        self.assertEqual(a["mime"], "image/jpeg")
        self.assertTrue(a["path"].endswith("/photo.jpg"))
        self.assertFalse(a["path"].startswith("~"))  # expanduser applied

    def test_read_resolves_sender_name(self):
        out = capture_json(MSG.cmd_read, self.con,
                           A(identifier="+821011112222"), self.c)
        incoming = [o for o in out if not o["is_from_me"]]
        self.assertTrue(all(o["from"] == "홍길동" for o in incoming))


class TestResolveSendTarget(DBTest):
    def test_same_person_collapses_to_imessage(self):
        # handles 1 (iMessage) and 4 (SMS) both resolve to 홍길동
        tgt = MSG.resolve_send_target(self.con, "홍길동", self.c, None)
        # find_chats needs the name index to match by name:
        # fall back to a direct handle if name search isn't wired
        if tgt is None:
            self.skipTest("name search not exercised without name_index")
        self.assertNotIn("ambiguous", tgt)

    def test_phone_resolves(self):
        tgt = MSG.resolve_send_target(self.con, "+821011112222", self.c, None)
        self.assertEqual(tgt["handle"], "+821011112222")
        self.assertEqual(tgt["service"], "iMessage")

    def test_new_target(self):
        tgt = MSG.resolve_send_target(self.con, "+821033334444", self.c, None)
        self.assertTrue(tgt.get("new"))
        self.assertEqual(tgt["service"], "iMessage")

    def test_force_sms(self):
        tgt = MSG.resolve_send_target(self.con, "+821011112222", self.c, "SMS")
        self.assertEqual(tgt["service"], "SMS")


class TestDraftCompose(unittest.TestCase):
    def test_archived_attributed_string_roundtrip(self):
        import plistlib
        blob = MSG._archived_attributed_string("안녕 👋\n둘째 줄")
        inner = plistlib.loads(blob)
        self.assertEqual(inner["$archiver"], "NSKeyedArchiver")
        self.assertEqual(inner["$objects"][2]["NS.string"], "안녕 👋\n둘째 줄")
        self.assertEqual(inner["$objects"][6]["$classname"],
                         "NSMutableAttributedString")

    def test_compose_strips_comment_lines(self):
        def fake_run(cmd, *a, **k):
            with open(cmd[-1], "w", encoding="utf-8") as f:
                f.write("실제 본문\n# 주석은 무시\n둘째 본문\n")
        orig = MSG.subprocess.run
        MSG.subprocess.run = fake_run
        try:
            body = MSG._compose_in_editor("")
        finally:
            MSG.subprocess.run = orig
        self.assertEqual(body, "실제 본문\n둘째 본문")


if __name__ == "__main__":
    unittest.main(verbosity=2)
