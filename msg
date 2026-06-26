#!/usr/bin/env python3
"""msg — read macOS Messages (메시지) conversations, read-only.

Subcommands:
  msg threads [--limit N] [--json]            최근 대화 스레드 목록
  msg read <identifier> [--limit N] [--json]  특정 스레드의 메시지

Reads ~/Library/Messages/chat.db. Requires Full Disk Access for the terminal.
The live DB (+ -wal/-shm sidecars) is copied to a temp dir and opened
read-only/immutable; the originals are never touched.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

COCOA_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01 UTC
DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")

# Make stdout UTF-8 regardless of locale so Korean never mojibakes.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------
# DB access (read-only, on a temp copy of chat.db + sidecars)
# --------------------------------------------------------------------------
def open_db():
    """Copy chat.db (+ -wal/-shm) to a temp dir and open it read-only.

    Returns (connection, tempdir). Caller cleans up tempdir.
    Exits with a friendly message on permission / missing-file errors.
    """
    if not os.path.exists(DB_PATH):
        _die(
            f"chat.db를 찾을 수 없습니다: {DB_PATH}\n"
            "이 Mac에서 메시지 앱을 한 번이라도 실행한 적이 있는지 확인하세요."
        )
    tmp = tempfile.mkdtemp(prefix="msg-")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = DB_PATH + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "chat.db" + suffix))
    except PermissionError:
        shutil.rmtree(tmp, ignore_errors=True)
        _die_no_access()
    copy = os.path.join(tmp, "chat.db")
    try:
        # IMPORTANT: open read-only but NOT immutable. immutable=1 makes SQLite
        # ignore the -wal sidecar, so the newest messages (which often live only
        # in the WAL until macOS checkpoints them into the main file) would be
        # invisible. We copied all 3 files, so mode=ro reads the WAL and sees
        # the latest. The user's real DB is never opened — only file-copied.
        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        _die(f"chat.db를 열지 못했습니다: {e}")
    con.row_factory = sqlite3.Row
    return con, tmp


def _die_no_access():
    _die(
        "chat.db를 읽을 권한이 없습니다 (Full Disk Access 필요).\n"
        "System Settings > Privacy & Security > Full Disk Access 에서\n"
        "이 명령을 실행하는 터미널 앱(예: Ghostty, Terminal, iTerm)을 추가/활성화한 뒤\n"
        "그 앱을 완전히 종료했다가 다시 실행하세요."
    )


def _die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# Field decoding
# --------------------------------------------------------------------------
def cocoa_to_local(date):
    """Apple Cocoa timestamp -> local datetime. Handles ns and (legacy) s."""
    if not date:
        return None
    # Modern rows are nanoseconds (~8e17); very old rows can be seconds.
    unix = (date / 1e9 if abs(date) > 1e11 else float(date)) + COCOA_EPOCH
    try:
        return datetime.fromtimestamp(unix)
    except (OverflowError, OSError, ValueError):
        return None


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "????-??-?? ??:??"


def decode_attributed_body(blob):
    """Extract the message string from an NSAttributedString typedstream blob.

    Dependency-free: locate the NSString UTF-8 run (type tag '+', 0x2B),
    read its length prefix, then slice BYTES before decoding so multibyte
    (Korean/emoji) text is never split. Returns None if not found.
    """
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i == -1:
        return None
    # The string run starts at the data marker + UTF-8 tag: b'\x84\x01+'.
    j = blob.find(b"\x84\x01+", i)
    if j == -1:
        return None
    p = j + 3  # now at the length prefix
    if p >= len(blob):
        return None
    b0 = blob[p]
    if b0 == 0x81:
        length = int.from_bytes(blob[p + 1:p + 3], "little", signed=True)
        p += 3
    elif b0 == 0x82:
        length = int.from_bytes(blob[p + 1:p + 5], "little", signed=True)
        p += 5
    elif b0 == 0x83:
        length = int.from_bytes(blob[p + 1:p + 9], "little", signed=True)
        p += 9
    elif b0 < 0x80:
        length = b0
        p += 1
    else:
        return None
    if length < 0 or p + length > len(blob):
        return None
    return blob[p:p + length].decode("utf-8", "replace")


def decode_message(text, attributed_body):
    """Return (clean_text, has_inline_attachment).

    Prefer plain text; fall back to attributedBody. Strips placeholder chars.
    U+FFFC (object replacement) means an attachment is inlined even when the
    cache_has_attachments flag is 0, so report it separately.
    """
    s = text if text else decode_attributed_body(attributed_body)
    if not s:
        return "", False
    had_attachment = "￼" in s
    # U+FFFC object-replacement (attachments), U+FFFE/FEFF noise -> drop.
    s = s.replace("￼", "").replace("￾", "").replace("﻿", "")
    # Normalize line breaks: bare \r returns the cursor to column 0 and
    # overwrites the line in a terminal (garbled output). Fold CRLF/CR -> LF.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip(), had_attachment


def message_text(text, attributed_body):
    """Convenience: just the clean text (used by snippets/audits)."""
    return decode_message(text, attributed_body)[0]


def digits(s):
    return re.sub(r"\D", "", s or "")


def _has_cjk(s):
    return any(
        "぀" <= ch <= "鿿" or "가" <= ch <= "힣"
        or "豈" <= ch <= "﫿"
        for ch in s
    )


def compose_name(first, last, nick, org):
    """Build a display name the way macOS shows it (CJK = last+first)."""
    first = (first or "").strip()
    last = (last or "").strip()
    if first or last:
        if _has_cjk(first + last):
            return (last + first) if (last and first) else (last or first)
        return (first + " " + last).strip()
    for alt in (nick, org):
        if alt and alt.strip():
            return alt.strip()
    return None


# --------------------------------------------------------------------------
# Contacts (macOS AddressBook) — resolve handle <-> name, read-only
# --------------------------------------------------------------------------
import glob  # noqa: E402  (kept local to the contacts feature)

ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb"
)


class Contacts:
    """Loads AddressBook DBs and maps phone/email handles to display names.

    Phone matching uses the last 8 digits (country-code agnostic), so
    +8210-9785-2884 and 010-9785-2884 resolve to the same person.
    Degrades gracefully (empty) if AddressBook is unreadable.
    """

    def __init__(self):
        self.by_email = {}        # lower email -> name
        self.by_phone8 = {}       # last-8 digits -> name
        self.name_handles = []    # [(name, {emails}, {phone8})] for search
        self._load()

    def _load(self):
        seen = set()
        paths = [p for p in glob.glob(ADDRESSBOOK_GLOB, recursive=True)
                 if os.path.exists(p)]
        # main DB (no Sources/) too
        main = os.path.expanduser(
            "~/Library/Application Support/AddressBook/AddressBook-v22.abcddb")
        if main not in paths and os.path.exists(main):
            paths.append(main)
        for db in paths:
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            except sqlite3.OperationalError:
                continue
            try:
                self._load_one(con, seen)
            except sqlite3.Error:
                pass
            finally:
                con.close()

    def _load_one(self, con, seen):
        names = {}  # Z_PK -> composed name
        for pk, fn, ln, nick, org in con.execute(
            "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION "
            "FROM ZABCDRECORD"
        ):
            nm = compose_name(fn, ln, nick, org)
            if nm:
                names[pk] = nm
        rec_emails = {}
        rec_phones = {}
        for addr, owner in con.execute(
            "SELECT ZADDRESS, ZOWNER FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"
        ):
            nm = names.get(owner)
            if not nm:
                continue
            e = addr.strip().lower()
            self.by_email.setdefault(e, nm)
            rec_emails.setdefault(owner, set()).add(e)
        for num, owner in con.execute(
            "SELECT ZFULLNUMBER, ZOWNER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"
        ):
            nm = names.get(owner)
            if not nm:
                continue
            d = digits(num)
            if not d:
                continue
            key = d[-8:]
            self.by_phone8.setdefault(key, nm)
            rec_phones.setdefault(owner, set()).add(key)
        # build per-record search rows (dedupe identical name+handles)
        for pk, nm in names.items():
            em = rec_emails.get(pk, set())
            ph = rec_phones.get(pk, set())
            if not em and not ph:
                continue
            sig = (nm, frozenset(em), frozenset(ph))
            if sig in seen:
                continue
            seen.add(sig)
            self.name_handles.append((nm, em, ph))

    def name_for(self, handle):
        """Resolve a chat.db handle id to a contact name, or None."""
        if not handle:
            return None
        if "@" in handle:
            return self.by_email.get(handle.strip().lower())
        d = digits(handle)
        if len(d) >= 8:
            return self.by_phone8.get(d[-8:])
        return self.by_phone8.get(d) if d else None

    def display(self, handle):
        """Contact name if known, else the raw handle."""
        return self.name_for(handle) or handle

    def search_handles(self, query):
        """Names containing query -> (emails set, phone8 set) to match chats."""
        ql = query.strip().lower()
        emails, phones8 = set(), set()
        for nm, em, ph in self.name_handles:
            if ql in nm.lower():
                emails |= em
                phones8 |= ph
        return emails, phones8


# --------------------------------------------------------------------------
# threads
# --------------------------------------------------------------------------
def cmd_threads(con, args, contacts):
    rows = con.execute(
        """
        SELECT c.ROWID            AS chat_id,
               c.chat_identifier  AS chat_identifier,
               c.display_name     AS display_name,
               c.service_name     AS service_name,
               m.date             AS date,
               m.text             AS text,
               m.attributedBody   AS ab,
               m.is_from_me       AS is_from_me,
               m.cache_has_attachments AS has_att
        FROM chat c
        JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        JOIN message m            ON m.ROWID = cmj.message_id
        JOIN (
            SELECT cmj2.chat_id AS cid, MAX(m2.date) AS md
            FROM chat_message_join cmj2
            JOIN message m2 ON m2.ROWID = cmj2.message_id
            GROUP BY cmj2.chat_id
        ) last ON last.cid = c.ROWID AND last.md = m.date
        GROUP BY c.ROWID
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    # participants per shown chat
    part = {}
    for r in rows:
        ph = con.execute(
            """SELECT h.id FROM chat_handle_join chj
               JOIN handle h ON h.ROWID = chj.handle_id
               WHERE chj.chat_id = ?""",
            (r["chat_id"],),
        ).fetchall()
        part[r["chat_id"]] = [p["id"] for p in ph]

    out = []
    for r in rows:
        participants = part[r["chat_id"]]
        name = r["display_name"] or _thread_label(
            r["chat_identifier"], participants, contacts)
        snippet, inline_att = decode_message(r["text"], r["ab"])
        if not snippet and (r["has_att"] or inline_att):
            snippet = "[첨부]"
        out.append({
            "name": name,
            "chat_identifier": r["chat_identifier"],
            "display_name": r["display_name"] or "",
            "participants": participants,
            "participant_names": [contacts.display(p) for p in participants],
            "service": r["service_name"] or "",
            "is_group": len(participants) > 1,
            "last_date": fmt_dt(cocoa_to_local(r["date"])),
            "from": "나" if r["is_from_me"] else _sender_short(participants, contacts),
            "snippet": snippet,
            "_name": name,
        })

    if args.json:
        for o in out:
            o.pop("_name", None)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    for o in out:
        tag = ""
        if o["service"] and o["service"] != "iMessage":
            tag = f" ({o['service']})"
        grp = " 〈그룹〉" if o["is_group"] else ""
        head = f"[{o['last_date']}] {o['_name']}{grp}{tag}"
        snip = o["snippet"].replace("\n", " ⏎ ")
        if len(snip) > 70:
            snip = snip[:70] + "…"
        who = o["from"]
        print(head)
        print(f"    └ {who}: {snip}" if snip else f"    └ {who}:")


