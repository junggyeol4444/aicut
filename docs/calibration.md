# 캘리브레이션 실행 절차 (17장)

기본 프로파일의 숫자는 전부 추측이다. 이 문서대로 한 번 돌리기 전까지
모든 실행 리포트가 "미측정 파라미터에 기대고 있다"고 말한다. 그게 정상이다.

필요한 것은 두 가지, 17.2가 요구하는 그대로다:

1. 본인 방송 원본 하나
2. **그 방송으로 사람이 실제로 만든 완성본**

2번이 병목이다. 없으면 스윕은 채점할 정답이 없다.

---

## 1. 원본을 한 번 통과시킨다

```bash
aicut run stream.mkv --transcript stream.json --no-render --stop-after UNDERSTANDING
```

신호(무음·라우드니스·화면변화)가 캐시된다. 이후 스윕은 이 캐시를 재생만 하므로
조합마다 원본을 다시 디코딩하지 않는다.

## 2. 데이터셋을 만든다

```bash
aicut dataset init ds.json \
  --source stream.mkv --transcript stream.json --channel mychannel
```

## 3. 사람이 콘텐츠라고 부를 구간을 표시한다 (17.2 c)

원본을 보면서, "이건 영상으로 만든다" 싶은 구간을 적는다.
타임스탬프는 `91.5`, `1:31.5`, `01:12:30` 다 받는다.

```bash
aicut dataset add-content ds.json --start 01:12:30 --end 01:19:05 --note "보스전 11번째 도전"
aicut dataset add-content ds.json --start 03:40:10 --end 03:52:00 --note "합방 중 사건"
```

이게 17.3의 "콘텐츠 발견 일치도"와 "오탐 비율"의 정답지가 된다.

## 4. 호흡 판정은 완성본에서 뽑는다 (12.3 B)

수백 개 정적을 손으로 표시하지 않는다. 사람이 만든 완성본의 트랜스크립트를 주면
시스템이 유도한다.

```bash
aicut dataset derive-silences ds.json --output-transcript finished.json
```

판정 근거는 **간격이 얼마나 살아남았는가**다. 정적 양옆 발화가 둘 다 완성본에
들어갔다면, 완성본에서의 간격과 원본에서의 간격을 비교한다. 대부분 남았으면
사람이 그 '마'를 지킨 것이고, 무너졌으면 자른 것이다. 한쪽 발화가 통째로
잘려나갔으면 그 정적도 같이 잘린 것이다.

출력 예:

```
   194.0-  214.0 KEEP 62% of the gap survived the human edit
   291.0-  311.0 cut  30% of the gap survived the human edit
     8.0-   20.0 cut  the editor dropped the material on one side of this pause
```

## 5. 스윕 (17.4)

```bash
aicut calibrate --dataset ds.json --channel mychannel
```

하네스를 직접 쓸 필요 없다. 내장 재생기가 후보 프로파일마다 판정 계층을 다시
돌리고 17.3 지표로 채점한다. `--grid`로 조합을 지정하지 않으면 기본 격자
(무음 레벨, 호흡 임계값, 텐션 고점)를 쓴다.

결과: 최고 점수 조합이 채널 프로파일로 저장되고, **측정된 파라미터만**
`measured`로 승격된다. 나머지는 계속 추측값으로 남아 리포트에 그렇게 적힌다.

```bash
aicut profile --list                      # 무엇을 언제 측정했는지
aicut --profile profiles/mychannel-calibrated.json run 다음방송.mkv
```

## 6. 환경이 바뀌면 다시 잰다 (17.4 4단계)

마이크, 게임, 합방 여부가 바뀌면 값도 달라진다. 프로파일은 채널 단위이고
영구적이지 않다.

---

## 데이터셋이 없을 때

돌아는 간다. 다만 모든 판정이 근거 없는 숫자 위에 서 있고, 리포트가 매 실행마다
그 목록을 출력한다. `--strict`를 켜면 미측정 파라미터를 읽는 순간 실행을 거부한다.
운영 전환 전에 한 번은 이 문서를 끝까지 돌려야 한다.
