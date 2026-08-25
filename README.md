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

## 설치

```bash
pip install -e .                 # 코어는 표준 라이브러리 + ffmpeg CLI만 필요
pip install -e '.[stt,vision,llm]'   # 단계별 선택 설치
aicut doctor                     # 20.2 사전 준비 항목 점검
```

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
- **실측 대기** — MVP 8(쿼터 증량 승인)과 20.2의 GPU STT 시간은 실제 계정·하드웨어가
  필요하다. 나머지 실측은 `docs/measurements.md`에 끝냈다: 처리 시간(R3),
  10.4 줌 전략 비교, 입력 검증. 자기 장비 숫자는 `aicut benchmark <원본>`으로 잰다.

---

## 개발

```bash
python -m unittest discover -s . -p "test_*.py"    # 238 tests, 커버리지 90%
```

209개는 ffmpeg 없이 돈다. 합성 방송 픽스처(`tests/fixtures.py`)로 파이프라인
전체를 오프라인 실행한다: 한 시간 떨어진 두 시점을 잇는 사건, 잘라야 할
자리비움, 지켜야 할 정적이 들어 있다.

나머지 18개(`test_render_live.py`, `test_pipeline_live.py`)는 **실제로 ffmpeg를
돌린다.** ffmpeg가 없으면 건너뛴다. 다른 테스트는 렌더러가 *만드는 명령*을
검사하지만, 이쪽은 그 명령이 실제 파일에 무슨 짓을 하는지 검사한다:

- `remove_spans`가 정말 파일에서 빠졌는가 (길이로 확인)
- 계획 순서대로 렌더되는가 — 완성본 첫 프레임이 원본 뒷부분과 일치하는지 PSNR로 대조
- 자막이 정말 태워졌는가 (자막 구간과 비자막 구간의 PSNR 차이)
- 2-pass 라우드니스가 목표치에 닿는가
- 줌이 픽셀을 실제로 옮기는가 (파싱만 되는 게 아니라)
- sendcmd 팬이 시간에 따라 화면을 옮기는가
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
7. `TB_CALIBRATION_PROFILE`(13장)에 **아무것도 안 쓰이고 있었다.** 캘리브레이션
   결과가 파일로만 남고 DB엔 기록 안 됨. 이제 `aicut calibrate`가 기록하고
   `aicut profile --list`로 조회한다.

`tests/test_consistency.py`가 이 부류를 구조적으로 막는다: 안 읽히는 프로파일 키,
프롬프트 없는 판단 태스크, 목 핸들러 없는 태스크, 아무것도 안 쓰는 테이블,
도달 불가능한 상태, 호출자 없는 공개 함수, 부모 행에 대한 `INSERT OR REPLACE` —
전부 테스트가 실패시킨다. 전부 실제로 이 저장소에 있었던 것들이다.
