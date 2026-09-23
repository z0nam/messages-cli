#!/usr/bin/env python3
"""
messages-cli HTTP surface — MCP Streamable-HTTP + Bearer, for remote/phone use.

This is the *personal remote* surface (e.g. https://msg.namun.net) layered on the
same `msg` CLI core as the stdio server. It lets ChatGPT / Claude phone apps (as a
custom connector) and other remote clients reach your Mac's Messages.

Threat model & guards (see README "MCP 서버" / ROADMAP D):
- **TLS**: terminate at the tunnel (cloudflared → msg.namun.net). This process
  binds 127.0.0.1 only; it is never directly internet-facing.
- **Bearer auth**: every /mcp call must carry `Authorization: Bearer <MSG_HTTP_TOKEN>`.
- **Send is push-gated**: `messages_send` with confirm=true does NOT send directly.
  It resolves + previews, fires a push (ntfy) with an Approve button to your phone,
  and only sends after you tap Approve. A tricked model cannot self-approve.
- Read tools (threads/read/unread/search) run directly under Bearer.

Config (env):
  MSG_HTTP_TOKEN     required. Bearer token for /mcp.
  MSG_HTTP_PORT      default 8787. Localhost bind port.
  MSG_APPROVE_SECRET required for send. HMAC key signing approve links.
  MSG_PUBLIC_URL     e.g. https://msg.namun.net . Base for approve links in the push.
  MSG_NTFY_URL       e.g. https://ntfy.sh/<long-random-topic> . Push channel.
                     Use a long random topic (it is a shared secret) or self-host with auth.
  MSG_SEND_ENABLED   "1" to allow remote send at all (default off — read-only).

Run:  MSG_HTTP_TOKEN=... python3 http_server.py
"""
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mcp_server as core  # SSOT: reuse the exact tool registry + msg subprocess

TOKEN = os.environ.get("MSG_HTTP_TOKEN", "")
PORT = int(os.environ.get("MSG_HTTP_PORT", "8787"))
APPROVE_SECRET = os.environ.get("MSG_APPROVE_SECRET", "").encode()
PUBLIC_URL = os.environ.get("MSG_PUBLIC_URL", "").rstrip("/")
NTFY_URL = os.environ.get("MSG_NTFY_URL", "").rstrip("/")
SEND_ENABLED = os.environ.get("MSG_SEND_ENABLED", "") == "1"
SEND_DRYRUN = os.environ.get("MSG_SEND_DRYRUN", "") == "1"  # approve → --dry-run (safe test)
APPROVE_TTL = 180  # seconds a pending send stays approvable


def log(*a):
    print("[messages-http]", *a, flush=True)


# --------------------------------------------------------------------------
# Pending-send store (approval gate)
# --------------------------------------------------------------------------
class Pending:
    def __init__(self, identifier, text, service):
        self.identifier = identifier
        self.text = text
        self.service = service
        self.created = None  # set by caller (no time-at-import concerns)
        self.event = threading.Event()
        self.result = None       # filled on approve/deny
        self.decided = None      # "approved" | "denied"


PENDING = {}
PENDING_LOCK = threading.Lock()


def _sig(pid):
    return hmac.new(APPROVE_SECRET, pid.encode(), hashlib.sha256).hexdigest()


def _new_pid():
    # unguessable id; os.urandom avoids any time/random-at-import constraints
    return os.urandom(16).hex()


