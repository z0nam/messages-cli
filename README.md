# messages-cli (`msg` / `messages`)

macOS **메시지(Messages)** 앱 대화를 읽는 읽기 전용 CLI. stdlib만 사용(외부 의존성 0).
더 큰 `msg` CLI(읽기+보내기)의 **읽기 절반**이며, Claude Code(SKILL.md)·Codex(AGENTS.md)
양쪽에서 호출할 single source of truth를 목표로 한다. (보내기는 별도 세션.)

## 설치
실물 파일은 `~/dev/messages-cli/msg`. PATH에는 심링크 두 개로 노출:

```
~/.local/bin/msg      -> ~/dev/messages-cli/msg
~/.local/bin/messages -> ~/dev/messages-cli/msg
```

`~/.local/bin`은 이미 PATH에 있음. 권한: 터미널 앱에 **Full Disk Access** 필요
(System Settings > Privacy & Security > Full Disk Access). 없으면 친절한 안내 후 종료.

### 탭 자동완성 (zsh)
`msg read <TAB>`에서 연락처 이름·업체명을 자동완성. `~/.zshrc`에 한 줄(설치 시 자동 추가됨):
```
source ~/dev/messages-cli/completions/msg.zsh
```

### AI 에이전트 스킬 (SSOT)
하나의 `SKILL.md`(`.claude/skills/messages/SKILL.md`)를 Claude Code·Codex 양쪽에 심링크:
```
~/.claude/skills/messages -> ~/dev/messages-cli/.claude/skills/messages
~/.codex/skills/messages   -> ~/dev/messages-cli/.claude/skills/messages
```

## 사용
```
msg threads [--limit N] [--json]            # 최근 대화 스레드 목록 (기본 20)
msg read <identifier> [--limit N] [--json]  # 특정 스레드 메시지 (기본 40)
msg unread [--limit N] [--all] [--json]     # 안 읽은 메시지만 (Messages 메인 파란 점; --all=필터 폴더까지)
msg complete [prefix]                        # 자동완성 후보 출력 (셸 completion용)
```
`<identifier>`: **연락처 이름**·전화번호(끝 8자리 느슨 매칭, `010…`/`+8210…` 무관)·이메일·
표시이름·`chat_identifier`·그룹 guid. 같은 사람은 **이름·번호·이메일 어느 것으로 쳐도** 같은 대화.

예 (이름·번호·이메일은 예시용 가짜 값):
```
msg threads -n 10
msg read 홍길동              # 연락처 이름으로
msg read 821012345678       # 같은 사람을 번호로 (동일 결과)
msg read 12345678            # 끝 8자리 suffix 매칭 (+821012345678)
msg read hong@example.com    # → 발신자가 연락처 이름으로 표시됨
```

출력(사람용):
```
[2026-06-24 18:46] 나: 내일 봐요
[2026-06-25 10:30] +821012345678 (SMS): [Web발신] …
```
`--json`: `{date, from, handle, contact, is_from_me, service, text, has_attachment}` 배열.
`from`=표시명(이름 or 핸들), `handle`=원본 번호/이메일, `contact`=연락처 이름(없으면 `""`).

## 설계 노트 (헤맨 지점 = 미래의 함정 방지)
- **읽기 전용 보장 + 최신 메시지**: live `chat.db`는 WAL 잠금/최신 누락이 있어
  `chat.db`+`-wal`+`-shm` 3개를 temp로 복사한 뒤 **`mode=ro`(immutable 아님)**로 열고,
  끝나면 temp 정리. 원본은 절대 안 건드림(복사만 함).
  ⚠️ **함정**: `immutable=1`을 쓰면 SQLite가 `-wal`을 무시해서, main 파일로 체크포인트되기
  전의 **최근 메시지(보통 가장 최신 몇 건)가 통째로 안 보인다.** 실제로 이것 때문에
  threads/read/unread가 최신 메시지를 놓치는 버그가 있었다. WAL을 읽으려면 immutable을 빼야 함.
  (복사본은 우리만 읽으므로 `mode=ro`로 충분히 안전하고 WAL까지 본다.)
