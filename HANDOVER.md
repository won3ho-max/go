# 금융권 뉴스 모니터링 텔레그램 봇 — 인수인계 문서

> 최종 업데이트: 2026-06-07 (Claude Opus 세션)
> 이전 버전: 2026-05-22 — 농협·수협 한정 시기. 이후 전 금융권으로 확장됨.

---

## 1. 프로젝트 개요

국내 주요 금융권(시중·지방·인터넷은행, 생·손해보험, 저축은행, 상호금융, 카드, 농협·수협) 핵심 뉴스를 자동 수집해 단일 텔레그램 채널로 발송하는 봇. **증권사·자산운용사 단독 기사는 의도적으로 제외.**

RSS 피드 + 네이버 뉴스 API를 폴링하고, 패턴 필터 + LLM(Claude Haiku) 이중 필터로 홍보성 기사를 걸러낸다.

---

## 2. 인프라

| 항목 | 값 |
|------|-----|
| 서버 | Google Cloud e2-micro |
| 서버 IP | 34.50.62.215 |
| SSH 사용자 | won3ho |
| 서비스 관리 | `sudo systemctl restart/status news_bot` |
| 코드 저장소 | GitHub: won3ho-max/go |
| 배포 방식 | GitHub Actions (`.github/workflows/deploy.yml`) |
| 배포용 로컬 클론 | `/tmp/go_deploy` |
| PAT 권한 | workflow scope 없음 (`.github/workflows/*` 수정 불가) |

### ⚠️ 배포 절차 (반드시 이 경로!)

GitHub Actions의 `deploy.yml`은 다음 두 가지 조건이 모두 충족돼야 트리거된다.

1. `news_bot/**` 경로의 파일이 변경됨 (`paths: news_bot/**`)
2. main 브랜치에 push됨

서버에서는 `news_bot/main.py`와 `news_bot/collector.py`만 git checkout 받는다. 따라서 코드 수정은 **반드시 `news_bot/` 디렉터리 안의 파일에 적용**해야 한다.

```bash
# 1. 파일 수정 후 news_bot/ 하위로 복사 (절대 root에 두지 말 것)
cp /sessions/<id>/mnt/<workspace>/collector.py /tmp/go_deploy/news_bot/collector.py

# 2. git config 확인 후 커밋 & 푸시
cd /tmp/go_deploy
git config user.email "won3ho@gmail.com"
git config user.name "won3ho"
git add news_bot/collector.py
git commit -m "fix: ..."
git push origin main
```

> 다른 시스템(예: 한탕)이 같은 저장소에 동시 커밋할 수 있어 push 거부가 나면 `git pull --rebase origin main` 후 재푸시.

---

## 3. 파일 구조

```
won3ho-max/go/                ← GitHub 저장소 루트
├── news_bot/                 ← 봇 배포 대상 (deploy.yml이 이 폴더만 본다)
│   ├── main.py               ← 텔레그램 봇, 스케줄러, ADMIN/BROADCAST 채널 분리
│   ├── collector.py          ← 핵심 파일. 필터 로직 전체 위치
│   ├── requirements.txt
│   └── news_bot.service
├── .github/workflows/
│   └── deploy.yml            ← news_bot/** 변경 시 자동 배포
└── HANDOVER.md               ← 이 문서
```

서버 측 파일 (자동 동기화되지 않음):
```
~/news_bot/news_bot/.env              # TELEGRAM_*, ANTHROPIC_API_KEY 등
~/news_bot/news_bot/seen_articles.json
~/news_bot/news_bot/seen_titles.json
```

---

## 4. 채널·발송 구조

### ADMIN_CHAT_ID vs BROADCAST_CHAT_ID 분리

```python
# main.py 상단
ADMIN_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')   # 1633958343 (won3ho 1:1)
BROADCAST_CHAT_ID = '-1003717850867'             # 금융 뉴스 모니터링(MTN) 채널
```

| 경로 | 발송 대상 |
|---|---|
| 30분→**15분** 폴링으로 수집한 새 뉴스 | **BROADCAST** (채널) |
| 06:00 일괄 발송 (새벽 쌓인 뉴스) | **BROADCAST** (채널) |
| 15:00 하트비트 | **ADMIN** (1:1) |
| 배포 완료 알림 (deploy.yml) | **ADMIN** (1:1, chat_id=1633958343 하드코딩) |
| `/news`, `/start`, `/status` 응답 | 명령 입력한 사람의 채팅창 |

