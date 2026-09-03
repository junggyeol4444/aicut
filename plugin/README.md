# aicut 편집기 플러그인

`aicut`은 편집기 없이 혼자 완성 영상까지 만든다. 이 폴더는 그 결과를
**렌더링하지 않고 편집기 타임라인으로 받는 쪽**이다 — 컷은 AI가 정하고,
마무리는 사람이 자기 편집기에서 한다.

두 갈래가 있다. 편집기를 고르기 전에 이걸 먼저 봐라.

| 방법 | 대상 | 설치 | 검증 상태 |
|---|---|---|---|
| `aicut export` (교환 파일) | Premiere Pro, Final Cut Pro, Resolve, Avid 등 **전부** | 불필요 | 실제 영상의 계획으로 생성·검증 완료 |
| `plugin/resolve` (스크립트) | DaVinci Resolve 전용 | 파일 2개 복사 | 산술은 테스트됨, **Resolve API 호출은 미검증** |
| `plugin/premiere` (스크립트) | Premiere Pro 전용 | 파일 2개 복사 | 산술은 테스트됨, **Premiere API 호출은 미검증** |

---

## 1. 어느 편집기든 — `aicut export`

플러그인이 필요 없는 쪽이다. 편집 계획을 EDL / FCPXML / SRT로 쓴다.

```bash
aicut run stream.mkv --no-render          # 계획까지만
aicut export workspace/<project>/plans/<episode>.json --format fcpxml --format srt
```

* `--fps` 를 주지 않으면 계획의 렌더 설정을 따른다. 편집기 시퀀스의 프레임
  레이트와 **반드시** 같아야 한다. 다르면 모든 컷이 조금씩 밀린다.
* FCPXML은 원본의 실제 해상도를 따로 선언한다. 원본을 읽을 수 없는 곳에서
  내보내면 그 사실을 출력에 적고, 편집기에서 클립이 늘어나 보이면 relink 하면 된다.
* EDL은 컷만 옮긴다. 자막·크롭·라우드니스는 EDL 포맷이 담지 못한다 —
  자막은 `--format srt` 로 따로 받고, 나머지는 `aicut render` 쪽에만 있다.
* 정직하게 옮길 수 있는 타임코드가 없는 프레임 레이트는 EDL 생성을 **거부**한다.
  틀린 타임코드를 내보내는 것보다 낫다.

---

## 2. DaVinci Resolve — `plugin/resolve`

교환 파일이 아니라 Resolve 안에서 직접 타임라인을 만든다. 원본을 미디어 풀에
넣고, 계획 순서대로 클립을 얹고, 옆에 있는 `.srt`를 자막 트랙으로 올린다.

### 설치

`plugin/resolve` 폴더의 두 파일을 Resolve의 스크립트 폴더에 복사한다.

```
Windows  %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
macOS    ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility
Linux    ~/.local/share/DaVinciResolve/Fusion/Scripts/Utility
```

`aicut_plan.py`도 같이 복사해야 한다. 결정은 전부 그쪽에 있다.

### 사용

1. Resolve에서 프로젝트를 열고, **프로젝트 설정의 프레임 레이트를 먼저 정한다.**
   스크립트는 계획이 아니라 프로젝트의 프레임 레이트로 타임라인을 만든다.
2. `Workspace > Scripts > aicut_resolve`
3. 편집 계획 `.json` 을 고른다 (`workspace/<project>/plans/<episode>.json`)

자막을 같이 원하면 미리 만들어 둔다. 스크립트는 계획 파일 옆의 같은 이름
`.srt` 를 찾는다:

```bash
aicut export workspace/<project>/plans/<episode>.json --format srt
```

터미널에서 바로 돌려도 된다:

```bash
python aicut_resolve.py workspace/<project>/plans/<episode>.json
```

### 이 스크립트가 지키는 것

* **계획 순서** (2.4). 원본 시간순이 아니라 `sequence_order` 순이다. 시간순으로
  다시 정렬해 버리면 AI가 고른 구조가 조용히 사라진다.
* **컷 안에서 버린 구간** (9.3). `remove_spans`가 있는 컷은 클립 하나가 아니라
  여러 개로 쪼개져 들어간다. 무시하면 aicut이 잘라낸 공백이 그대로 재생된다.
* **`endFrame`은 마지막 프레임이지 그 다음 프레임이 아니다.** 여기서 1이 어긋나면
  타임라인의 모든 컷이 한 프레임씩 길거나 짧아지고, 내보내기 전까지 아무도 모른다.
* **한 프레임보다 짧은 구간**은 버리되 조용히 버리지 않고 몇 초짜리였는지 출력한다.

### 검증 상태 — 읽고 넘어가라

이 저장소가 만들어진 환경에 **Resolve가 설치되어 있지 않다.** 그래서 코드를
둘로 쪼갰다:

* `aicut_plan.py` — Resolve를 import하지 않는다. 초→프레임, 컷 순서,
  `remove_spans` 분할, 짧은 구간 처리, 계획 로딩·오류 메시지. `tests/test_plugin_resolve.py`
  가 이걸 전부 테스트하고, 그중 하나는 실제 직렬화된 계획을 통과시켜
  플러그인의 `kept_spans`가 aicut 본체의 `Cut.kept_spans`와 같은 답을 내는지
  대조한다. 두 벌로 갈라진 구현은 반드시 어긋나기 때문이다.
