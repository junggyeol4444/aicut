# aicut

생방송 VOD 하나를 넣으면, 시스템이 방송 전체를 이해하고
그 안에 **독립적인 콘텐츠가 몇 개 존재하는지 스스로 판단해서**
그 개수만큼 완성 영상을 만든다.

`[통합 기획안 v1]`의 구현체다. 문서의 장 번호를 코드 주석·리포트·에러 메시지에
그대로 인용해 두었으므로, 어떤 코드가 어떤 결정을 근거로 존재하는지 추적할 수 있다.

```
생방송 1개 → 장편 4개 + Shorts 5개
생방송 1개 → 장편 1개
생방송 1개 → 0개          ← 이것도 정상 결과다 (NO_CONTENT)
```

---

## 이 시스템이 하드코딩하지 않는 것 (2장)

| 하드코딩하지 않는 것 | 어디서 결정되는가 |
|---|---|
| 영상 구조 (Hook→Climax 같은 고정 틀) | `producer.plan_structure()` — 콘텐츠마다 다름 |
| 콘텐츠 개수 | `discovery` + `evaluating` — 0개 허용 |
| 콘텐츠 종류 | 화면 상황 라벨은 내부 신호일 뿐 출력 카테고리가 아님 |
| 시간순 편집 | `TB_EDIT_TIMELINE` — 컷 단위 타임라인, 원본 순서와 무관 |
| 영상 길이 | 슬라이더는 힌트. 어긋나면 리포트에 사유 기록 (2.6) |
| 판정 임계값 | 전부 캘리브레이션 프로파일. 코드에 숫자 없음 (17.1) |

**측정하지 않은 값은 확정값으로 쓰지 않는다 (17.5).**
기본 프로파일의 미측정 파라미터는 `provisional`로 표시되고,
그 값을 읽은 실행은 리포트에 “이 결과는 아직 추측값에 기대고 있다”고 남긴다.
`--strict`를 켜면 미측정 파라미터를 읽는 순간 실행이 거부된다.

---

## 플랫폼

CI가 Windows·macOS에서 실제로 돌린다. **처음 돌렸을 때 양쪽 다 실패했다.** 나온 버그:

| 플랫폼 | 증상 | 원인 |
|---|---|---|
| macOS | 자막이 있는 렌더가 전부 실패 | `subtitles='경로'`의 위치 인자를 ffmpeg 7.2가 거부 (`No option name near`). 6.x·7.1은 받아줬다 |
| Windows | sendcmd 줌 렌더 실패 | 명령 파일 경로가 이스케이프 없이 필터로 들어가 `C:`의 콜론에서 끊김 |
| Windows | 워크스페이스 삭제 불가 | UI가 요청 스레드마다 SQLite 연결을 만들고 **닫지 않았다**. 리눅스에선 그냥 누수, Windows에선 파일 잠금 |
| macOS | 그 다음 실행에서 자막 렌더가 또 전부 실패 | Homebrew가 formula를 쪼갰다. 지금 `brew install ffmpeg`가 주는 빌드에는 **libass가 없다** — `subtitles` 필터 자체가 존재하지 않는다 |

넷 다 고쳤다. 마지막 것은 코드 버그가 아니라 사용자 환경이라, 고치는 방식이
다르다: 빌드에 뭐가 있는지 **묻고**(`ffmpeg -filters`), 자르기 전에 확인하고,
없으면 에피소드를 버리는 대신 자막 없이 렌더하고 `.ass`를 옆에 남긴 뒤 그
이탈을 report에 적는다 (2.6). `aicut doctor`가 필터 가용성을 미리 찍는다.
`subtitles` 필터를 지운 가짜 ffmpeg로 스위트 전체를 돌려서 확인했다.

Windows에서 구조적으로 다른 나머지 지점:

| 지점 | 처리 |
|---|---|
| ffmpeg 필터 안의 `C:\경로` | `C\:/경로`로 이스케이프 (자막·폰트 경로 둘 다) |
| 콘솔 코드페이지 | stdout/stderr를 UTF-8로 재구성. cp1252·cp437에서 한글 파일명 출력이 `UnicodeEncodeError`로 죽던 것 |
| `resource` 모듈 부재 | 메모리 측정만 건너뛰고 나머지는 실행 |
| `Scripts\` vs `bin/` | 설치 검증이 양쪽에서 돈다 |
| concat 목록 경로 | 항상 슬래시 (`as_posix()`) |

`tests/test_platform.py`가 이 가정들을 고정하고, CI의 `windows-latest`·`macos-latest`
잡이 실제로 실행한다. 리눅스 테스트만으로는 위 네 개 중 **하나도** 잡히지 않았다.
현재 6잡 전부 통과한다 (`docs/measurements.md`의 플랫폼 표).

## 설치

```bash
pip install .                    # 코어는 표준 라이브러리 + ffmpeg CLI만 필요
pip install '.[stt,vision,llm]'  # 단계별 선택 설치
aicut doctor                     # 20.2 사전 준비 항목 점검
```

STT 백엔드 둘:

```bash
aicut transcribe stream.mkv                          # faster-whisper, CPU에서 돈다
aicut transcribe stream.mkv --backend whisperx --device cuda --stt-model large-v3
```

STT는 GPU를 원하는 유일한 단계다. 분리해 뒀으니 장비 있는 기계에서 트랜스크립트만
뽑아 옮겨도 된다. 17.2의 원본↔완성본 쌍 트랜스크립트도 이걸로 만든다.

세 번째 백엔드로 PocketSphinx가 있다(`pip install pocketsphinx`). 정확도는
나쁘지만 음향 모델을 패키지에 싣고 와서 네트워크도 GPU도 필요 없다 — **실제
인식기 출력으로 파이프라인이 도는지 검증하는 용도**다 (`tests/test_stt_live.py`,
24.9초 오디오를 5.3초에 인식, 침묵 3개가 발화 경계 3개로).

`ffmpeg` / `ffprobe`는 PATH에 있어야 한다.
화자 분리(pyannote)는 HuggingFace 게이트 모델 승인이 선행되어야 한다 (20.2).

---

## 기본 사용

```bash
# 1. 방송 하나 투입 → 편집 계획까지 (MVP 5. 렌더링 없음)
aicut run stream.mkv --no-render --producer anthropic

# 2. 사람이 편집 계획을 읽는다 — MVP 5의 합격 기준 그 자체
aicut plan workspace/<project>/plans/<episode>.json

# 3. AI가 무엇을 콘텐츠로 봤고 무엇을 버렸는지, 그 사유를 검토 (15.4)
aicut candidates <project-id>
aicut candidates <project-id> --candidate <id> --verdict disagree --note "이건 안 나감"

# 4. 렌더링까지 (MVP 6)
aicut run stream.mkv --producer anthropic

# 5. 사람 검수 게이트 — 이걸 통과하지 않으면 공개되지 않는다 (11.3)
aicut review <episode-id> approve --reviewer me

# 중간에 실패했으면 — 이해한 것은 그대로 두고 이어서 (16장)
aicut resume <project-id>

# 6. 쿼터 상태와 다음 PT 자정 리셋 시각 (11.4)
aicut quota

# 화면으로 조작 (15장) — 1~5번을 브라우저에서
aicut ui                 # http://127.0.0.1:8765
```

### 학습 루프 (12.3)

```bash
# A. 레퍼런스 — 공개 지표와 메타데이터만 수집, 패턴만 저장 (4.2, 4.6)
aicut learn reference --query "게임 스트리머 편집 영상" --producer anthropic

# B. 원본↔완성본 — 이 시스템의 핵심 차별점. 네트워크 불필요.
#    같은 작업이 17.2 캘리브레이션 데이터셋이 된다
aicut learn pairs --source-transcript src.json --output-transcript out.json