- **본문은 99.9%가 `attributedBody`**: 이 Mac 기준 `message.text`는 0.1%만 채워져 있고
  나머지는 NSAttributedString typedstream 바이너리. 의존성 없이 `NSString` 런의
  `\x84\x01+`(UTF-8 타입태그) 뒤 길이 프리픽스(0x81=int16LE, 0x82=int32LE, 0x83=int64LE,
  signed)를 읽어 **바이트를 먼저 자르고 UTF-8 디코드**(decode-먼저는 한글/이모지 깨짐).
  전체 24.3만 행 감사 결과 96.7% 본문 추출, 나머지는 첨부/리액션/개행 등 원래 빈 메시지.
  → pytypedstream 같은 라이브러리 불필요. (필요해지면 그때 도입.)
- **날짜**: Apple Cocoa epoch 나노초. `unix = date/1e9 + 978307200`. 단 아주 오래된 행(2010년대)은
  초 단위일 수 있어 자릿수(`>1e11`)로 가드. 표시는 localtime. (2010년 SMS까지 정확히 디코드 확인.)
- **전화번호 매칭**: 숫자만 추출해 끝 8자리 suffix 매칭(국가코드 무관). iMessage+SMS가
  서로 다른 chat으로 나뉘어도 같은 사람이면 합쳐서 읽음.
- **연락처 이름(AddressBook)**: `~/Library/Application Support/AddressBook/**/*.abcddb`를
  읽기 전용으로 읽어 핸들↔이름 매핑. 표시명은 macOS 방식(한국어=성+이름)으로 조합
  (`ZNAME`이 비어 있어 `ZFIRSTNAME`/`ZLASTNAME`/닉네임/조직으로 직접 조합). 전화는 끝 8자리,
  이메일은 소문자 매칭. 이름으로 검색하면 그 연락처의 모든 핸들을 가진 대화를 찾음.
  AddressBook 못 읽으면 조용히 원본 핸들로 fallback. 소스 DB가 여러 개(iCloud/Google 등)면 병합.
- **탭 자동완성**: `msg complete`가 연락처 이름+채팅 표시이름을 부분일치로 출력(라이브 DB를
  복사 없이 ro로 열어 빠름, ~0.08s). `completions/msg.zsh`가 `compadd -U`로 노출.
  조합 중(미확정) 한글은 빈 단어로 와서 전체를 쏟으므로 **빈 입력 가드**로 막음(밑줄 사라진 뒤 Tab).
- **`unread` = Messages 메인 파란 점**: `is_from_me=0 AND is_read=0 AND
  date > chat.last_read_message_timestamp AND chat.is_filtered = 0`.
  - `is_read`만 보면 옛 행에 stale하게 0이 남아 과다 포착(2019년 메시지 섞임),
    `last_read_message_timestamp`만 보면 한 번도 안 연 대화가 과다 포착 → **둘을 AND**.
  - `chat.is_filtered`: **0=메인 받은편지함**, **1=알 수 없는 발신자**, **2=필터됨(프로모션 등)**.
    1·2는 앱 메인 목록에 뱃지가 안 뜨므로 기본 숨김(footer로 건수 안내), `--all`로 포함.
    → 스팸/택배/광고가 unread를 더럽히던 문제를 **chat.db 쓰기 없이**(읽기 전용 유지) 해결.
- **출력 줄바꿈**: 메시지 본문의 `\r`(CR)을 LF로 접음 — 안 그러면 터미널에서 커서가 줄 앞으로
  돌아가 글자가 겹쳐 깨진다(특히 `[Web발신]` CRLF 문자).
- **발신자/서비스**: `is_from_me`→"나", 아니면 `handle.id`. `service`가 iMessage가 아니면
  `(SMS)`/`(RCS)` 라벨. 그룹은 `chat_handle_join`→`handle.id`로 참여자 매핑.
- **첨부**: `cache_has_attachments` 또는 본문에 U+FFFC(object-replacement)가 있으면 `[첨부]` 표시.
  파일 경로 해석은 stretch(미구현).

## 검증 메모
- `attributedBody` 디코더: 알고 있는 최근 메시지로 정답 대조(한글/이모지/멀티라인 포함).
- 한글·이모지·긴 메시지·멀티라인 정상. 전체 DB 디코드 실패율 사실상 0(빈 메시지 제외).

## 범위 밖 (다음 세션)
보내기(osascript / SMS 릴레이), 첨부 파일 경로 해석.
(연락처 이름 매핑·이름 검색·탭 자동완성은 구현됨.)