def _thread_label(chat_identifier, participants, contacts):
    disp = [contacts.display(p) for p in participants]
    if len(disp) == 1:
        return disp[0]
    if len(disp) > 1:
        return ", ".join(disp[:3]) + (" 외" if len(disp) > 3 else "")
    return contacts.display(chat_identifier) if chat_identifier else "(알 수 없음)"


def _sender_short(participants, contacts):
    if len(participants) == 1:
        return contacts.display(participants[0])
    return "상대"


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------
def find_chats(con, identifier, contacts):
    """Return list of chat ROWIDs matching identifier (loose).

    Matches by: exact guid/chat_identifier, phone last-8 suffix, substring on
    chat_identifier/display_name/handle, AND contact name (via AddressBook) —
    so `msg read 홍길동` and `msg read 821012345678` both resolve.
    """
    chats = con.execute(
        """SELECT c.ROWID AS chat_id, c.chat_identifier, c.display_name, c.guid
           FROM chat c"""
    ).fetchall()
    # participants per chat
    parts = {}
    for cid, hid in con.execute(
        """SELECT chj.chat_id, h.id FROM chat_handle_join chj
           JOIN handle h ON h.ROWID = chj.handle_id"""
    ):
        parts.setdefault(cid, []).append(hid)

    q = identifier.strip()
    qd = digits(q)
    ql = q.lower()
    # handles belonging to contacts whose NAME matches the query
    name_emails, name_phones8 = contacts.search_handles(q)

    matches = []
    for c in chats:
        cid = c["chat_id"]
        ci = c["chat_identifier"] or ""
        dn = c["display_name"] or ""
        guid = c["guid"] or ""
        plist = parts.get(cid, [])

        # exact guid / chat_identifier
        if q == guid or q == ci:
            matches.append(cid)
            continue
        # phone suffix match (last 8 digits) against identifier or participants
        if qd and len(qd) >= 7:
            suf = qd[-8:]
            hay = [digits(ci)] + [digits(p) for p in plist]
            if any(h and (h.endswith(suf) or suf.endswith(h[-8:] if len(h) >= 8 else h)) for h in hay):
                matches.append(cid)
                continue
        # contact-name match: any participant handle belongs to that contact
        if name_emails or name_phones8:
            hay_e = [p.lower() for p in plist if "@" in p] + [ci.lower()]
            hay_p = [digits(p)[-8:] for p in plist if digits(p)] + ([digits(ci)[-8:]] if digits(ci) else [])
            if any(e in name_emails for e in hay_e) or any(p in name_phones8 for p in hay_p):
                matches.append(cid)
                continue
        # substring match on names / handles (email, display name)
        if ql and (ql in ci.lower() or ql in dn.lower()
                   or any(ql in p.lower() for p in plist)
                   or any(ql in contacts.display(p).lower() for p in plist)):
            matches.append(cid)
            continue
    return matches, parts