채널 ID는 `deploy.yml`로 이전하려면 PAT workflow scope 필요 — 우선 `main.py`에 하드코딩.

---

## 5. main.py 주요 동작

- `CHECK_INTERVAL = 15` (분, 코드 상수). `.env`의 `CHECK_INTERVAL_MINUTES`는 무시. 변경 시 코드 직접 수정.
- 수면시간(22:00~06:00 KST)에는 `pending_articles.json`에 큐잉 → 06:00 일괄 발송
- `/news` 명령어로 즉시 수동 수집 가능
- 매일 15:00 KST 하트비트 (ADMIN_CHAT_ID로만)
- 단일 인스턴스 잠금 (fcntl) — 구봇 부활 원천 차단

---

## 6. 환경변수 (`.env` — 서버에만 존재)

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=1633958343  # ADMIN — deploy.yml이 매번 덮어쓰므로 직접 수정 불필요
ANTHROPIC_API_KEY=...        # Claude Haiku LLM 필터용
NAVER_CLIENT_ID=...          # 네이버 뉴스 API (선택)
NAVER_CLIENT_SECRET=...      # 네이버 뉴스 API (선택)
CHECK_INTERVAL_MINUTES=...   # 무시됨 (코드 하드코딩)
```

---

## 7. 메시지 포맷 (format_article)

```
<제목 (굵게)>
표출날짜 (예: 5월 26일 오전 10:30)
표출매체명
링크
```

매체명은 다음 순서로 추출:
1. 제목 뒤 ` - 매체명` 또는 ` | 매체명` 패턴
2. RSS feed의 `entry.source.title` (Google RSS) 또는 `feed.feed.title`
3. URL 도메인 (예: `hankyung.com`)

---

## 8. collector.py 필터링 구조

### 필터 실행 순서

```
[수집] RSS 피드 / 네이버 API
  ↓
[1] _is_blocked_source(url, source_name)
    → BLOCKED_DOMAINS 또는 BLOCKED_SOURCE_NAMES 해당하면 즉시 차단
  ↓
[2] is_relevant(title, summary)
    ├── 제목 KEYWORDS 매치 → 통과
    ├── 예외 a) '금고' 단독 + 본문 KEYWORDS → 통과 (지자체 금고)
    ├── 예외 b) 제목에 CLICKBAIT_PRODUCT_HINTS + 본문 KEYWORDS → 통과 (클릭베이트)
    ├── 예외 c) 제목에 [단독] + 본문 KEYWORDS → 통과 (단독 취재 fallback)
    ├── STRUCTURAL_PROMO_PATTERNS 해당 → 차단
    ├── PROMO_KEYWORDS 해당 → 차단
    ├── SCOOP_TAGS([단독]/[단독보도]만, [속보] 제외) → 통과
    ├── 제목 WHITELIST 매치 → 통과
    ├── 클릭베이트/단독 fallback이면 본문 WHITELIST 또는 본문 KEYWORDS 2+ 매치 → 통과
    └── 그 외 → 차단
  ↓
[3] LLM 필터 (_llm_filter)
    조건: not _is_trusted_source(url) OR _has_exec_name(title) OR _is_clickbait_pass(title)
    → 신뢰 출처라도 경영진 실명 또는 클릭베이트면 LLM 강제 검증
  ↓
[4] 중복 체크
    → get_article_id(url, title): URL 해시 기반
    → _is_similar_title(): 핵심 단어 3개 이상 겹치면 유사 기사로 차단