# C. 성과 — 자기 채널 한정
aicut learn performance --project <id> --days 28
```

### 업로드 (11.3, 11.4)

```bash
aicut upload <episode-id>              # 비공개로 올린다
aicut review <episode-id> approve --reviewer me
aicut upload <episode-id> --publish    # 승인된 것만 공개된다
aicut upload --retry                   # 쿼터 초과로 밀린 큐 처리
```

`--producer mock`(기본값)은 모델 호출 없이 파이프라인 전체를 돌리는 오프라인
스텁이다. 판단을 하지 않으며, 모든 판정에 `mock` 딱지가 붙는다.

---

## 파이프라인 (14장)

```
QUEUED → PARSING → UNDERSTANDING → DISCOVERING → EVALUATING
       → PLANNING → RENDERING → PACKAGED → REVIEW_PENDING → PUBLISHED
분기: NO_CONTENT (정상 종료) / FAILED / RETRY_QUEUED
```

`REVIEW_PENDING`을 거치지 않고 `PUBLISHED`에 도달하는 경로는 상태 기계 자체에
존재하지 않는다. 테스트로도 고정해 두었다.

| 단계 | 하는 일 | 문서 |
|---|---|---|
| PARSING | 음성/무음/라우드니스/화면 변화 측정, 캐시 | 5.2, 20장 |
| UNDERSTANDING | **1차 전 구간 통과 + 2차 정밀 통과**, 사건 그래프 | 5.1, 5.4 |
| DISCOVERING | 방송 안에 어떤 콘텐츠가 있는가 (0개 허용) | 6장 |
| EVALUATING | 제작 / 결합 / 보류 / 제작안함 + 사유 | 6.3 |
| PLANNING | 구조 결정 → 장면 검색 → 호흡 설계 → 편집 계획 JSON | 7~9장 |
| RENDERING | 편집 계획만 읽고 실행. 판단 없음 | 10장 |
| PACKAGED | 썸네일 후보 추출, 제목/설명/태그/챕터 | 11장 |
| REVIEW_PENDING | 사람 검수 게이트 (필수) | 11.3 |

1차 통과가 실행 비용의 대부분이다 — 6시간 방송이면 창마다 추론 1회. 실측하니
**181회**였다. 뒷단계에서 실패했다고 그 값을 다시 치르면 16장의 단계 분리가
의미가 없다. `aicut resume <project>`는 저장된 창 요약과 사건 그래프를 재사용하고
그 뒤만 다시 판단한다 — 프로파일을 다시 재거나 사람 판단이 바뀌면 결과가 달라져야
하므로. 30분 소스 실측: 56초 걸리던 재실행이 **0.37초**. **6시간 소스 실측:
254초가 2.4초** (창 181개·사건 3개·발화 4,976개를 그대로 재사용).

조작 화면은 `aicut ui` (15.1의 4단계 플로우: 입력 / 진행 / 후보 검토 / 결과).
localhost 전용이고 인증이 없다 — 포트를 외부에 열지 말 것.

1차 통과는 **화면 전환 감지로 대체하지 않는다.** 게임 화면이 30분간 그대로여도
그 안에서 일이 벌어지므로, 전 구간을 빠짐없이 통과한다 (5.1).
각 창은 앞선 창들의 기억을 들고 읽히기 때문에 03:41의 발언이 00:32의 사건을
가리킨다는 것을 알아본다 (5.4).

---

## 데이터 모델의 핵심 (13.1)

에피소드는 **원본의 시간 구간이 아니다.** `start_sec`/`end_sec` 컬럼이 없다.

```
TB_EPISODE (start_sec / end_sec 없음)
  └ TB_EDIT_TIMELINE
      sequence_order    ← 완성본에서의 순서
      source_start_sec  ← 원본에서의 위치 (순서와 무관)
      pacing_mode       ← KEEP / TRIM / CUT
