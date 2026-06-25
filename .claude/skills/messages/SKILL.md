---
name: messages
description: macOS 메시지(Messages) 앱 대화를 읽는 읽기 전용 CLI. 최근 스레드 목록과 특정 상대/그룹의 대화 내용을 읽는다. 어느 폴더·세션에서든 셸 명령 `msg`(별칭 `messages`)로 실행. "내 문자/메시지/iMessage 뭐 왔어", "OO이랑 나눈 대화 보여줘", 인증번호·택배·약속 문자 찾기 등 메시지 읽기 작업이면 이 스킬을 먼저 본다. (보내기는 범위 밖.)
---

# messages — macOS 메시지 읽기 CLI

macOS **메시지(Messages)** 앱의 로컬 DB(`~/Library/Messages/chat.db`)를 **읽기 전용**으로
읽는 Python CLI. **Claude 슬래시 스킬이 아니라 PATH에 설치된 셸 명령**이다
(`~/.local/bin/msg`, 별칭 `~/.local/bin/messages`). 어느 폴더·세션에서든 `Bash`로
`msg <subcommand>` 호출하면 된다. 소스: `~/dev/messages-cli/msg` (stdlib-only, 의존성 0).

이건 더 큰 `msg` CLI의 **읽기 절반**이다. **보내기는 아직 미구현 — 이 스킬로 메시지를
보내려 하지 말 것**(`osascript` 등으로 임의 발송 금지).

## 전제
- **Full Disk Access** 필요: 이 세션을 실행하는 터미널 앱(Ghostty 등)에 부여돼 있어야 한다.
  권한 없으면 `msg`가 친절한 안내 메시지 후 종료한다 → 그대로 사용자에게 전달.
- 원본 DB는 절대 안 건드린다(temp 복사 + `mode=ro&immutable=1`). 디스트럭티브 동작 없음.

## 명령
```
msg threads [--limit N] [--json]            # 최근 대화 스레드 목록 (기본 20)
msg read <identifier> [--limit N] [--json]  # 특정 스레드 메시지, 시간순 (기본 40)
msg unread [--limit N] [--all] [--json]     # 안 읽은 메시지만 (기본=메인 받은편지함 파란 점)
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

## 범위 밖 (다음 세션)
보내기(osascript/SMS 릴레이), 첨부 파일 경로 해석.
(연락처 이름 표시·이름 검색·탭 자동완성은 구현됨.)
