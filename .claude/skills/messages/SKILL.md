---
name: messages
description: macOS 메시지(Messages) 앱 대화를 읽고(읽기 전용 chat.db) 보내는(Messages.app 경유) CLI. 최근 스레드·특정 상대/그룹 대화 읽기, 안 읽은 메시지, 그리고 iMessage/SMS 보내기. 어느 폴더·세션에서든 셸 명령 `msg`(별칭 `messages`). "내 문자/메시지 뭐 왔어", "OO이랑 대화 보여줘", "OO한테 ~라고 보내줘", 인증번호·택배 문자 찾기 등 메시지 읽기·쓰기 작업이면 이 스킬을 먼저 본다. **보내기는 사용자가 명시적으로 요청할 때만**, 기본 미리보기+확인.
---

# messages — macOS 메시지 읽기 CLI

macOS **메시지(Messages)** 앱의 로컬 DB(`~/Library/Messages/chat.db`)를 **읽기 전용**으로
읽는 Python CLI. **Claude 슬래시 스킬이 아니라 PATH에 설치된 셸 명령**이다
(`~/.local/bin/msg`, 별칭 `~/.local/bin/messages`). 어느 폴더·세션에서든 `Bash`로
`msg <subcommand>` 호출하면 된다. 소스: `~/dev/messages-cli/msg` (stdlib-only, 의존성 0).

읽기는 chat.db를 **읽기 전용**으로 보고, 보내기는 **Messages.app을 osascript로 구동**한다
(chat.db에 쓰지 않음). 읽기 도구의 read-only 순수성은 유지된다.

## 전제
- **Full Disk Access** 필요(읽기): 이 세션을 실행하는 터미널 앱(Ghostty 등)에 부여돼 있어야 한다.
  권한 없으면 `msg`가 친절한 안내 후 종료 → 그대로 사용자에게 전달.
- **Automation 권한** 필요(보내기): System Settings > Privacy & Security > Automation 에서
  터미널 앱의 'Messages' 제어 허용. 없으면 `msg send`가 안내 후 종료.
- 원본 DB는 절대 안 건드린다(temp 복사 + `mode=ro`로 WAL 최신까지 읽음). chat.db 쓰기 없음.

## 명령
```
msg threads [--limit N] [--json]            # 최근 대화 스레드 목록 (기본 20)
msg read <identifier> [--limit N] [--json]  # 특정 스레드 메시지, 시간순 (기본 40)
msg unread [--limit N] [--all] [--json]     # 안 읽은 메시지만 (기본=메인 받은편지함 파란 점)
msg send <identifier> <text…> [--force] [--dry-run] [--sms|--imessage]
msg reply <identifier> <text…> [...]         # send의 별칭
msg complete [prefix]                        # 자동완성 후보(이름/업체명) 출력 — 셸 completion용
```

- **`msg unread`**: Messages 앱 **메인 받은편지함의 파란 점(뱃지)과 일치**하는 안 읽은 수신
  메시지만 대화별로 보여준다(`is_read=0` AND `date > chat.last_read_message_timestamp` AND
  `chat.is_filtered = 0`). 필터됨/알 수 없는 발신자 폴더(스팸·프로모션 등)는 기본 숨김이고
  footer로 건수만 알려준다. **`--all`**이면 그 필터 폴더까지 포함.
  "안 읽은 거 있어?", "새 메시지/문자 뭐 왔어"류에 바로 쓴다. `--json`은 평탄화된
  `[{thread, chat_identifier, date, from, handle, contact, service, text, has_attachment}]`.

### `<identifier>` 매칭 (느슨함)
- **연락처 이름**: macOS 연락처(AddressBook)와 대조해 **이름으로도 검색**된다.
  예) `msg read 홍길동`. 부분 일치 가능(`길동` → 홍길동).
- **전화번호**: 숫자만 추출해 끝 8자리 suffix 매칭. `010…`이든 `+8210…`이든 무관.
  iMessage/SMS가 다른 chat으로 갈려 있어도 같은 상대면 **합쳐서** 읽는다.
- **이메일**(Apple ID), **`chat_identifier`/그룹 guid**, 채팅 표시이름도 받음.
- **같은 사람을 이름·번호·이메일 어느 것으로 쳐도** 같은 대화가 나온다.
- 식별자를 모르면 먼저 `msg threads`로 목록을 보고 거기서 골라라.

