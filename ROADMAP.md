# messages-cli ROADMAP / 위시리스트

현재(v0): **읽기 전용**. `threads` / `read` / `unread` / `complete` + 연락처 이름 매핑,
attributedBody 디코딩, zsh 탭완성, Claude·Codex 공용 SKILL.md.

다음 큰 덩어리는 **쓰기(보내기)**. 그 외 읽기 강화·인프라 항목도 함께 적어둔다.

---

## A. 쓰기(보내기) — 다음 세션의 핵심

> 원칙: **읽기 도구는 read-only로 그대로 둔다.** 보내기는 명확히 분리된 opt-in 모듈로,
> 외부로 나가는 동작이라 미리보기·확인을 기본값으로 한다.

### A0. 전송 메커니즘 — **실기기 검증 완료 ✅ (2026-06-26, Darwin 25.5)**
- **확정 레시피**: `osascript`로 Messages.app 제어, 핸들+서비스 명시:
  ```applescript
  tell application "Messages"
    set svc to service id "<service-uuid>"        -- 런타임 탐색으로 얻음
    send "<text>" to buddy "<handle>" of svc
  end tell
  ```
  - **iMessage 자기 전송 테스트 → `is_sent=1` 도착 확인.** 우려했던 `send to buddy` 깨짐 **없음**.
  - **SMS 전송 → 메시지 생성됨**(self-SMS는 단말이 확인 안 해 `is_sent=0`이지만, 타 번호 SMS는 `is_sent=1` 확인 — 릴레이 정상).
- **서비스 탐색**: `services`를 돌며 `service type`(iMessage/SMS/RCS)으로 식별.
  일부 레거시/비활성 서비스는 `service type` 접근 시 `-10000` → `try`로 스킵.
  대상 핸들이 iMessage 등록이면 iMessage 서비스, 아니면 SMS 서비스 선택(스레드 기존 service도 참고).
- **상태 확인**: 전송 후 chat.db의 해당 행 `is_sent`/`is_delivered`/`error`로 결과 판정.
- **샌드박스 함정**: AppleEvent는 샌드박스에서 막혀 `-1712` 타임아웃. 실제 `msg`는 일반 셸에서
  osascript를 호출하므로 무관하나, 자동화/CI 환경에선 주의(문서화).
- **Automation 권한** 필요: System Settings > Privacy & Security > Automation > (터미널) → Messages 허용.
  첫 호출 시 팝업 → 없으면 FDA처럼 친절 안내.
- 대안(비공식 IMCore 등)은 비지원 → 다루지 않음.
- (탐구거리) `send … to chat id "<guid>"` 경로 = 그룹/기존 스레드 답장용. 다음 단계에서 검증.

### A1. 명령 표면 (제안)
- `msg send <id> <text…>` — 해당 스레드/상대에게 즉시 전송(미리보기+확인 후).
- `msg reply <id> <text…>` — `send`의 의미적 별칭(대화에 답장). `id` 해석은 `read`와 동일(이름/번호/이메일/guid).
- `msg write [<id>]` — 인터랙티브 작성: `$EDITOR`로 본문 열고 → 미리보기 → 확인 → 전송. id 없으면 대상부터 고름(탭완성/threads).
- `msg draft <id> <text…>` — **보내지 않고** 로컬 드래프트로 저장.
- `msg drafts` — 드래프트 목록 / `msg draft --send <n>` / `msg draft --rm <n>`.

### A2. 드래프트 = Messages.app Drafts 통합 ✅(결정됨)
앱에도 드래프트가 보이도록 `~/Library/Messages/Drafts/`에 직접 쓴다.
**정찰 결과(포맷 확정):**
- 경로: `~/Library/Messages/Drafts/<핸들>/composition.plist`
  - `<핸들>` = 1:1이면 전화번호/이메일(예: `+821012345678`), 그룹은 별도(아마 guid; 그룹 드래프트는 후속).
  - `Pending/` 폴더도 존재.
- `composition.plist`(XML plist) 키: `audioMessage`(bool=false), `text`(**중첩 바이너리 plist**).
- `text` = **NSKeyedArchiver** 아카이브의 **NSMutableAttributedString**:
  `$objects = [$null, {NSString,NSAttributes,$class}, {NS.string: "<본문>"}, NSMutableString클래스, 빈 NSDictionary, …]`.
