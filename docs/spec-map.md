# 기획안 ↔ 코드 대응표

통합 기획안 v1의 각 장이 어디에 구현되어 있는지, 그리고 문서가 명시적으로
금지하거나 수정한 사항이 코드의 어느 지점에서 강제되는지 적는다.

## 1~2장 — 설계 철학

| 원칙 | 강제 지점 |
|---|---|
| 2.1 영상 구조 하드코딩 금지 | `llm/prompts.py` `plan_structure`, `TB_EPISODE.planned_structure` |
| 2.2 콘텐츠 개수 하드코딩 금지 | `pipeline/runner.py` `_no_content()`, `State.NO_CONTENT` |
| 2.3 콘텐츠 종류 하드코딩 금지 | `SituationLabel`은 내부 신호. `Episode.target_type`은 자유 문자열 |
| 2.4 시간순 편집 강제 금지 | `TB_EDIT_TIMELINE.sequence_order` ≠ `source_start_sec`, `render/timeline.py` |
| 2.5 화면이 아니라 사건으로 분할 | `pipeline/discovery.py` — 사건 앵커 없는 후보는 폐기 |
| 2.6 길이 하드코딩 금지 | `planning._note_length_deviation()` → 리포트의 `length_deviations` |

## 4장 — YouTube Content Intelligence

- 4.1 좁게 시작 → `intelligence/reference.py: DEFAULT_QUERIES`
- 4.2 **사실관계**: 유지율/CTR은 자기 채널 한정 → `YouTubeClient.public_metrics()`(타 채널)
  와 `.analytics()` / `.audience_retention()`(`ids="channel==MINE"`)이 분리되어 있다
- 4.5 지식이지 규칙이 아님 → `ProductionKnowledge.summary_for_planner()`가 support/share와
  caveat을 함께 넘긴다
- 4.6 원본 미디어 미보관 → `tb_yt_reference`에 미디어 컬럼이 없다 (테스트로 고정)

## 5장 — Long-Term Broadcast Understanding

- 5.1 1차 전 구간 통과 → `understanding._first_pass()`가 0초부터 끝까지 창을 이어 붙인다
  (테스트: 창 사이에 틈이 없음을 검사)
- 5.1 2차 정밀 통과 → `_second_pass()`, 대상 선정 기준은 `scan.pass2_trigger`
- 5.2 멀티트랙 전제 → `media/probe.py: AudioTrack.role`, `needs_diarization`
- 5.3 상황 라벨 → `analysis/signals.py: label_situations()` (얼굴 신호 없으면 UNKNOWN)
- 5.4 장기 메모리 → `TB_EVENT` + `TB_EVENT_MENTION`, `_memory()`가 누적 문맥을 실어 나른다

## 6장 — Autonomous Content Discovery

- 6.2 사건 기준 분할 → `discovery.run()`이 사건 앵커를 요구
- 6.3 가치 판단 4종 → `Decision`, `evaluating.group_for_production()`이 결합 후보를 병합
- 6.4 경계 감지는 힌트일 뿐 → `analysis/signals.py: boundary_hints()`,
  `boundary_hints.min_hint_count` 이상 겹칠 때만 후보 지점으로 올린다

## 7~8장 — Planning / Retrieval

- 7장 구조 결정 → `producer.plan_structure()`
- 8.1 장면 검색 후 AI 검증 → `pipeline/retrieval.py` (BM25 + 사건/역할 가중) →
  `producer.select_scene()`이 전부 거절할 수도 있다
- 8.2 편집 계획과 렌더링의 완전 분리 → `render/editplan.py`

## 9장 — 스마트 페이싱

- 9.1 두 종류의 정적 → `analysis/pacing.py`
- 9.2 판정 신호 → `SilenceContext` (직전 텐션 / 화자 전환 / 화면 정지 / 컷 역할)
- 9.3 KEEP·TRIM·CUT → `PacingMode`, `TB_EDIT_TIMELINE.pacing_mode`
- 9.4 사람 완성본과 대조 → `calibration/metrics.py: score_pacing()`

## 10장 — 렌더링

- 10.1 렌더러는 판단하지 않음 → `Renderer.render()`는 `EditPlan`만 받는다
- 10.3 자막 스타일 외부화 → `config/subtitle_styles/*.json`
- 10.4-1 crop 표현식 오류 수정 → `render/ffmpeg.py: zoom_filter()`, `sendcmd_file()`
- 10.4-2 acrossfade 오용 수정 → `audio_edge_filter()` + `build_concat_command()`
- 10.4-3 2-pass 라우드니스 → `media/audio.py: measure_loudness()` → `build_final_command()`

## 11장 — 패키징 / 업로드

- 11.1 썸네일 후보 → `render/thumbnails.py` (템플릿 없음, 사람이 고른다)
- 11.3 사람 검수 게이트 → `State`에 REVIEW_PENDING을 거치지 않는 PUBLISHED 경로가 없다.
  `publishing.publish_approved()`는 승인되지 않은 에피소드에 `PermissionError`를 던진다
- 11.4 쿼터 사실관계 수정 → `intelligence/quota.py` (10,000 units / 1,600 units /
  PT 자정 리셋). "24시간 후 재시도"가 아니라 다음 PT 자정을 겨냥한다

## 12장 — 학습

- 12.1 자기 채널 한정 지표 → `performance.collect()`
- 12.3 A/B/C → `intelligence/reference.py`, `intelligence/source_output.py`,
  `pipeline/performance.py`

## 13~14장 — 데이터 모델 / 상태 기계

- 13.1 에피소드는 컷의 순서 있는 집합 → `db/schema.sql`의 `tb_episode`에
  start/end 컬럼이 없다
- 14장 → `pipeline/states.py`

## 16장 — 예외 처리

| 상황 | 처리 |
|---|---|
| 음성 미감지 | 발화 없는 사건도 `SceneIndex`에서 검색 가능 |
| 단일 주제 방송 | 억지 분할 없음 — 사건이 하나면 후보도 하나 |
| 제작 가치 없음 | `NO_CONTENT` 정상 종료 + 사유 리포트 |
| 쿼터 초과 | 로컬 보관 + `tb_upload_queue`에 PT 자정 기준 재시도 등록 |
| 렌더링 실패 | 편집 계획 보존, `aicut render <plan>`으로 렌더만 재실행 |
| 화자 분리 실패 | `UNKNOWN` 태그로 진행, 화자 기반 연출만 비활성 (`speaker_reliability`) |
| 10시간 초과 원본 | `scan.long_source_split_sec` 기준 청크 처리 후 사건 그래프에서 병합 |

## 17장 — 캘리브레이션

- 17.1 전면 외부화 → `config.py`. 프로파일에 없는 값을 읽으면 `ConfigError`
  (코드 기본값으로 대충 넘어가지 않는다)
- 17.5 미측정 값은 확정값이 아님 → `provisional` / `measured` 마킹,
  `touched_provisional()`가 리포트에 실린다, `--strict`는 아예 거부

## 18장 — 담당 경계

`llm/base.py: Producer`의 메서드 목록이 "AI가 담당"의 전부다.
그 밖의 모든 것(디코딩·DB·검색·렌더링·API·큐)은 프로그램이 한다.
