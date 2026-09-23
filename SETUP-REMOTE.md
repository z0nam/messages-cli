# 원격 HTTP 표면 세팅 (`msg.namun.net`)

`http_server.py`를 인터넷에 노출해 **폰(ChatGPT/Claude 앱)에서** 내 맥 Messages를 읽고,
**폰 승인 게이트**로 보내기까지 하는 절차. 로컬 코어는 이미 검증됨. 아래는 *go-live*.

> ⚠️ 사적 대화 + 전송 권한을 인터넷에 여는 작업이다. TLS·Bearer·푸시 승인이 다 갖춰진 뒤에만 켠다.
> 잔여 위험은 맨 아래 "위협 모델" 참고.

## 0. 구성 요소
```
ChatGPT/Claude 폰 앱 ──TLS──> cloudflared(msg.namun.net) ──> 127.0.0.1:8787 http_server.py ──> msg CLI
                                                                      │ send(confirm) 시
                                                                      └─> ntfy 푸시 ─> 폰에서 Approve 탭 ─> /approve ─> 실제 전송
```

## 1. 시크릿 생성 + 저장 (repo 밖!)
```bash
mkdir -p ~/.config/msg
umask 077
cat > ~/.config/msg/http.env <<EOF
MSG_HTTP_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
MSG_APPROVE_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
MSG_NTFY_URL=https://ntfy.sh/msg-$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')
MSG_PUBLIC_URL=https://msg.namun.net
MSG_HTTP_PORT=8787
# 처음엔 읽기 전용으로 시작 권장. 보내기 열 때 아래 두 줄:
# MSG_SEND_ENABLED=1
# MSG_SEND_DRYRUN=1   # 라이브 테스트: 승인해도 실제로는 안 보냄. 확인되면 이 줄 삭제.
EOF
chmod 600 ~/.config/msg/http.env
echo "ntfy 토픽:"; grep NTFY ~/.config/msg/http.env
```
- `MSG_NTFY_URL`의 토픽 문자열이 **곧 비밀**이다(구독자면 승인 버튼을 봄). 길고 랜덤하게(위 자동생성). 노출되면 재발급.
- 폰에 **ntfy 앱** 설치 후 그 토픽을 구독.

## 2. 로컬 기동 확인
```bash
./run-http.sh          # 127.0.0.1:8787 에서 뜸
curl -s localhost:8787/health          # -> messages-cli http ok
```

## 3. cloudflared named tunnel → namun.net
namun.net이 Cloudflare DNS를 쓴다는 전제(아니면 네임서버를 Cloudflare로 이전).
```bash
brew install cloudflared
cloudflared tunnel login                       # 브라우저에서 namun.net 존 인증
cloudflared tunnel create msg-mac
cloudflared tunnel route dns msg-mac msg.namun.net
# ~/.cloudflared/config.yml :
#   tunnel: msg-mac
#   credentials-file: /Users/namun/.cloudflared/<TUNNEL-ID>.json
#   ingress:
#     - hostname: msg.namun.net
#       service: http://127.0.0.1:8787
#     - service: http_status:404
cloudflared tunnel run msg-mac                  # 테스트 실행
curl -s https://msg.namun.net/health           # 외부에서 확인
```

## 4. 상시 실행 (launchd)
`~/Library/LaunchAgents/net.namun.msg-http.plist` 와 `...msg-tunnel.plist` 두 개로
`run-http.sh` 와 `cloudflared tunnel run msg-mac` 를 KeepAlive. (맥이 깨어 있어야 함.)
```bash
launchctl load -w ~/Library/LaunchAgents/net.namun.msg-http.plist
launchctl load -w ~/Library/LaunchAgents/net.namun.msg-tunnel.plist
```
(plist 템플릿은 필요하면 생성해줌.)

### 4-FDA. ⚠️ Full Disk Access — 정식 서명 .app 래퍼 필수

launchd로 뜬 백그라운드 데몬은 그냥은 `chat.db`를 못 읽는다(Full Disk Access). 그리고
**인터프리터(python) 바이너리를 FDA 목록에 직접 넣어도 launchd 잡에는 안 먹힌다** —
`/usr/bin/python3`(Apple 플랫폼 바이너리)는 사용자 FDA를 무시하고, python은 실행 시
프레임워크 내부 `Python.app`으로 재-exec 되며, **adhoc 서명 .app + `open` 기동도 실패**한다.

확실히 되는 방법 = **정식(자체) 서명된 .app 래퍼로 감싸고, 그 .app에 FDA를 준다.**
(터미널 앱이 자식 프로세스에 FDA를 물려주는 것과 동일 원리.)

