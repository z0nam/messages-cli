#!/usr/bin/env python3
"""
messages-cli MCP server (stdio) — a thin, dependency-free wrapper over the `msg` CLI.

Exposes macOS Messages read tools (threads/read/unread/search) plus a strictly
gated send/draft, to any MCP stdio client: Claude Code, Codex, Gemini CLI,
Claude Desktop, Cursor.

Design:
- **stdlib only.** Speaks MCP's stdio transport (newline-delimited JSON-RPC 2.0)
  by hand — no `mcp` pip package, no venv. Install = "run this file with python3".
- Every tool shells out to the sibling `msg` script; the CLI stays the single
  source of truth (contact resolution, attributedBody decode, send safety).
- **Send is gated:** `send` only actually delivers when called with
  `confirm=true`; otherwise it runs `--dry-run` and returns a preview. The `msg`
  CLI itself also refuses to send without a TTY unless `--force`, so this is
  belt-and-suspenders.

An optional streamable-HTTP surface (personal remote, e.g. msg.namun.net) can be
layered on later; this file is the local stdio core.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Resolve the `msg` CLI: env override → sibling file → PATH.
MSG_BIN = os.environ.get("MSG_BIN") or os.path.join(HERE, "msg")
if not os.path.exists(MSG_BIN):
    MSG_BIN = "msg"

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "messages-cli", "version": "0.1.0"}


def log(*a):
    """Diagnostics go to stderr; stdout is reserved for JSON-RPC frames."""
    print("[messages-mcp]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# msg subprocess
# --------------------------------------------------------------------------
def run_msg(cli_args, timeout=60):
    """Run `msg <cli_args>` and return (ok, stdout, stderr)."""
    cmd = [sys.executable, MSG_BIN, *cli_args]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s: msg {' '.join(cli_args)}"
    except FileNotFoundError:
        return False, "", f"msg CLI not found at {MSG_BIN!r}"
    return p.returncode == 0, p.stdout, p.stderr


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _run_and_wrap(cli_args, timeout=60):
    ok, out, err = run_msg(cli_args, timeout=timeout)
    if not ok:
        return text_result((err or out or "명령 실패").strip(), is_error=True)
    return text_result(out.strip() or "(빈 결과)")


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------
def tool_threads(a):
    return _run_and_wrap(["threads", "-n", str(int(a.get("limit", 20))), "--json"])


def tool_read(a):
    ident = a.get("identifier")
    if not ident:
        return text_result("identifier가 필요합니다.", is_error=True)
    args = ["read", str(ident), "-n", str(int(a.get("limit", 40)))]
    if a.get("media"):
        args.append("--media")
    args.append("--json")
    return _run_and_wrap(args)


def tool_unread(a):
    args = ["unread", "-n", str(int(a.get("limit", 100)))]
    if a.get("all"):
        args.append("--all")
    args.append("--json")
    return _run_and_wrap(args)


def tool_search(a):
    q = a.get("query")
    if not q:
        return text_result("query가 필요합니다.", is_error=True)
    args = ["search", str(q)]
    if a.get("from"):
        args += ["--from", str(a["from"])]
    if a.get("since"):
        args += ["--since", str(a["since"])]
    if a.get("until"):
        args += ["--until", str(a["until"])]
    args += ["--limit", str(int(a.get("limit", 30))), "--json"]
    return _run_and_wrap(args)


def _service_flag(a):
    svc = (a.get("service") or "auto").lower()
    if svc == "sms":
        return ["--sms"]
    if svc == "imessage":
        return ["--imessage"]
    return []


def tool_send(a):
    ident, text = a.get("identifier"), a.get("text")
    if not ident or not text:
        return text_result("identifier와 text가 모두 필요합니다.", is_error=True)
    args = ["send", str(ident), str(text), *_service_flag(a)]
    if a.get("confirm") is True:
        # Actually deliver. --force skips the (non-existent, no-TTY) confirm prompt.
        args.append("--force")
        return _run_and_wrap(args, timeout=90)
    # Preview only. Prepend guidance so the agent knows nothing was sent.
    args.append("--dry-run")
    res = _run_and_wrap(args)
    note = ("ℹ️ confirm=false → 미리보기만 했고 **보내지 않았습니다**. "
            "사용자가 명시적으로 보내라고 하면 confirm=true 로 다시 호출하세요.\n\n")
    res["content"][0]["text"] = note + res["content"][0]["text"]
    return res


def tool_draft(a):
    ident, text = a.get("identifier"), a.get("text")
    if not ident or not text:
        return text_result("identifier와 text가 모두 필요합니다.", is_error=True)
    # Draft is local (no external send); --force skips the interactive confirm.
    return _run_and_wrap(["draft", str(ident), str(text), "--force"])


TOOLS = [
    {
        "name": "messages_threads",
        "description": "최근 Messages 대화 스레드 목록(JSON). 상대 이름·마지막 메시지 스니펫·서비스·그룹 여부 포함. '최근 문자/메시지 뭐 왔어'류에 사용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "가져올 스레드 수 (기본 20)", "default": 20},
            },
        },
        "_fn": tool_threads,
    },
    {
        "name": "messages_read",
        "description": "특정 상대/그룹과의 대화 메시지를 시간순으로 읽음(JSON). identifier는 연락처 이름·전화번호·이메일·chat guid 모두 가능(느슨한 매칭). 같은 사람의 iMessage/SMS는 합쳐서 나옴.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "상대: 이름/전화번호/이메일/chat_identifier"},
                "limit": {"type": "integer", "description": "메시지 수 (기본 40)", "default": 40},
                "media": {"type": "boolean", "description": "첨부 있는 메시지만", "default": False},
            },
            "required": ["identifier"],
        },
        "_fn": tool_read,
    },
    {
        "name": "messages_unread",
        "description": "안 읽은 수신 메시지만(JSON) — Messages 앱 메인 받은편지함의 파란 점과 일치. all=true면 필터됨(알 수 없는 발신자·프로모션) 폴더까지 포함. '안 읽은 거 있어?'에 사용.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "최대 개수 (기본 100)", "default": 100},
                "all": {"type": "boolean", "description": "필터 폴더까지 포함", "default": False},
            },
        },
        "_fn": tool_unread,
    },
    {
        "name": "messages_search",
        "description": "전체 메시지 본문 full-text 검색(JSON). 인증번호·택배·예약 문자 등 키워드로 찾을 때 가장 빠름. from으로 특정 상대, since/until로 날짜 범위(YYYY-MM-DD).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어"},
                "from": {"type": "string", "description": "특정 상대로 한정 (이름/번호/이메일)"},
                "since": {"type": "string", "description": "이 날짜 이후 (YYYY-MM-DD)"},
                "until": {"type": "string", "description": "이 날짜 이전 (YYYY-MM-DD)"},
                "limit": {"type": "integer", "description": "결과 수 (기본 30)", "default": 30},
            },
            "required": ["query"],
        },
        "_fn": tool_search,
    },
    {
        "name": "messages_send",
        "description": (
            "iMessage/SMS 메시지 전송. ⚠️ 외부로 나가는 동작. "
            "**사용자가 명시적으로 '보내라'고 요청했을 때만** confirm=true 로 호출해 실제 전송한다. "
            "그 외에는 confirm 생략(=false) → 미리보기(받는 사람·서비스·내용)만 돌려주고 보내지 않는다. "
            "확신이 없으면 항상 먼저 confirm=false로 미리보기를 사용자에게 보여주고 확정받아라. "
            "service는 auto(기본)·imessage·sms."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "받는 사람: 이름/번호/이메일. 계정 소유자 본인에게 보낼 때는 'me'(또는 '나'/'내번호')를 쓰면 설정된 본인 번호로 iMessage 전송됨 — 사용자에게 번호를 되묻지 말 것."},
                "text": {"type": "string", "description": "보낼 내용"},
                "confirm": {"type": "boolean", "description": "true여야 실제 전송. 생략/false면 미리보기만.", "default": False},
                "service": {"type": "string", "enum": ["auto", "imessage", "sms"], "default": "auto", "description": "본인('me') 전송은 자동으로 iMessage."},
            },
            "required": ["identifier", "text"],
        },
        "_fn": tool_send,
    },
    {
        "name": "messages_draft",
        "description": "보내지 않고 Messages 앱의 Drafts에 저장 — 앱에서 그 대화에 '입력 중'으로 보인다. 사람이 앱에서 검토 후 직접 보내게 하는 안전 경로. 기존 드래프트는 백업 후 덮어씀.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "대상: 이름/번호/이메일"},
                "text": {"type": "string", "description": "드래프트 내용"},
            },
            "required": ["identifier", "text"],
        },
        "_fn": tool_draft,
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def public_tools():
    """tools/list payload — strip the internal _fn key."""
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC / MCP dispatch
# --------------------------------------------------------------------------
def handle_request(method, params, req_id):
    """Return a JSON-RPC result dict, or raise for an error response."""
    if method == "initialize":
        client_ver = params.get("protocolVersion")
        return {
            "protocolVersion": client_ver if isinstance(client_ver, str) else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": public_tools()}
    if method == "tools/call":
        name = params.get("name")
        tool = TOOL_BY_NAME.get(name)
        if not tool:
            raise JsonRpcError(-32602, f"unknown tool: {name}")
        args = params.get("arguments") or {}
        try:
            return tool["_fn"](args)
        except Exception as e:  # never crash the server on a tool bug
            log("tool error:", name, repr(e))
            return text_result(f"도구 실행 오류: {e}", is_error=True)
    raise JsonRpcError(-32601, f"method not found: {method}")


class JsonRpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def send_message(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    log(f"starting; MSG_BIN={MSG_BIN}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log("bad JSON frame, skipping")
            continue

        method = msg.get("method")
        params = msg.get("params") or {}
        req_id = msg.get("id")

        # Notifications (no id) get no response.
        if req_id is None:
            if method == "notifications/initialized":
                log("client initialized")
            continue

        try:
            result = handle_request(method, params, req_id)
            send_message({"jsonrpc": "2.0", "id": req_id, "result": result})
        except JsonRpcError as e:
            send_message({"jsonrpc": "2.0", "id": req_id,
                          "error": {"code": e.code, "message": e.message}})
        except Exception as e:
            log("internal error:", repr(e))
            send_message({"jsonrpc": "2.0", "id": req_id,
                          "error": {"code": -32603, "message": str(e)}})
    log("stdin closed; exiting")


if __name__ == "__main__":
    main()