```

### 주요 상수 위치

| 상수 | 역할 |
|------|------|
| `TRUSTED_DOMAINS` | LLM 체크 면제 신뢰 도메인 (107개) |
| `BLOCKED_DOMAINS` | 즉시 차단 도메인 |
| `BLOCKED_SOURCE_NAMES` | Google RSS 우회 시 소스명 기반 차단 |
| `EXEC_NAMES` | 경영진 실명 (LLM 강제 트리거) |
| `KEYWORDS` | 1차 게이트 — 금융사·기관·통칭 키워드 약 130종 |
| `CLICKBAIT_PRODUCT_HINTS` | 본문 fallback 트리거 (펀드/예금/대출/카드/주담대 등) |
| `WHITELIST_KEYWORDS` | 통과 조건 키워드 (실적·인사·비리·자본 등) |
| `STRUCTURAL_PROMO_PATTERNS` | 구조적 홍보 패턴 (PR 어구·시리즈 태그) |
| `PROMO_KEYWORDS` | 블랙리스트 키워드 (출시·MOU·수상 등) |
| `RSS_FEEDS` | 수집 피드 목록 |
| `NAVER_SEARCH_QUERIES` | 네이버 API 검색 쿼리 |

### 수집 범위 (KEYWORDS)

- **농협 계열**: 농협, NH농협, 농협은행/중앙회/금융/생명/손해보험/카드
- **수협 계열**: 수협, Sh수협, 수협은행/중앙회/금융/캐피탈/증권/개발
- **5대 금융지주**: KB금융, 신한금융, 하나금융, 우리금융, NH농협금융 + 각 지주명
- **시중은행 5사**: KB국민/신한/하나/우리/IBK기업
- **지방 금융지주·은행**: BNK금융(부산/경남), DGB는 iM금융지주/iM뱅크로 사명 변경, JB금융(광주/전북), 제주은행, SC제일, 한국씨티
- **인터넷전문은행**: 카카오뱅크, 토스뱅크, 케이뱅크
- **생명보험 14사**: 삼성/한화/교보/신한라이프/KB라이프/미래에셋/동양/흥국/ABL/iM라이프/AIA/메트라이프/푸르덴셜/하나생명
- **손해보험 10사**: 삼성화재, DB손해보험, 현대해상, KB손해보험, 메리츠화재, 한화/롯데/흥국/MG/캐롯
- **저축은행 8사**: SBI/OK/페퍼/웰컴/한국투자/JT친애/OSB + 중앙회
- **상호금융**: 신협(중앙회), 새마을금고(중앙회)
- **카드 9사**: 신한/KB국민/삼성/현대/롯데/우리/하나/BC + '카드사'
- **규제·정책·중앙은행**: 금감원, 금융감독원, 금융위(원회), 예금보험공사, 한국은행, 금융통화위원회(금통위), 금융결제원, 한국거래소(KRX), 코스콤, 한국예탁결제원
- **금융 협·단체**: 은행연합회, 여신금융협회(여신협회/화보협회), 생명보험협회(생보협회), 손해보험협회(손보협회), 한국금융연구원, 보험연구원
- **통칭**: 시중은행, 지방은행, 인터넷전문은행, 5대 금융지주, 금융지주, 저축은행, 상호금융, 5대 은행, 4대 은행, 금융권, 은행권, 보험권, 금융사고
- **업종 주식 통칭**: 금융주, 은행주, 보험주, 카드주, 저축은행주, 지주주
- **정부·정책 프로그램**: 국민성장펀드, 국민참여성장펀드, 생산적 금융, 포용금융, NH 상생성장 프로젝트
- **칼럼 시리즈 태그**: [금융 히스토리], [금융 인사이트], [금융 풍속도], [금융 IN], [금융 NOW], [CEO 라운지]

### EXEC_NAMES (LLM 강제 검증 대상)

- 농협: 강호동, 박서홍, 이찬우, 강태영
- 수협: 노동진, 신학기

### CLICKBAIT_PRODUCT_HINTS

펀드, 예금, 적금, 대출, 신상품, 금융상품, 카드, 보험, 신탁, 주담대, 주택담보대출, 전세대출, 신용대출, 갈아타기, 회장 복귀, 회장직 복귀, 이사장 후보, 이사장 선출

---

## 9. RSS 피드 구성

```python
# Google 뉴스 — 농협·수협 계열
- 농협 / NH농협은행 / 농협중앙회 / 농협금융 검색
- 농협+site:news.mtn.co.kr (MTN 전용 — 딜레이 있음)
- 강호동/박서홍/이찬우/강태영 + 농협 (경영진 실명)
- 수협 / 수협중앙회 / Sh수협은행
- 노동진/신학기 + 수협

# Google 뉴스 — 5대 금융지주 그룹 차원
- KB금융지주, 신한금융지주, 하나금융지주, 우리금융지주

# Google 뉴스 — 인터넷전문은행
- 카카오뱅크, 토스뱅크, 케이뱅크

# Google 뉴스 — 업종별 검색
- 손해보험+실적, 생명보험+실적, 저축은행+건전성, 카드사+실적