def _push_approval(pid, preview_text):
    """Fire an ntfy notification with an Approve action button."""
    if not (NTFY_URL and PUBLIC_URL and APPROVE_SECRET):
        return False, "푸시 미설정 (MSG_NTFY_URL/MSG_PUBLIC_URL/MSG_APPROVE_SECRET)"
    sig = _sig(pid)
    approve = f"{PUBLIC_URL}/approve?pid={pid}&sig={sig}"
    deny = f"{PUBLIC_URL}/deny?pid={pid}&sig={sig}"
    # HTTP header values are latin-1; keep Title/action labels ASCII. The Korean
    # preview (recipient + content) rides in the UTF-8 body, which is fine.
    actions = (f"http, Approve, {approve}, method=POST, clear=true; "
               f"http, Deny, {deny}, method=POST, clear=true")
    req = urllib.request.Request(
        NTFY_URL, data=preview_text.encode("utf-8"),
        headers={
            "Title": "Messages: send approval",
            "Priority": "high",
            "Tags": "warning",
            "Actions": actions,
        }, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        return True, "pushed"
    except Exception as e:
        return False, f"푸시 실패: {e}"


def _remote_send(identifier, text, service):
    """The gated send: preview (resolve) → push → wait for phone approval → send."""
    if not SEND_ENABLED:
        return core.text_result(
            "원격 전송이 비활성화돼 있습니다 (MSG_SEND_ENABLED=1 필요). "
            "읽기만 가능합니다.", is_error=True)
    svc = core._service_flag({"service": service})
    # 1) resolve + preview via the CLI (reuses all target-resolution safety)
    ok, out, err = core.run_msg(["send", identifier, text, *svc, "--dry-run"])
    if not ok:
        return core.text_result((err or out or "대상 해석 실패").strip(), is_error=True)
    preview = out.strip()
    # 2) register pending + push
    pid = _new_pid()
    pend = Pending(identifier, text, service)
    pend.created = time.monotonic()
    with PENDING_LOCK:
        PENDING[pid] = pend
    pushed, pmsg = _push_approval(pid, preview)
    if not pushed:
        with PENDING_LOCK:
            PENDING.pop(pid, None)
        return core.text_result(pmsg, is_error=True)
    log(f"send pending {pid}: {identifier!r} — awaiting phone approval")
    # 3) wait for approval (long-poll)
    wait_s = min(APPROVE_TTL, 100)
    approved = pend.event.wait(timeout=wait_s)
    if not approved:
        # Do NOT drop the pending on timeout: keep it approvable until
        # APPROVE_TTL so a late Approve tap still sends (the /approve handler
        # executes independently of this waiter). The reaper clears it later.
        remain = max(0, int(APPROVE_TTL - (time.monotonic() - pend.created)))
        return core.text_result(
            f"⏳ 아직 승인 대기 중 ({wait_s}s 경과). 폰 ntfy 알림의 **Approve**를 앞으로 "
            f"약 {remain}초 안에 누르면 그때 전송됩니다 — 다시 보내달라고 할 필요 없습니다.\n\n"
            + preview,
            is_error=True)
    with PENDING_LOCK:
        PENDING.pop(pid, None)
    if pend.decided == "denied":
        return core.text_result("🚫 폰에서 거부됨 — 보내지 않았습니다.")
    return core.text_result(f"✓ 폰 승인 후 전송됨\n{pend.result or ''}".strip())


def _execute_approved(pend):
    """Called from the /approve handler thread: actually send now."""
    svc = core._service_flag({"service": pend.service})
    final = "--dry-run" if SEND_DRYRUN else "--force"
    ok, out, err = core.run_msg(
        ["send", pend.identifier, pend.text, *svc, final], timeout=90)
    pend.result = (out or err or "").strip()
    return ok


# --------------------------------------------------------------------------
# MCP dispatch over HTTP
# --------------------------------------------------------------------------
def dispatch_mcp(msg):
    """Handle one JSON-RPC message; return a response dict or None (notification)."""
    method = msg.get("method")
    params = msg.get("params") or {}
    req_id = msg.get("id")
    if req_id is None:
        return None  # notification
    try:
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "messages_send" and args.get("confirm") is True:
                result = _remote_send(
                    args.get("identifier", ""), args.get("text", ""),
                    args.get("service", "auto"))
            else:
                result = core.handle_request(method, params, req_id)
        else:
            result = core.handle_request(method, params, req_id)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except core.JsonRpcError as e:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": e.code, "message": e.message}}
    except Exception as e:
        log("dispatch error:", repr(e))
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": str(e)}}


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        line = fmt % a
        if TOKEN and TOKEN in line:      # never write the path-embedded token to logs
            line = line.replace(TOKEN, "<token>")
        log(self.address_string(), line)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        h = self.headers.get("Authorization", "")
        want = f"Bearer {TOKEN}"
        return bool(TOKEN) and hmac.compare_digest(h, want)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(n) if n else b""

    # -- approval callbacks (from the ntfy button; no Bearer, uses HMAC sig) --
    def _handle_decision(self, decided):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pid = (q.get("pid") or [""])[0]
        sig = (q.get("sig") or [""])[0]
        if not (pid and sig and APPROVE_SECRET
                and hmac.compare_digest(sig, _sig(pid))):
            return self._text(403, "invalid or expired approval link")
        with PENDING_LOCK:
            pend = PENDING.get(pid)
        if not pend or pend.event.is_set():
            return self._text(410, "이미 처리됐거나 만료된 요청입니다.")
        if time.monotonic() - pend.created > APPROVE_TTL:
            with PENDING_LOCK:
                PENDING.pop(pid, None)
            return self._text(410, "승인 유효시간이 지났습니다. 다시 보내달라고 요청하세요.")
        if decided == "approved":
            _execute_approved(pend)
            pend.decided = "approved"
        else:
            pend.decided = "denied"
        pend.event.set()
        return self._text(200, "승인됨 ✓" if decided == "approved" else "거부됨")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # Always drain the request body first: on HTTP/1.1 keep-alive an
        # unread body would be mis-parsed as the next request.
        raw = self._read_body()
        # Path-embedded token: clients that can't set an Authorization header
        # (e.g. Claude's iOS custom connector, which only offers OAuth or none)
        # use https://host/<TOKEN>/mcp with "no auth". TLS encrypts the path.
        path_token_ok = False
        if TOKEN and path.startswith(f"/{TOKEN}/"):
            path_token_ok = True
            path = path[len(TOKEN) + 1:]   # strip "/<TOKEN>", keep "/mcp"
        if path == "/approve":
            return self._handle_decision("approved")
        if path == "/deny":
            return self._handle_decision("denied")
        if path == "/mcp":
            if not (self._authed() or path_token_ok):
                return self._json(401, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32001, "message": "unauthorized"}})
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                return self._json(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}})
            resp = dispatch_mcp(msg)
            if resp is None:
                return self._text(202, "")  # notification accepted, no body
            return self._json(200, resp)
        return self._text(404, "not found")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/health"):
            return self._text(200, "messages-cli http ok")
        # MCP streamable-HTTP GET (server->client SSE) not supported; that's allowed.
        return self._text(405, "method not allowed")


def _reap_pending():
    """Drop pendings past APPROVE_TTL so the store can't grow unbounded."""
    while True:
        time.sleep(60)
        now = time.monotonic()
        with PENDING_LOCK:
            for pid in [p for p, pd in PENDING.items()
                        if now - (pd.created or now) > APPROVE_TTL]:
                PENDING.pop(pid, None)


def main():
    if not TOKEN:
        raise SystemExit("MSG_HTTP_TOKEN이 필요합니다 (Bearer 토큰).")
    threading.Thread(target=_reap_pending, daemon=True).start()
    log(f"listening on 127.0.0.1:{PORT}  send_enabled={SEND_ENABLED} "
        f"push={'on' if NTFY_URL else 'off'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