### 연락처 이름 표시
- 출력의 발신자/스레드 라벨은 macOS 연락처에 등록된 **이름으로 표시**된다
  (한국어는 성+이름, 예: `홍길동`·`김철수`). 등록 안 된 핸들은 번호/이메일 원본 그대로.
- 연락처 매칭은 전화 끝 8자리·이메일 소문자 기준. AddressBook을 못 읽으면
  조용히 원본 핸들로 fallback(기능 저하만, 에러 아님).

## 사용 패턴 (에이전트용)
- **기계 파싱이 필요하면 항상 `--json`을 붙여라.** 사람용 출력은 스니펫이 잘리고 라벨이 섞인다.
  - `threads --json`: `[{name, chat_identifier, display_name, participants, participant_names, service, is_group, last_date, from, snippet}]`
  - `read --json`: `[{date, from, handle, contact, is_from_me, service, text, has_attachment}]`
    - `from`: 표시명(연락처 이름 있으면 이름, 없으면 핸들). `handle`: 원본 번호/이메일.
      `contact`: 연락처 이름(없으면 `""`). 셋 다 제공하니 이름·핸들 둘 다로 후속 검색 가능.
- 특정인과의 최근 대화 요약: `msg read "<상대>" -n 60 --json` 후 분석.
- "최근 뭐 왔어"류: `msg threads -n 15` (사람용) 또는 `--json`.
- 인증번호/택배/예약 문자 찾기: 상대를 알면 `msg read <번호> --json`, 모르면
  `msg threads -n 40 --json`로 스니펫 훑고 후보 스레드를 `read`로 파고든다.

## 보내기 (send / reply) — ⚠️ 외부로 나가는 동작
```
msg send <상대> <보낼 내용…>      # 미리보기 후 y/N 확인하고 전송
msg reply <상대> <보낼 내용…>     # send의 별칭
```
- **에이전트 안전수칙**:
  - **사용자가 명시적으로 "보내달라"고 할 때만** 실행한다. 추측해서 먼저 보내지 않는다.
  - 보낼 내용·대상이 조금이라도 불확실하면 **먼저 `--dry-run`**으로 미리보기를 사용자에게 보여주고 확정받는다.
  - `--force`(확인 생략)는 **사용자가 분명히 승인했을 때만**. 비대화형(스크립트/CI)에선 확인 프롬프트가
    안 뜨므로 더 조심.
- 대상 해석은 `read`와 동일(이름/번호/이메일). 같은 사람의 여러 핸들은 자동으로 하나 고름
  (iMessage·최근 대화 우선). **서로 다른 사람**이 매칭되면 번호로 고르라고 되묻는다(오발송 방지).
- service 자동 선택: 기존 스레드가 iMessage면 iMessage, 아니면 SMS. `--sms`/`--imessage`로 강제.
- 전송 후 chat.db의 `is_sent`/`error`로 결과를 확인해 알려준다(자기 번호로 SMS는 단말 특성상 실패).
- 그룹 전송·첨부 전송은 아직 미지원.

## 출력 읽는 법
- 발신자: `is_from_me`면 `"나"`, 아니면 상대 핸들(번호/이메일).
- `service`가 `iMessage`가 아니면 `(SMS)`·`(RCS)` 라벨이 붙는다(JSON은 `service` 필드).
- `has_attachment`/`[첨부]`: 첨부가 있다는 표시만. **파일 경로 해석은 미구현**(stretch).
- 시각은 localtime, `YYYY-MM-DD HH:MM`.

## 주의 / 함정
- 본문 대부분은 `attributedBody`(NSAttributedString 바이너리)에서 디코딩된다. CLI가
  이미 처리하므로 신경 쓸 필요 없지만, 드물게 첨부·리액션·개행만 있는 메시지는 본문이 빈다.
- 큰 `-n`(예: 수백)은 무겁다. 필요한 만큼만.
- **프라이버시**: 사용자의 사적 대화다. 요청 범위를 넘어 무단으로 다른 스레드를 뒤지지 말 것.

## 범위 밖 (다음)
그룹/첨부 전송, `msg write`($EDITOR)·`msg draft`(Drafts 통합), 첨부 파일 경로 해석.
(읽기·연락처 이름·검색·탭완성·iMessage/SMS 1:1 보내기는 구현됨.)