# Google 뉴스 — 규제·정책 기관
- 금융감독원, 금융위원회, 예금보험공사

# 직접 RSS (언론사별)
- 연합뉴스 (economy/society/industry), 뉴시스 (economy/bank)
- 매일경제, 머니투데이, 파이낸셜뉴스, 서울경제, 아시아경제
```

---

## 10. LLM 프롬프트 (`_llm_filter`)

모델: `claude-3-5-haiku-20241022`, YES/NO 이진 판단 (max_tokens=5)

**YES 통과 카테고리:**
- 금융 실적 (순이익·영업이익·자산·대출 등)
- 금감원·금융위 규제, 법령 개정, 제재
- 주요 인사 (행장·대표이사 취임·해임·사퇴)
- 비리·수사 (횡령·배임·압수수색·검찰)
- 시장 분석 (금리·부실·연체·건전성)
- 유상증자·자본 확충·출자·자본정책 (CET1·RWA 등)
- 농협 지배구조·선출제도 변경
- 중앙회장·행장의 주요 정책 공식 발표
- 경영진 비판 보도

**NO 차단 카테고리:**
- TV광고·모델·브랜드 홍보
- 감사패·수상·시상식
- 피해농가·피해어가 지원, 농촌·어촌 봉사, 모내기·어업 봉사
- 협약·MOU 체결
- 교육·캠페인·이벤트·행사
- 지역사회 활동
- 전기차·농산물·축산·수산물·어업 활동 등 금융 무관
- 지방선거·정치
- **증권사·자산운용사 단독 기사** (지주 차원 그룹 기사는 YES)
- 경영 비전 선포 홍보 (상생성장, 돈길 튼다 등)
- 칼럼·기고 시리즈 (금융기업가정신 등)
- IT 인프라·시스템 (감리원 확충 등)
- 내부통제 행사·회의 개최
- 신규 금융상품 출시·홍보 (인기·완판·매진·페이백 강조)
  - 단, 정부 공적 펀드의 시장 반응은 YES
- **지역 단위 농협·축협·수협 PR** (XX농협 OO 돌파/달성)
- **행장·임원 현장 방문·동정** (스타트업 방문·간담회 등)
- **사회공헌 일자리 확대** (장애인 일터·굿윌스토어·발달장애인 고용)
- **자산관리 세미나·머니쇼 등 행사 개최**

---

## 11. 차단 도메인·소스 (BLOCKED)

**BLOCKED_DOMAINS:**
- youngnong.co.kr, pinpointnews.co.kr, newsworker.co.kr, thefirstmedia.net
- gukjenews.com, jndn.com, woryesanup.co.kr, newsquest.co.kr
- insnews.co.kr (보험뉴스 — IT 인프라 홍보)
- aflnews.co.kr (농수축산 전문지 — 스마트팜·축협 행사)

**BLOCKED_SOURCE_NAMES (Google RSS 소스명):**
- 핀포인트뉴스, 영농뉴스, 원예산업신문, 뉴스워커, 더퍼스트미디어,
- 국제뉴스, 전남도민뉴스, 뉴스퀘스트, 시민행정신문, 경기경제신문,
- 안전신문, 농수축산신문, 일간경기, gmitoday, 위즈뉴스, 투데이안,
- 뉴스포스트 (반부패·청렴 행사), 팜인사이트 (한우·축산 PR)

---

## 12. 패치 이력 (2026-05-22 이후 누적 요약)

### 2026-05-22 ~ 23 — 수협 + 전 금융권 확장
- 농협·수협 → 시중은행·지방은행·인터넷뱅크·생/손보·저축은행·상호금융·카드·규제기관 통합
- EXEC_NAMES에 수협 경영진 노동진·신학기 추가
- 증권·운용사는 KEYWORDS에 미포함 → 1단계 게이트에서 자동 차단, 지주 차원 그룹 기사는 통과

### 2026-05-22 — DGB → iM 사명 변경
- iM금융지주, iM뱅크, iM라이프 / 대구은행은 legacy로 유지

### 2026-05-23 — 채널·1:1 분리 + 15분 폴링
- BROADCAST_CHAT_ID (채널), ADMIN_CHAT_ID (1:1) 분리
- 폴링 주기 30→15분 (LLM 캐시로 비용 영향 미미)

### 2026-05-23 — 금융 협단체·중앙은행
- 한국은행/금통위, 금융결제원/한국거래소/KRX, 은행연합회/여신금융협회/생/손보협회

### 2026-05-23 — 클릭베이트 fallback 도입
- 제목에 CLICKBAIT_PRODUCT_HINTS만 있고 본문에 KEYWORDS 매치 시 통과
- 신뢰 출처라도 클릭베이트 fallback이면 LLM 강제 검증

### 2026-05-23 — SGI 보도자료·코스닥 속보 차단
- STRUCTURAL: 연차총회, 협력 강화, 코스피/코스닥 마감
- SCOOP_TAGS에서 [속보] 제거 (내용 없는 마감 보도)

### 2026-05-25 — aflnews 차단 + MTN 칼럼 통과
- aflnews.co.kr 도메인 차단
- MTN [금융 히스토리] 등 정통 칼럼 시리즈 태그를 KEYWORDS+WHITELIST에 추가

### 2026-05-26 — 묶음 PR 시리즈 + 합병 substring 버그 + 메시지 포맷
- [이모저모], [카드레터] 등 묶음 PR 시리즈 태그 차단
- WHITELIST `합병` → `종합병원` substring 매치 버그 해결 (인수합병 등 구체 어구로)
- format_article 아이콘 제거, 매체명 라인 추가

### 2026-05-26 — 아침자 누락 7건 종합 패치
- KEYWORDS: 금융권, 은행권, 보험권, 금융사고, 화보협회
- CLICKBAIT_HINTS: 주담대, 주택담보대출, 전세대출, 신용대출, 회장 복귀
- PROMO 정밀화: 제휴 카드 단독 → 출시·혜택 결합 어구만
- [단독] + 본문 KEYWORDS fallback 룰 추가
- WHITELIST: 역풍, 셈법, 딜레마, 난감 (비즈니스 분석 어구)

### 2026-05-27 — [한 컷] 동정 + 장애인 맞춤지원
- STRUCTURAL: [한 컷], [현장], [사진], [화보]
- 장애인 맞춤·재활부터·통합 지원체계

### 2026-05-31 — 금융주/은행주 KEYWORDS
- 시장분석 기사 누락 대응 (MTN 팔천피 금융주)

### 2026-05-31 — 12건 누락 PR 종합
- [그래픽], [이주의 시리즈, 머니쇼, 굿윌스토어, 발달장애인 일자리
- 행장 동정 어구 (현장이 답이다, 소통 통해, 동반자 역할)
- 정기예금 PR (개월 최고 연, 최고 연 3./4./5.)
- LLM NO 카테고리에 지역 농협 PR·행장 동정·사회공헌 일자리 명시

### 2026-06-07 — 강원농협·금감원 점검·시니어 카드
- 지역 농협 자랑 (전국 두번째, 여신 선도, 건전 여신)
- 행정 점검 클릭베이트 (점검 나선, 나선 이유는, 특사경과)
- 카드 시리즈 ([1분 어드바이스], 액티브 시니어, 시니어 전용카드)

---

## 13. 알려진 한계 및 주의사항

### 키워드 기반 필터의 한계
PR 패턴이 무한히 변형되어 사용자가 발견할 때마다 패치 필요 ("두더지 잡기"). 대안으로 모든 기사에 LLM을 통과시키는 방안 검토했으나, 사용자 결정으로 현재 키워드 기반 유지.

### 신뢰 출처(TRUSTED_DOMAINS)의 허점
107개 신뢰 출처는 LLM을 면제받지만, 클릭베이트 fallback과 경영진 실명일 때는 LLM 강제 실행. PR 새는 통로는 주로 신뢰 출처 + STRUCTURAL/PROMO 미매치 조합.

### 부분 문자열 매치 위험
Python `in` 연산자 substring 매치라 `합병` ↔ `종합병원` 같은 오매치 가능. 단어 추가 시 다른 단어의 substring으로 작동하는지 점검 필수.

### Google RSS 딜레이
발행 직후 Google RSS에 즉시 반영되지 않는 경우 있음. 특히 MTN은 직접 RSS 없어 Google에 의존. 경영진 실명 RSS·네이버 API로 보완하나 완벽하지 않음.

### 중복 탐지 민감도
`_is_similar_title()` min_matches=3 — 핵심 단어 3개 이상 겹치면 유사 기사로 차단. 너무 민감하면 다른 관점의 기사 함께 차단될 수 있음.

### 24시간 컷오프
RSS 수집 시 발행 후 24시간 이상 지난 기사는 자동 제외. 발견이 늦은 기사는 패치해도 다시 수집되지 않음.

---

## 14. 유지보수 가이드

### 새 PR 기사 패치 요청 시

1. 시뮬레이션으로 어느 단계에서 통과했는지 역추적
   ```python
   from collector import is_relevant, KEYWORDS, STRUCTURAL_PROMO_PATTERNS, PROMO_KEYWORDS, WHITELIST_KEYWORDS, CLICKBAIT_PRODUCT_HINTS, _is_trusted_source, _is_clickbait_pass
   ```

2. 패치 위치 결정
   - 매체 자체 문제 → BLOCKED_DOMAINS / BLOCKED_SOURCE_NAMES
   - 제목에 명확한 PR 패턴 → STRUCTURAL_PROMO_PATTERNS
   - 단독 키워드 차단 가능 → PROMO_KEYWORDS
   - LLM 오판 → LLM 프롬프트 NO 카테고리 보완

3. 새 누락 기사 패치 요청 시
   - KEYWORDS 누락? → KEYWORDS 추가
   - 본문 fallback 필요? → CLICKBAIT_PRODUCT_HINTS 추가
   - WHITELIST 누락? → WHITELIST 추가

4. 패치 시 주의사항
   - STRUCTURAL 패턴이 정상 기사 오차단하지 않는지 확인
   - `sentv.co.kr` (서울경제TV)는 BLOCKED 금지 — 과거 실수로 사용자 명시 지시
   - WHITELIST에서 키워드 제거 시 의존 기사 함께 차단되는지 점검
   - 새 KEYWORDS가 다른 단어의 substring으로 매치되는지 확인
   - **반드시 `news_bot/collector.py`를 수정**

### 배포 후 검증

git push 후 GitHub Actions 워크플로(`deploy.yml`) 자동 실행. 배포 완료 메시지는 ADMIN_CHAT_ID(1:1)로만 전송.

직접 확인:
```bash
ssh won3ho@34.50.62.215 "sudo systemctl status news_bot"
```

GitHub Actions 로그에서 `BOT_RUNNING_OK` 확인:
```bash
curl -s -H "Authorization: token <PAT>" "https://api.github.com/repos/won3ho-max/go/actions/runs?per_page=1"
```

---

## 15. 신뢰 출처(TRUSTED_DOMAINS) 명단 — 총 107개

| 분류 | 매체 |
|---|---|
| 통신사 (4) | 연합뉴스, 뉴시스, 뉴스1, 연합인포맥스 |
| 경제 대형 (24) | 매일경제, 한국경제, 서울경제, 머니투데이, 파이낸셜뉴스, 아시아경제, 이데일리, 헤럴드경제, 이투데이, 뉴스핌, 더벨, 비즈니스포스트, 아이뉴스24, 아주뉴스, FN투데이, 한국금융신문, 비즈워치, 머니S, 뉴스토마토, 딜사이트, 인베스트조선, 이코노미스트, 시사저널, 시사인 |
| 경제 중형 (12) | seoulfn, efnews, joseilbo, taxtimes, insure, aitimes, econovill, forbes, thescoop, newdaily, polinews, newsworks |
| 종합일간지 (15) | 조선, 중앙, 동아, 한겨레, 경향, 국민, 세계, 문화, 한국, 내일, 쿠키, 프레시안, 오마이, 미디어오늘, 데일리안 |
| 방송 (15) | YTN, MBC, KBS, SBS, JTBC, TV조선, 채널A, MBN, MTN, 한국경제TV, 서울경제TV, 매일경제TV, 연합뉴스TV, CBS, 노컷, TBS, OBS, 아리랑 |
| IT 전문 (11) | 전자신문, ZDNet, 블로터, 디지털데일리, 보안뉴스, 디지털타임스, IT데일리, AI타임스, 벤처스퀘어, 플래텀, 아이로봇뉴스 |
| 지역 (17) | 부산일보, 국제, 경남, 영남, 매일, 대전, 충청투데이, 광주, 전남, 전북도민, 도민, 강원, 강원도민, 제주, 한라, 인천, 경기, 중도 |
| 기타 (2) | 더팩트, 뉴스와이어 |
