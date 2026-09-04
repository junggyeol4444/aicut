# 편집 계획 (Edit Plan) 스키마

편집 계획은 판단 계층과 실행 계층의 유일한 계약이다 (8.2, 10.1).
PLANNING이 이 파일을 쓰고 멈추고, RENDERING은 이 파일만 읽는다.

- 렌더링이 실패해도 계획은 남는다 (16장) → `aicut render <plan.json>`으로 렌더만 재실행
- 사람이 열어서 판단에 반대하고 고칠 수 있어야 한다 (22.5)
- MVP 5의 합격 기준: **계획만 읽고도 결과물을 예상할 수 있는가**

```jsonc
{
  "schema_version": "1",
  "episode_id": "…",
  "source_path": "/media/stream.mkv",
  "target_type": "long",                  // AI가 고른 형태. 고정 카테고리 아님
  "planned_duration_sec": 184.5,
  "structure": {                          // 7장: 이 콘텐츠에만 해당하는 구조
    "structure_name": "result_first",
    "rationale": "결과를 먼저 보여주고 원인으로 되돌아간다",
    "length_note": "힌트는 60초였으나 사건이 끝나지 않는다",
    "beats": [ { "role": "result", "intent": "…", "query": "…" } ]
  },
  "provenance": {
    "profile": "mychannel-calibrated",
    "provisional_parameters_used": ["silence.level_db"],   // 17.5
    "producer": "anthropic"
  },
  "cuts": [
    {
      "sequence_order": 0,                // 완성본에서의 순서
      "source_start_sec": 18355.2,        // 원본에서의 위치 (순서와 무관)
      "source_end_sec": 18402.8,
      "speaker_tag": "HOST",
      "scene_role": "result",
      "pacing_mode": "KEEP",              // KEEP / TRIM / CUT (9.3)
      "pacing_reason": "3 silences: 2 kept as beats, 1 compressed",
      "remove_spans": [[18380.1, 18382.4]],   // 컷 내부에서 실제로 제거할 구간
      "visual_effect": { "type": "zoom", "scale": 0.83, "center": [0.62, 0.41] },
      "audio_effect": { "gain_db": 0 },
      "subtitle_ref": null
    }
  ],
  "subtitles": [
    { "start_sec": 0.0, "end_sec": 2.4, "text": "…", "speaker": "HOST", "emphasis": false }
  ]
}
```

## 규칙

- `sequence_order`는 중복될 수 없고, `source_end_sec > source_start_sec`이어야 하며,
  `remove_spans`는 반드시 해당 컷 내부에 있어야 한다. 위반하면
  `PlanValidationError`가 어느 컷인지 지목한다.
- 자막 시간은 **완성본 시계** 기준이다. 원본 시각과의 변환은
  `render/timeline.py: Timeline`이 담당한다 (제거 구간을 건너뛴다).
- `visual_effect.type == "zoom"`일 때 `keyframes`를 넣으면
  `render.zoom.strategy == "sendcmd"`인 프로파일에서 시간축 카메라 워크가 된다.
  기본값 `segment_crop`은 구간별 고정 crop이다 (10.4-1).