```

에피소드를 연속 구간으로 저장하는 순간 2.4(비선형 재구성)와
5.4(멀리 떨어진 장면 연결)가 표현 불가능해지기 때문이다.

---

## 스마트 페이싱 (9장)

같은 길이의 정적이 정반대의 의미를 가질 수 있다. 길이만으로는 구분되지 않으므로
맥락을 읽는다 — 직전 텐션, 화자 전환 대기 여부, 화면 속 인물의 정지 여부,
그 컷이 편집 계획에서 부여받은 역할.

```
KEEP  황당해서 말을 잇지 못하는 구간, 반박 직전의 숨, 화자 전환 대기
TRIM  애매한 구간 — 숨은 남기고 압축
CUT   파밍·이동·자리비움 — 통째로 제거
```

가중치와 임계값은 전부 프로파일에 있다. 판정은 규칙 계층이 점수를 내고,
추론 계층이 뒤집을 수 있으며(`decided_by`에 기록),
**사람이 만든 완성본과 대조해 채점하지 않은 페이싱은 신뢰하지 않는다** (9.4 → 17.3).

---

## 기획안 v1이 지적한 기술적 오류 3건 (10.4)

1. **얼굴 추적 줌** — `crop=...:x=face_center_x` 는 동작하지 않는다.
   `face_center_x`는 ffmpeg 내장 변수가 아니고, crop은 프레임마다 좌표를
   자유롭게 바꾸지 못한다. → `segment_crop`(구간별 고정 crop + concat)과
   `sendcmd`(시간축 좌표 변화) 두 전략을 구현하고, 선택은 프로파일 파라미터로 뒀다.
   MVP 6에서 실측 후 확정한다.
2. **컷 연결부** — 수백 개 컷마다 `acrossfade`를 쓰면 필터 그래프가 폭발한다.
   → 컷 단위 수 ms `afade` in/out + `concat`.
3. **라우드니스** — EBU R128 정규화는 유지하되 2-pass(측정 후 적용).
   1-pass는 구간별 레벨이 흔들린다.

---

## 3종 학습 루프 (12.3)

| 루프 | 입력 | 코드 |
|---|---|---|
| A. 레퍼런스 | 유튜브 영상의 **공개 지표**와 메타데이터 | `intelligence/reference.py` |
| B. 원본↔완성본 | 내 방송 원본 + 사람이 만든 완성본 | `intelligence/source_output.py` |
| C. 성과 | 내 채널의 유지율·이탈 구간 | `pipeline/performance.py` |

- 유지율·평균 시청 지속 시간·CTR은 **자기 채널에서만** 조회 가능하다.
  타 채널은 조회수/좋아요/댓글 수가 한계다 (4.2). API 표면이 이 구분을 강제한다.
- 레퍼런스 원본 미디어는 저장하지 않는다. 스키마에 그럴 컬럼이 없다 (4.6).
- **B가 이 시스템의 핵심 차별점이다.** B가 없으면 규칙 엔진에 머문다.
  같은 작업이 17.2 캘리브레이션 데이터셋을 겸한다.

---

## 캘리브레이션 (17장)

```bash
aicut profile                                   # 지금 무엇이 추측값인지 확인
aicut calibrate --init --channel mychannel      # 17.4 1단계: 내 방송의 실제 레벨 분포에서 시작값 측정

# 17.2 데이터셋 — 이 프로젝트의 병목
aicut dataset init ds.json --source stream.mkv --transcript stream.json
aicut dataset add-content ds.json --start 01:12:30 --end 01:19:05 --note "보스전"
aicut dataset derive-silences ds.json --output-transcript 완성본.json