def cmd_read(con, args, contacts):
    chat_ids, parts = find_chats(con, args.identifier, contacts)
    if not chat_ids:
        _die(f"'{args.identifier}'에 해당하는 대화를 찾지 못했습니다. "
             f"`msg threads`로 식별자를 확인하세요.")

    placeholders = ",".join("?" * len(chat_ids))
    rows = con.execute(
        f"""
        SELECT m.date AS date, m.is_from_me AS is_from_me,
               m.text AS text, m.attributedBody AS ab,
               m.cache_has_attachments AS has_att,
               m.service AS service, h.id AS handle
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id IN ({placeholders})
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (*chat_ids, args.limit),
    ).fetchall()
    rows = list(reversed(rows))  # chronological

    out = []
    for r in rows:
        txt, inline_att = decode_message(r["text"], r["ab"])
        handle = r["handle"] or ""
        out.append({
            "date": fmt_dt(cocoa_to_local(r["date"])),
            "from": "나" if r["is_from_me"] else (contacts.display(handle) or "상대"),
            "handle": "" if r["is_from_me"] else handle,
            "contact": "" if r["is_from_me"] else (contacts.name_for(handle) or ""),
            "is_from_me": bool(r["is_from_me"]),
            "service": r["service"] or "",
            "text": txt,
            "has_attachment": bool(r["has_att"]) or inline_att,
        })

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    for o in out:
        tag = ""
        if o["service"] and o["service"] != "iMessage":
            tag = f" ({o['service']})"
        body = o["text"]
        if o["has_attachment"]:
            body = ("[첨부] " + body).strip()
        print(f"[{o['date']}] {o['from']}{tag}: {body}")


# --------------------------------------------------------------------------
# unread (incoming messages not yet read, grouped by thread)
# --------------------------------------------------------------------------
def cmd_unread(con, args, contacts):
    # Match the Messages.app blue-dot badge: an incoming message that is both
    # flagged unread AND newer than the chat's last-read timestamp. is_read
    # alone is stale on old rows; the timestamp guard fixes that.
    # By default restrict to chat.is_filtered = 0 (the MAIN inbox). Filtered
    # folders — 1 = Unknown Senders, 2 = promotions/transactions — don't show a
    # badge in the main conversation list, so we hide them unless --all.
    filter_clause = "" if args.all else "AND c.is_filtered = 0"
    rows = con.execute(
        f"""
        SELECT m.date AS date, m.text AS text, m.attributedBody AS ab,
               m.cache_has_attachments AS has_att, m.service AS service,
               h.id AS handle, c.ROWID AS chat_id,
               c.chat_identifier AS ci, c.display_name AS dn
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.is_from_me = 0 AND m.is_read = 0
          AND m.date > c.last_read_message_timestamp
          {filter_clause}
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    # count what's hidden in filtered folders, to show a hint
    hidden = 0
    if not args.all:
        hidden = con.execute(
            """SELECT COUNT(*) FROM message m
               JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
               JOIN chat c ON c.ROWID = cmj.chat_id
               WHERE m.is_from_me = 0 AND m.is_read = 0
                 AND m.date > c.last_read_message_timestamp
                 AND c.is_filtered != 0"""
        ).fetchone()[0]

    # group by thread, newest thread first, chronological within thread
    groups = {}
    order = []
    for r in rows:
        if r["chat_id"] not in groups:
            groups[r["chat_id"]] = []
            order.append(r["chat_id"])
        groups[r["chat_id"]].append(r)

    flat = []
    for cid in order:
        msgs = list(reversed(groups[cid]))  # chronological
        first = msgs[0]
        label = first["dn"] or contacts.display(first["handle"] or first["ci"] or "")
        for r in msgs:
            txt, inline_att = decode_message(r["text"], r["ab"])
            handle = r["handle"] or ""
            flat.append({
                "thread": label,
                "chat_identifier": first["ci"] or "",
                "date": fmt_dt(cocoa_to_local(r["date"])),
                "from": contacts.display(handle) or "상대",
                "handle": handle,
                "contact": contacts.name_for(handle) or "",
                "service": r["service"] or "",
                "text": txt,
                "has_attachment": bool(r["has_att"]) or inline_att,
            })

    if args.json:
        print(json.dumps(flat, ensure_ascii=False, indent=2))
        return

    def filtered_hint():
        if hidden:
            print(f"(필터됨/알 수 없는 발신자에 {hidden}건 더 있음 — `msg unread --all`로 보기)")

    if not flat:
        print("안 읽은 메시지가 없습니다. 👍")
        filtered_hint()
        return

    scope = " (전체: 필터 폴더 포함)" if args.all else ""
    print(f"안 읽은 메시지 {len(flat)}개 · 대화 {len(order)}개{scope}\n")
    for cid in order:
        msgs = list(reversed(groups[cid]))
        first = msgs[0]
        label = first["dn"] or contacts.display(first["handle"] or first["ci"] or "")
        svc = first["service"] or ""
        tag = f" ({svc})" if svc and svc != "iMessage" else ""
        print(f"== {label}{tag} · {len(msgs)}개 ==")
        for r in msgs:
            txt, inline_att = decode_message(r["text"], r["ab"])
            body = txt
            if (r["has_att"] or inline_att):
                body = ("[첨부] " + body).strip()
            elif not body:
                body = "[내용 없음]"
            body = body.replace("\n", " ⏎ ")
            if len(body) > 70:
                body = body[:70] + "…"
            print(f"  [{fmt_dt(cocoa_to_local(r['date']))}] {body}")
        print()
    filtered_hint()


# --------------------------------------------------------------------------
# send / reply (OUTWARD — drives Messages.app via osascript)
#
# This is the only place the tool reaches outside read-only territory. It never
# writes chat.db; it asks Messages.app to send, then re-reads chat.db to confirm.
# --------------------------------------------------------------------------
_SEND_APPLESCRIPT = r'''on run argv
  set theHandle to item 1 of argv
  set theText to item 2 of argv
  set theType to item 3 of argv
  tell application "Messages"
    set theService to missing value
    repeat with s in services
      try
        if (service type of s as text) is theType then set theService to s
      end try
    end repeat
    if theService is missing value then error "NO_SERVICE"
    send theText to buddy theHandle of theService
  end tell
end run
'''


def send_via_messages(handle, text, service_type):
    """Ask Messages.app to send. Returns (ok: bool, message: str).

    handle/text/service_type are passed as argv (no escaping pitfalls).
    """
    try:
        p = subprocess.run(
            ["osascript", "-", handle, text, service_type],
            input=_SEND_APPLESCRIPT, text=True,
            capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        return False, "osascript를 찾을 수 없습니다 (macOS 전용 기능입니다)."
    except subprocess.TimeoutExpired:
        return False, "전송 시간 초과 — Automation 권한 팝업이 떠 있는지 확인하세요."
    if p.returncode == 0:
        return True, "Messages에 전송 요청을 보냈습니다"
    err = (p.stderr or "").strip()
    low = err.lower()
    if "-1743" in err or "not allow" in low or "not authoriz" in low:
        return False, (
            "Automation 권한이 필요합니다: System Settings > Privacy & Security >\n"
            "Automation 에서 이 터미널 앱의 'Messages' 제어를 허용한 뒤 다시 시도하세요.")
    if "NO_SERVICE" in err:
        return False, (f"'{service_type}' 서비스를 찾을 수 없습니다. "
                       "Messages에 해당 계정(iMessage/문자전달)이 설정돼 있는지 확인하세요.")
    return False, f"전송 실패: {err}"


def resolve_send_target(con, identifier, contacts, force_service):
    """Figure out (handle, name, service) to send to. Returns a dict:
      {handle, name, service, new}                  — ready to send
      {ambiguous: [handles]}                          — too many people matched
      {group: True}                                   — matched a group (unsupported)
      None                                            — nothing matched
    """
    chat_ids, parts = find_chats(con, identifier, contacts)
    if not chat_ids:
        # brand-new target typed directly as a phone/email?
        q = identifier.strip()
        if "@" in q or len(digits(q)) >= 7:
            stype = force_service or "iMessage"
            return {"handle": q, "name": contacts.display(q),
                    "service": stype, "new": True}
        return None

    ph = ",".join("?" * len(chat_ids))
    det = {r["rid"]: r for r in con.execute(
        f"SELECT ROWID AS rid, chat_identifier AS ci, service_name AS svc "
        f"FROM chat WHERE ROWID IN ({ph})", chat_ids)}
    last = {}
    for cid, md in con.execute(
        f"SELECT cmj.chat_id, MAX(m.date) FROM chat_message_join cmj "
        f"JOIN message m ON m.ROWID = cmj.message_id "
        f"WHERE cmj.chat_id IN ({ph}) GROUP BY cmj.chat_id", chat_ids):
        last[cid] = md or 0

    # candidates: 1:1 chats only. identity collapses a person's many handles.
    cands = []   # (handle, service, last_date, identity)
    groups = []
    for cid in chat_ids:
        plist = parts.get(cid, [])
        d = det.get(cid)
        if len(plist) > 1:
            groups.append(cid)
            continue
        h = plist[0] if plist else (d["ci"] if d else None)
        if not h:
            continue
        identity = contacts.name_for(h) or h
        cands.append((h, (d["svc"] if d else None), last.get(cid, 0), identity))

    if not cands:
        return {"group": True} if groups else None

    identities = []
    for _h, _s, _d, idy in cands:
        if idy not in identities:
            identities.append(idy)
    if len(identities) > 1:
        # genuinely different people — surface one handle per identity
        reps = []
        for idy in identities:
            reps.append(next(h for h, _s, _d, i in cands if i == idy))
        return {"ambiguous": reps}

    # one person: prefer an iMessage thread, then the most recent.
    best = max(cands, key=lambda c: (1 if c[1] == "iMessage" else 0, c[2]))
    handle, svc = best[0], best[1]
    if force_service:
        stype = force_service
    elif svc == "iMessage":
        stype = "iMessage"
    elif svc == "SMS":
        stype = "SMS"
    else:
        stype = "iMessage"
    return {"handle": handle, "name": contacts.display(handle),
            "service": stype, "new": False}


def _verify_sent(handle, text):
    """Re-read chat.db (fresh) to report is_sent/error for the just-sent msg."""
    suf = digits(handle)[-8:] if digits(handle) else ""
    hl = handle.lower()
    for _ in range(4):
        time.sleep(1.5)
        con, tmp = open_db()
        try:
            rows = con.execute(
                "SELECT m.is_sent AS sent, m.error AS err, m.text AS t, "
                "m.attributedBody AS ab, h.id AS hid FROM message m "
                "LEFT JOIN handle h ON h.ROWID = m.handle_id "
                "WHERE m.is_from_me = 1 ORDER BY m.date DESC LIMIT 8"
            ).fetchall()
        finally:
            con.close()
            shutil.rmtree(tmp, ignore_errors=True)
        for r in rows:
            hid = (r["hid"] or "")
            same_handle = (suf and digits(hid).endswith(suf)) or (hl and hl == hid.lower())
            if not same_handle:
                continue
            if message_text(r["t"], r["ab"]).strip() == text.strip():
                if r["err"]:
                    return f"⚠️ 전송 오류 코드 {r['err']}"
                if r["sent"]:
                    return "✓ 전송 확인됨 (is_sent=1)"
                return "⏳ 전송 요청됨 (아직 is_sent=0 — 잠시 후 반영)"
    return "⏳ 전송 요청됨 (확인 보류 — chat.db 반영 지연)"


def cmd_send(con, args, contacts):
    force_service = "SMS" if args.sms else ("iMessage" if args.imessage else None)
    text = " ".join(args.text).strip()
    if not text:
        _die("보낼 내용이 비어 있습니다.")

    tgt = resolve_send_target(con, args.identifier, contacts, force_service)
    if tgt is None:
        _die(f"'{args.identifier}' 대상을 찾지 못했습니다. "
             f"`msg threads`로 확인하거나 번호/이메일을 직접 쓰세요.")
    if tgt.get("group"):
        _die("그룹 대화 전송은 아직 미지원입니다. (ROADMAP의 다음 단계)")
    if tgt.get("ambiguous"):
        reps = tgt["ambiguous"]
        lines = "\n".join(f"  {i}. {contacts.display(h)}  ({h})"
                          for i, h in enumerate(reps, 1))
        # non-interactive (or --force) → safe error, don't guess a recipient
        if args.force or not sys.stdin.isatty():
            _die("대상이 여러 명입니다. 번호/이메일로 정확히 지정하세요:\n" + lines)
        print("대상이 여러 명입니다. 누구에게 보낼까요?\n" + lines)
        try:
            pick = input("번호 선택 (취소: Enter) : ").strip()
        except EOFError:
            pick = ""
        if not pick.isdigit() or not (1 <= int(pick) <= len(reps)):
            print("취소했습니다.")
            return
        chosen = reps[int(pick) - 1]
        tgt = resolve_send_target(con, chosen, contacts, force_service)
        if not tgt or tgt.get("ambiguous") or tgt.get("group"):
            _die(f"선택한 대상({chosen})을 확정하지 못했습니다.")

    handle, name, stype = tgt["handle"], tgt["name"], tgt["service"]
    label = name if (name and name != handle) else handle

    print("─ 전송 미리보기 ─")
    print(f"  받는 사람 : {label}" + (f"   ({handle})" if label != handle else ""))
    print(f"  서비스    : {stype}" + ("    ⚠️ 새 대화(기존 스레드 없음)" if tgt.get("new") else ""))
    print(f"  내용      : {text}")

    if args.dry_run:
        print("  (--dry-run: 실제로 보내지 않았습니다)")
        return

    if not args.force:
        try:
            ans = input("보낼까요? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("취소했습니다.")
            return

    ok, msg = send_via_messages(handle, text, stype)
    if not ok:
        _die(msg)
    print(f"✓ {msg}")
    if not args.no_verify:
        print("  " + _verify_sent(handle, text))


# --------------------------------------------------------------------------
# complete (shell autocompletion candidates)
# --------------------------------------------------------------------------
def cmd_complete(args, contacts):
    """Print newline-separated identifier candidates matching a prefix.

    Fast path: contact names + chat display names. Opens chat.db read-only
    in place (no 459MB copy) so tab-completion stays snappy.
    """
    pref = args.prefix.strip().lower()
    cands = {nm for nm, _em, _ph in contacts.name_handles}
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        for (dn,) in con.execute(
            "SELECT DISTINCT display_name FROM chat "
            "WHERE display_name IS NOT NULL AND display_name != ''"
        ):
            cands.add(dn)
        con.close()
    except sqlite3.Error:
        pass
    out = sorted(c for c in cands if c and (not pref or pref in c.lower()))
    print("\n".join(out))


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        prog="msg", description="macOS 메시지 대화 읽기 (읽기 전용)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("threads", help="최근 대화 스레드 목록")
    pt.add_argument("--limit", "-n", type=int, default=20)
    pt.add_argument("--json", action="store_true")

    pr = sub.add_parser("read", help="특정 스레드의 메시지")
    pr.add_argument("identifier", help="전화번호/이메일/이름/chat_identifier")
    pr.add_argument("--limit", "-n", type=int, default=40)
    pr.add_argument("--json", action="store_true")

    pu = sub.add_parser("unread", help="안 읽은 메시지만 (Messages 파란 점)")
    pu.add_argument("--limit", "-n", type=int, default=100)
    pu.add_argument("--all", "-a", action="store_true",
                    help="필터됨/알 수 없는 발신자 폴더까지 포함")
    pu.add_argument("--json", action="store_true")

    for cmd_name, h in (("send", "메시지 보내기 (미리보기+확인)"),
                        ("reply", "메시지 보내기 (send의 별칭)")):
        ps = sub.add_parser(cmd_name, help=h)
        ps.add_argument("identifier", help="전화번호/이메일/이름/chat_identifier")
        ps.add_argument("text", nargs="+", help="보낼 내용")
        ps.add_argument("--force", "-f", action="store_true", help="확인 없이 바로 발송")
        ps.add_argument("--dry-run", action="store_true", help="미리보기만, 안 보냄")
        ps.add_argument("--sms", action="store_true", help="SMS로 강제")
        ps.add_argument("--imessage", action="store_true", help="iMessage로 강제")
        ps.add_argument("--no-verify", action="store_true", help="전송 후 확인 생략")

    pc = sub.add_parser("complete", help="자동완성 후보 출력 (셸 completion용)")
    pc.add_argument("prefix", nargs="?", default="")

    args = p.parse_args()
    contacts = Contacts()
    if args.cmd == "complete":
        cmd_complete(args, contacts)
        return
    con, tmp = open_db()
    try:
        if args.cmd == "threads":
            cmd_threads(con, args, contacts)
        elif args.cmd == "read":
            cmd_read(con, args, contacts)
        elif args.cmd == "unread":
            cmd_unread(con, args, contacts)
        elif args.cmd in ("send", "reply"):
            cmd_send(con, args, contacts)
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