1. `~/Applications/MessagesRemote.app` 번들 — `Contents/MacOS/<launcher>`는 **진짜 Mach-O**
   (C로 컴파일)여야 한다. 셸 스크립트면 `exec`로 프로세스가 번들 밖으로 나가 귀속이 깨진다.
   런처는 `fork`로 python 서버를 **자식**으로 돌리고 자신은 살아남아 responsible process가 된다.
2. **자체 서명 인증서**로 서명(안정적 DR): `openssl`로 codeSigning 인증서 생성 →
   PKCS12는 `-keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1`(legacy)로 만들어야
   `security import`가 먹음 → 로그인 키체인에 `-T /usr/bin/codesign`으로 import →
   `codesign -s "<식별자>" --force --deep <app>`. **같은 인증서로 재서명하면 DR이 유지되어
   FDA grant가 안 깨진다**(앱 수정해도 재-grant 불필요).
3. LaunchAgent가 `open -W -n <app>` 로 기동(KeepAlive).
4. System Settings > Privacy & Security > Full Disk Access 에 **그 .app**을 추가(GUI, CLI 불가).
   재서명하면 서명이 바뀌므로 한 번 재추가 필요.

부수: 데몬 컨텍스트에선 AddressBook(연락처)도 TCC로 막힐 수 있는데, `msg`는 연락처를 못 읽으면
**이름 없이 계속 진행**(크래시 안 함)하도록 되어 있다.

## 5. 폰 앱에 커넥터 등록

인증은 **두 방식 중 하나**(같은 토큰):
- **Bearer 헤더**: URL `https://msg.namun.net/mcp` + `Authorization: Bearer <MSG_HTTP_TOKEN>`.
- **경로 토큰(no-auth)**: URL `https://msg.namun.net/<MSG_HTTP_TOKEN>/mcp`, 인증칸 비움.
  서버는 경로에 토큰이 있으면 401을 안 내므로, 클라이언트가 401→OAuth 탐색으로 새는 것을 막는다.

- **ChatGPT**: (2026 기준) Settings → Apps → Advanced settings에서 **Developer mode** ON →
  Apps & Connectors → Create → URL 입력, 인증 No-auth(경로토큰) 또는 Token(Bearer). Plus/Pro 이상.
- **Claude(웹/iOS)**: Settings → Connectors → Add custom connector. **iOS 앱은 Bearer 입력란이 없어**
  "로그인 필요"를 켜면 OAuth를 요구하므로, **경로 토큰 URL + 로그인 필요 OFF**로 등록한다.
- 등록 후 `messages_threads` 등으로 스모크 테스트. 읽기 먼저.

### 본인에게 보내기 (self)
`me`/`나`/`내번호` 로 보내면 소유자 본인에게 iMessage로 간다. 본인 번호를 per-user 설정에 등록:
```bash
echo 'self=+8210XXXXXXXX' >> ~/.config/msg/config   # 또는 env MSG_SELF
```
(repo에 하드코딩하지 않으므로 각 머신에서 그 머신 소유자로 해석된다. 자기 번호는 SMS가 안 되어 iMessage 고정.)
- 등록 후 `messages_threads` 등으로 스모크 테스트. 읽기 먼저.

## 6. 보내기 켜기 (마지막)
1. `http.env`에서 `MSG_SEND_ENABLED=1`, `MSG_SEND_DRYRUN=1` 주석 해제 → 재기동.
2. 폰 앱에서 "나한테 테스트 보내줘" → **ntfy 승인 푸시** 확인 → Approve 탭 →
   응답에 `(--dry-run: 실제로 보내지 않았습니다)` 나오면 파이프라인 정상.
3. `MSG_SEND_DRYRUN` 줄 삭제 → 재기동. 이제 승인 시 실제 발송.

## 위협 모델 (요약)
- **회선 가로채기(MITM)**: cloudflared TLS로 차단. http_server는 127.0.0.1 바인드라 직접 노출 안 됨.
- **토큰 유출 = 최대 위험**: `MSG_HTTP_TOKEN` 새면 전부 열림. ChatGPT/Claude 클라우드가 이 토큰을 보관함(그들 신뢰 전제). 유출 의심 시 `http.env`에서 교체 후 재기동 + 커넥터 갱신.
- **프롬프트 인젝션**: 받은 문자에 심긴 지시로 모델이 낚여도, 전송은 **폰 Approve 탭**이 필요해 실제로는 안 나감(핵심 방어).
- **ntfy 토픽 비밀성**: 공개 ntfy.sh는 토픽만 알면 구독 가능 → 토픽을 길고 랜덤하게. 더 강히 하려면 self-host ntfy(+auth)나 Telegram 봇 인라인 버튼으로 교체.
- 읽기/쓰기 분리를 더 원하면: 원격은 `MSG_SEND_ENABLED=0`(읽기 전용)로 두고 보내기는 로컬 stdio에서만.