aicut calibrate --dataset ds.json --channel mychannel    # 하네스 직접 안 써도 된다
aicut profile --list                                     # 무엇을 언제 측정했나
```

전체 절차는 `docs/calibration.md`.

스윕이 측정한 파라미터는 `measured`로 승격되고 더 이상 경고를 띄우지 않는다.
측정하지 않은 형제 파라미터는 계속 추측값으로 남는다.
프로파일은 채널 단위다 — 마이크·게임·합방 여부가 바뀌면 다시 측정한다.

---

## 구현되지 않은 것

정직하게 적는다.

- **UI 인증** — `aicut ui`는 localhost 전용이고 인증이 없다. 요청 본문 크기는
  4MB로 제한하지만, 포트를 외부에 열면 누구나 조작할 수 있다. 열지 말 것.
- **PyQt6 / Electron 래퍼 (20.1)** — 15장의 네 화면은 `aicut ui`로 구현되어 있으나,
  전달 방식이 기획안이 적은 데스크톱 래퍼가 아니라 로컬 HTTP 서버 + 정적 페이지다.
  헤드리스에서 실제로 돌고 테스트되기 때문에 이 방식을 택했다.
  PyQt6 `QWebEngineView`나 Electron 셸이 같은 서버를 감싸면 UI 로직 변경 없이
  22.1의 "단일 실행 가능한 데스크톱 프로그램"이 된다.
- **얼굴 인식 정밀도** — OpenCV 4.x면 Haar cascade, `AICUT_FACE_MODEL`에 YuNet
  `.onnx`를 주면 DNN (`aicut run --frames`).
  "얼굴이 화면을 얼마나 채우는가" 수준의 거친 질문에만 답한다.
  OpenCV가 없으면 토크/게임 구분은 `UNKNOWN`으로 남는다 — 추측하지 않는다.
  표정 변화는 얼굴 박스의 이동·크기 변화로 대신한다(11.1). 랜드마크 모델 아님.
- **웃음/비명 분류기** — 학습된 분류기가 아니다. "이 방송 자체의 발화 레벨 대비
  크고, 그 아래 받아쓰인 단어가 거의 없다"는 두 신호로 판정한다 (`analysis/vocalburst.py`).
  웃음·비명·환호는 크고 단어가 없고, 흥분한 발화는 크고 단어가 많다는 구분이다.
  거칠다 — 길게 지르는 문장은 놓치고 큰 기침은 잡는다. 숨기지 않고 적어 둔다.
  진짜 분류기는 `VocalBurstDetector` 뒤에 그대로 갈아 끼운다.
- **실측 대기** — MVP 8(쿼터 증량 승인)과 20.2의 **GPU** STT 시간은 실제 계정·하드웨어가
  필요하다. 나머지 실측은 `docs/measurements.md`에 끝냈다: 처리 시간(R3),
  **6시간 원본 실측**(외삽 아님 — 신호 추출 6.6분, 편집 계획까지 4.2분, 최대 RSS 74MB),
  렌더 비용, 10.4 줌 전략 비교, 입력 검증, 실제 인식기 STT, ffmpeg 빌드 차이,
  플랫폼별 실행 결과. 자기 장비 숫자는 `aicut benchmark <원본>`으로 잰다.

  6시간 원본을 실제로 통과시킨 것이 짧은 픽스처로는 나올 수 없는 버그를 잡았다:
  장면 길이 상한이 없어 6시간 발화가 장면 **하나**가 되고, 편집 계획이 원본
  3,830,063초(44일)에 자막 828,949줄로 나왔다. 규모는 직접 통과시켜야 보인다.

---

## 개발

```bash
python -m unittest discover -s . -p "test_*.py"    # 323 tests, 커버리지 90%
```

ffmpeg가 없으면 미디어를 만지는 51개가 스스로 건너뛰고 나머지는 그대로 돈다.
CI가 ffmpeg를 **숨긴 채** 한 번 더 돌려서 그 주장을 검증한다 — GitHub 러너에는
ffmpeg가 기본으로 깔려 있어서, 숨기지 않으면 이 잡은 아무것도 증명하지 못한다. 합성 방송 픽스처(`tests/fixtures.py`)로 파이프라인
전체를 오프라인 실행한다: 한 시간 떨어진 두 시점을 잇는 사건, 잘라야 할
자리비움, 지켜야 할 정적이 들어 있다.

나머지 29개(`test_render_live.py`, `test_pipeline_live.py`)는 **실제로 ffmpeg를
돌린다.** ffmpeg가 없으면 건너뛴다. 다른 테스트는 렌더러가 *만드는 명령*을
검사하지만, 이쪽은 그 명령이 실제 파일에 무슨 짓을 하는지 검사한다:

- `remove_spans`가 정말 파일에서 빠졌는가 (길이로 확인)
- 계획 순서대로 렌더되는가 — 완성본 첫 프레임이 원본 뒷부분과 일치하는지 PSNR로 대조
- 자막이 정말 태워졌는가 (자막 구간과 비자막 구간의 PSNR 차이)
- 2-pass 라우드니스가 목표치에 닿는가
- 줌이 픽셀을 실제로 옮기는가 (파싱만 되는 게 아니라)
- sendcmd 팬이 시간에 따라 화면을 옮기는가
- `방송_2026-08-19 [하이라이트].mkv` 같은 경로에서 자막이 태워지는가 —
  ASS 경로는 ffmpeg 필터 문자열로 들어가고 거기선 `:`와 따옴표가 문법이다
- 그리고 파이프라인 전체: 실제 다중트랙 파일을 넣어 컨테이너 판독, 무음 검출,
  버스트 검출, 렌더, 썸네일, 메타데이터까지 스스로 하게 두고 결과를 검사한다

YouTube API와 추론 프로바이더는 가짜 클라이언트로 검증한다 — 네트워크 없이
쿼터 소진, PT 자정 재시도, 업로드 요청 본문(제목 100자·태그 30개 컷),
재시도 백오프, 응답 파싱까지 실제로 실행된다.

라이브·가짜 클라이언트 층이 잡은 것:

1. concat 목록의 상대경로가 목록 파일 위치 기준으로 다시 풀려 경로가 중복됨.
   인자 배열만 보면 멀쩡해 보이는 종류.
2. sendcmd로 crop `w`/`h`를 바꾸면 필터 그래프가 **교착**된다 (ffmpeg 7.1 실측:
   벽시계 60초, CPU 0.5초, 출력 0바이트). 그래서 sendcmd 전략은 고정 크롭 크기로
   **팬만 한다**. 배율이 변하는 줌은 `segment_crop`이 담당한다.
   scale 키프레임이 섞여 들어오면 조용히 무시하지 않고 경고하며 평탄화한다.
3. `INSERT OR REPLACE`가 SQLite에서 기존 행을 삭제 후 삽입하므로
   `ON DELETE CASCADE`가 자식 행을 같이 지운다 — 에피소드를 저장할 때마다
   성과 데이터(루프 C)와 대기 중인 업로드가 조용히 사라지고 있었다. upsert로 교체.
4. 쿼터 재시도가 실패할 때마다 큐에 **중복 행**을 쌓았다. 에피소드당 한 행으로 고정.
5. 배열 응답 앞에 문장이 붙으면 JSON 추출이 안쪽 객체만 뽑아 배열이 잘렸다.
6. 프로파일이 **아무도 안 읽는 파라미터 4개**를 광고하고 있었다. 17.1이
   "판정 기준은 프로파일에" 라고 한 이상, 읽히지 않는 손잡이는 거짓말이다.
7. **설치하면 아예 안 돌아갔다.** 캘리브레이션 프로파일·자막 스타일·UI 페이지가
   패키지 밖에 있어서 `pip install`이 안 실어감. 저장소 안에서만 동작하는 프로그램이었다.
   `aicut/resources/`로 옮기고, 설치본을 실제로 만들어 저장소 밖에서 실행하는
   테스트를 뒀다.
8. 편집 계획의 `source_path`가 상대경로로 저장됐다. 다른 디렉터리에서
   `aicut render <plan>` 하면 원본을 못 찾는다 — 16장의 "렌더만 재실행"이 깨진다.
   1번과 같은 부류: 상대경로가 다른 문맥에서 다시 풀린다.
9. `TB_CALIBRATION_PROFILE`(13장)에 **아무것도 안 쓰이고 있었다.** 캘리브레이션
   결과가 파일로만 남고 DB엔 기록 안 됨. 이제 `aicut calibrate`가 기록하고
   `aicut profile --list`로 조회한다.

`tests/test_consistency.py`가 이 부류를 구조적으로 막는다: 안 읽히는 프로파일 키,
프롬프트 없는 판단 태스크, 목 핸들러 없는 태스크, 아무것도 안 쓰는 테이블,
도달 불가능한 상태, 호출자 없는 공개 함수, 부모 행에 대한 `INSERT OR REPLACE` —
전부 테스트가 실패시킨다. 전부 실제로 이 저장소에 있었던 것들이다.