- **구현**: 평문 드래프트는 이 구조를 **`plistlib`로 순수 파이썬에서 직접 생성** 가능(pyobjc 불필요).
  속성 없는 텍스트면 $objects 7개 고정 틀에 본문만 끼우면 됨.
- **위험/완화**: 이건 chat.db는 아니지만 **Messages 데이터 영역에 쓰기**다.
  → 기존 `composition.plist`는 백업 후 덮어쓰기, 새 폴더만 생성, 쓰기 전 plist 유효성 검사.
  → 앱이 드래프트를 **실시간 반영 안 할 수** 있음(캐시) — 실기기에서 갱신 타이밍 검증 필요.

### A3. 안전 모델 ✅(결정됨: 기본 확인 / `--force`면 바로 발송)
- 기본: 전송 전 **미리보기**(받는 사람 이름+핸들+service+본문) 출력 후 **y/N 확인**.
- **`--force`**로 확인 생략(바로 발송), `--dry-run`으로 해석·미리보기만.
- 대상이 **모호**(여러 채팅 매칭)하면 전송 거부하고 후보 제시.
- 그룹 전송은 명시적으로(`--group`/guid)만.
- (선택) 전송 로그 `~/.local/share/msg/sent.log`.

### A4. 범위 = iMessage + SMS 동시 ✅(결정됨)
- iMessage: `send … to buddy/chat` 경로.
- SMS: `service "SMS"` 경유 — **아이폰 문자 전달(Text Message Forwarding)** 설정 의존.
  - 빌드 첫 단계에서 **실기기 1건 테스트**로 iMessage·SMS 각각 동작 확인 후 코드 확정.
  - service 자동 선택: 상대가 iMessage 등록이면 iMessage, 아니면 SMS(스레드의 기존 service 참고).

### A5. 부수
- `msg mark-read <id>` — 읽음 처리. **chat.db 쓰기**라 위험(Messages 실행 중 WAL 충돌). 별도 가드(앱 종료 권고/확인) 하에만. 우선순위 낮음.

---

## B. 읽기 강화

- `msg search <query> [--from <id>] [--since <date>]` — 전체 본문 검색(attributedBody 디코드 인덱스/순회).
- `msg read` 필터: `--since/--until/--from`, `--media`(첨부만), `--limit` 페이징.
- **첨부 경로 해석**: `attachment`/`message_attachment_join` → 실제 파일 경로 출력/열기(`--open`). (현재 `[첨부]` 표시만)
- `msg export <id> [--format txt|json|html]` — 대화 내보내기.
- `msg watch [<id>]` — 새 메시지 실시간 tail(폴링).
- 리액션/탭백(associated_message_type) 표시.
- 그룹: 발신자별 색/이름 정렬, 참여자 목록 `msg info <id>`.
- `msg contacts <query>` — 연락처 조회(이름↔핸들).

## C. 인프라 / 품질

- **테스트**: 합성 chat.db 픽스처로 디코더·날짜·매칭·unread 단위테스트.
- 성능: 큰 `-n`/검색용 인덱스 활용, 연락처 캐시.
- 설정파일 `~/.config/msg/config.toml`(기본 limit, 계정 등).
- 패키징: `pipx`/`uv tool`로 설치 가능하게, 버전·`--version`.
- `bash` 자동완성(현재 zsh만).
- CI(lint), LICENSE(MIT?) 추가.

---

## 결정 완료 ✅ (A 착수 준비됨)
1. 드래프트: **Messages.app Drafts 통합**(composition.plist, plistlib로 생성)
2. 전송: **미리보기+y/N 기본**, `--force`면 바로 발송
3. 범위: **iMessage + SMS 동시**(첫 단계 실기기 검증 포함)

### 착수 스텝
1. ~~실기기 osascript `send` 검증(iMessage·SMS)~~ **완료 ✅** — A0 레시피 확정.
2. `msg send/reply` MVP(미리보기+확인, `--force`/`--dry-run`) — A0 레시피로 구현, chat.db로 결과 판정.
3. `msg write`($EDITOR 작성) → `msg draft`(composition.plist 생성).
4. Automation 권한 안내 추가, 전송 모듈을 읽기 모듈과 분리(별 파일/명령군).
