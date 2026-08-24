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
aicut calibrate --dataset ds.json --grid grid.json --harness eval.py --channel mychannel
```

스윕이 측정한 파라미터는 `measured`로 승격되고 더 이상 경고를 띄우지 않는다.
측정하지 않은 형제 파라미터는 계속 추측값으로 남는다.
프로파일은 채널 단위다 — 마이크·게임·합방 여부가 바뀌면 다시 측정한다.

---

## 구현되지 않은 것

정직하게 적는다.

- **GUI (15장)** — CLI로만 조작한다. 15.2~15.5의 화면은 미구현이며,
  대신 같은 정보를 `aicut candidates` / `aicut plan` / `report.json`이 제공한다.
- **얼굴/표정 인식** — 화면 상황 판정(5.3)에서 토크/게임 구분은 얼굴 신호가
  주입되지 않으면 `UNKNOWN`으로 남긴다. 추측하지 않는다.
- **웃음/비명 검출기** — 텐션 계산의 laughter 항은 검출기가 없으면 0점을 주는
  대신 가중치를 재분배한다.
- **실측 대기** — MVP 6(10.4 줌 전략 선택), MVP 8(쿼터 증량 승인),
  20.2(GPU에서의 6시간 원본 처리 시간)은 실제 하드웨어·계정에서 측정해야 한다.
  이 저장소에는 그 측정을 돌릴 코드까지만 있다.

---

## 개발

```bash
python -m unittest discover -s . -p "test_*.py"    # 71 tests, ffmpeg 불필요
```

테스트는 합성 방송 픽스처(`tests/fixtures.py`)로 파이프라인 전체를 오프라인
실행한다: 한 시간 떨어진 두 시점을 잇는 사건, 잘라야 할 자리비움,
지켜야 할 정적이 들어 있다.