* `aicut_resolve.py` — `CreateTimelineFromClips`, `AddItemListToMediaPool`,
  `ImportIntoTimeline` 등 Resolve API 호출만 있다. **이 호출들은 실행된 적이 없다.**
  Blackmagic 스크립팅 문서를 보고 쓴 것이다. 첫 실행이 곧 테스트다.

실제 영상(Big Buck Bunny)의 편집 계획으로 클립 리스트 생성까지는 돌려 봤다:

```
30 fps     : 8 clips, 33.6s, dropped 0   (frames 676-883, 0-13, 42-61, ...)
23.976 fps : 8 clips, 33.6s, dropped 0   (frames 540-706, ...)
```

여기까지가 확인된 부분이고, 그 리스트를 Resolve에 넘기는 마지막 한 줄은 확인되지 않았다.

### 잘 안 될 때

| 증상 | 원인 |
|---|---|
| `this script must be run from inside DaVinci Resolve` | Resolve 메뉴가 아니라 밖에서 돌렸고 `DaVinciResolveScript`가 경로에 없다 |
| `Resolve is not running, or scripting is disabled` | 환경설정 > System > General 의 외부 스크립팅을 켠다 |
| `Resolve refused to build the timeline` | 클립과 프로젝트의 프레임 레이트가 다르다 |
| `the plan's source is not at ...` | 원본을 옮겼다. 되돌리거나 새 위치로 다시 계획을 만든다 |
| 자막이 안 들어감 | 계획 옆에 `.srt`가 없다. `aicut export --format srt` |

---

## 3. Premiere Pro — `plugin/premiere`

ExtendScript 파일이다. **CEP 확장이 아니다** — ZXP도, 서명 인증서도, 확장 관리자도
필요 없다. 파일 두 개 복사하면 설치 끝이다.

### 설치

```
Windows  %APPDATA%\Adobe\Premiere Pro\<버전>\Scripts
macOS    ~/Documents/Adobe/Premiere Pro/<버전>/Scripts
```

`aicut_premiere.jsx`와 `aicut_plan.js` 둘 다 복사해야 한다. 판단은 `.js` 쪽에 있다.

### 사용

1. 프로젝트를 열고 **빈 시퀀스를 하나 만든다.** 스크립트는 활성 시퀀스에,
   그 시퀀스의 프레임 레이트로 넣는다.
2. `File > Scripts > aicut_premiere`
3. 편집 계획 `.json` 을 고른다

첫 비디오 트랙에 클립이 이미 있으면 **거부한다.** 남의 편집 위에 조용히
얹는 것보다 시퀀스 하나 새로 만드는 편이 싸다.

자막은 계획 옆의 `.srt`를 프로젝트로 임포트한다. 캡션 트랙에 얹는 건
스크립팅 API에 없어서 손으로 끌어야 한다.

### 이 스크립트가 지키는 것

Resolve 쪽과 같다 — 계획 순서(2.4), `remove_spans` 분할(9.3), 한 프레임 미만
구간 보고. 더해서 Premiere 특유의 두 가지:

* **틱**. Premiere는 초도 프레임도 아닌 틱(초당 254,016,000,000)으로 센다.
  6시간이면 5.5e15라 double이 1 단위로 못 세는 영역이고, 그래서 Premiere의
  `Time.ticks`도 문자열이다. 이쪽도 문자열로 낸다.
* **프레임 그리드 스냅**. 생초로 넣으면 클립마다 한 프레임 미만의 틈이 남고,
  긴 타임라인 끝에 가면 누구도 못 찾는 어긋남으로 쌓인다.

### 검증 상태

Resolve와 같은 이유로 같은 방식이다. Premiere Pro가 이 환경에 없다.

* `aicut_plan.js` — Premiere를 건드리지 않는다. node로 돌고,
  `tests/test_plugin_premiere.py`가 그 실제 파일을 node로 실행해서 검사한다
  (23개). 파이썬으로 다시 구현해서 비교하면 두 복사본이 서로 같다는 것만
  증명되므로 그렇게 하지 않았다. 하나는 실제 직렬화된 계획을 통과시켜
  `keptSpans`가 `Cut.kept_spans`와 같은 답을 내는지 대조한다.
* `aicut_premiere.jsx` — `insertClip`, `setInPoint`, `importFiles` 등 API 호출만.
  **실행된 적 없다.** Adobe ExtendScript 문서를 보고 썼다.

첫 실행에서 확인할 것 두 가지, 산술에 묻지 않고 상수로 빼놨다:

* `OUT_POINT_IS_EXCLUSIVE` (`aicut_plan.js`). 모든 클립이 한 프레임씩 짧으면 이것 때문이다.
* 빈 트랙 요구. `insertClip`이 실제로 어떻게 동작하든 남의 클립을 밀지 않는다.

FCPXML 경로(`aicut export --format fcpxml`)는 Premiere에서도 되고 그쪽은 검증됐다.
스크립트가 안 맞으면 그걸 쓰면 된다.
