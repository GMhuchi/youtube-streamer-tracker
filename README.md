# 유튜브 스트리머 라이브 트래커 (API 키 불필요)

등록해둔 유튜브 채널들이 지금 방송 중인지 5~10분마다 자동으로 확인하고,
지정한 검색어(태그)로 새로 방송을 시작한 스트리머도 찾아서 한 페이지에
보여주는 완전 무료 트래커입니다.

- **유튜브 공식 API(Data API v3) 키가 전혀 필요 없습니다.** 대신 로그인 없이도
  볼 수 있는 유튜브의 공개 페이지(채널 RSS, `/live` 리다이렉트, 검색결과
  페이지)를 직접 페이지쥼 시그니처를
그대로 유지한 채 내부 구현만 API 호출로 바꾸면 나머지 코드는 그대로
씁니다.

## 화면 미리보기

`docs/index.html` 페이지는:

- 🔴 **지금 방송 중** — 등록 채널 중 라이브인 것만, 시청자 수 높은 순
- **등록 채널 중 오프라인** — 접어둔 목록, 최근 영상 정보 표시
- 🔍 **태그로 새로 발견** — 검색어에 걸린 실시간 방송, 이미 등록된
  채널이면 "등록됨" 표시
- 60초마다 자동 새로고침 (데이터 자체는 Actions가 5~10분마다 갱신)

## 설정 방법

### 1. 저장소 만들고 올리기

GitHub에 새 저장소를 만들고(공개 저장소 권장 — Actions/Pages 완전 무료),
이 폴더 전체를 push 하세요.

```bash
cd youtube-streamer-tracker
git remote add origin https://github.com/<내계정>/<저장소이름>.git
git branch -M main
git add -A
git commit -m "init: 유튜브 스트리머 라이브 트래커"
git push -u origin main
```

### 2. GitHub Pages 켜기

저장소 → **Settings → Pages** → Source를 **Deploy from a branch**로,
Branch를 **main / `/docs`** 로 설정 → Save.
몇 분 뒤 `https://<내계정>.github.io/<저장소이름>/` 에서 페이지가 보입니다.

### 3. Actions 켜기 (필요하면)

저장소 → **Actions** 탭에 들어가서 워크플로 실행을 허용하라는 안내가
보이면 활성화하세요. 그다음 **"스트리머 트래킹"** 워크플로를 열고
**Run workflow** 버튼으로 한 번 수동 실행해보면, 스케줄을 기다리지 않고
바로 `docs/status.json`이 채워집니다.

### 4. 채널 등록

방법 A — 페이지에서: Pages 사이트 우상단 **"＋ 채널 등록"** 버튼 클릭 →
채널 URL/핸들 입력 → 제출. 몇 분 안에 자동으로 처리되어 이슈가 닫히고,
다음 확인 주기부터 목록에 나타납니다.

방법 B — 직접 편집: `data/channels.json`에 아래 형식으로 추가 후 커밋.

```json
{ "id": "UC로시작하는24자리채널ID", "name": "표시할 이름" }
```

채널 ID는 채널 페이지에서 "정보 더보기" 또는 페이지 소스에서
`"channelId":"UC..."` 를 찾아 확인할 수 있습니다. (핸들 `@이름` 은
채널 ID가 아니므로 그대로 쓰면 안 됩니다 — 방법 A를 쓰면 이 변환을
자동으로 해줍니다.)

### 5. 검색 태그(디스커버리) 설정

`data/tags.json`에서 `queries` 목록을 수정하세요. 태그를 너무 많이
넣으면 한 번 검색 사이클이 오래 걸리니 2~4개 정도를 권장합니다.

```json
{
  "discoveryIntervalCycles": 6,
  "queries": [
    { "query": "이터널리턴", "label": "이터널리턴(한국어)" },
    { "query": "エターナルリターン", "label": "エターナルリターン(일본어)" }
  ]
}
```

## ⚠️ 스케줄 자동 실행이 꺼지는 경우

GitHub은 **저장소에 60일 동안 아무 활동(커밋 등)이 없으면 예약된(cron)
워크플로를 자동으로 비활성화**합니다. 이 트래커는 실행될 때마다
`docs/status.json`을 커밋하므로 평소엔 문제가 없지만, 만약 페이지가
오래 멈춰있다면 **Actions 탭 → 스트리머 트래킹 → 우측 "···" → Enable
workflow** 로 다시 켜주면 됩니다.

또한 GitHub Actions의 cron 스케줄은 정확히 그 분에 실행되는 것을
보장하지 않고 트래픽이 몰리면 몇 분 밀릴 수 있습니다 (공식 문서에
명시된 제약입니다). 5~10분 간격 트래킹 목적에는 충분합니다.

## 로컬에서 테스트하기

이 샌드박스/여러분의 컴퓨터에서 실제 유튜브 접속 없이 로직만
검증하려면 (합성 데이터로 만든 단위 테스트):

```bash
pip install -r requirements-dev.txt
pytest
```

실제로 값을 확인해보고 싶다면 (인터넷 되는 환경에서):

```bash
pip install -r requirements.txt
python scripts/track.py   # docs/status.json 이 갱신됩니다
```

## 파일 구조

```
.
├── data/
│   ├── channels.json   # 등록된 채널 목록 (id, name)
│   ├── tags.json       # 디스커버리 검색어 + 실행 주기
│   └── state.json      # 내부 상태(사이클 카운트 등, 손대지 않아도 됨)
├── docs/
│   ├── index.html      # GitHub Pages로 서빙되는 트래커 페이지
│   └── status.json     # track.py가 매 사이클 덮어쓰는 결과 데이터
├── scripts/
│   ├── common.py        # RSS/라이브상태/채널해석/검색 스크레이핑 함수 모음
│   ├── track.py          # 메인 실행 스크립트 (Actions가 주기적으로 실행)
│   └── resolve_channel.py # 이슈 폼 → 채널 등록 처리
├── tests/                # 합성 데이터 기반 단위 테스트 (pytest)
└── .github/
    ├── workflows/track.yml        # 많니륤 실행되는 트래킹 워크턌
├── workflows/add-channel.yml  # 채널 채뛌 이슈 자쟙 벘리
└── ISSUE_TEMPLATE/add-channel.yml  # 채널 등록 이슈 폼
```
