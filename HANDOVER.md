# NH농협 텔레그램 뉴스봇 — 인수인계 문서

> 최종 업데이트: 2026-05-22 (Claude Opus 세션)
> 목적: 다음 작업자가 컨텍스트를 즉시 이어받을 수 있도록 정리

---

## 1. 프로젝트 개요

NH농협 관련 금융·경영 핵심 뉴스를 자동 수집해 텔레그램 채널로 발송하는 봇.
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

루트(`/tmp/go_deploy/collector.py`)에 수정해도 **서버에 반영되지 않는다.** 이 함정 때문에 2026-05-21 ~ 22 세션에서 패치 누락이 발생했다. 루트의 dead 파일은 2026-05-22 세션에서 제거됨.

---

## 3. 파일 구조

```
won3ho-max/go/                ← GitHub 저장소 루트
├── news_bot/                 ← 봇 배포 대상 (deploy.yml이 이 폴더만 본다)
│   ├── main.py               ← 텔레그램 봇, 스케줄러
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

## 4. main.py 주요 동작

- `CHECK_INTERVAL_MINUTES`(기본 30분)마다 `fetch_new_articles()` 호출
- 수면시간(22:00~06:00 KST)에는 `pending_articles.json`에 큐잉 → 06:00 일괄 발송
- `/news` 명령어로 즉시 수동 수집 가능
- 매일 15:00 KST 하트비트 전송

---

## 5. 환경변수 (`.env` — 서버에만 존재)

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ANTHROPIC_API_KEY=...        # Claude Haiku LLM 필터용
NAVER_CLIENT_ID=...          # 네이버 뉴스 API (선택)
NAVER_CLIENT_SECRET=...      # 네이버 뉴스 API (선택)
CHECK_INTERVAL_MINUTES=30
```

---

## 6. collector.py 필터링 구조

### 필터 실행 순서

```
[수집] RSS 피드 / 네이버 API
  ↓
[1] _is_blocked_source(url, source_name)
    → BLOCKED_DOMAINS 또는 BLOCKED_SOURCE_NAMES에 해당하면 즉시 차단
  ↓
[2] is_relevant(title, summary)
    ├── 농협 키워드 없으면 차단 (KEYWORDS)
    │   └── 예외: '금고' 단독 + summary에 농협 있으면 통과 (새마을금고 오탐 방지)
    ├── STRUCTURAL_PROMO_PATTERNS 해당하면 차단
    ├── PROMO_KEYWORDS 해당하면 차단
    ├── [단독]/[속보] 태그면 즉시 통과
    └── WHITELIST_KEYWORDS 해당하면 통과, 없으면 기본 차단
  ↓
[3] LLM 필터 (_llm_filter)
    조건: not _is_trusted_source(url) OR _has_exec_name(title)
    → 신뢰 출처가 아니거나, 경영진 실명(강호동·박서홍·이찬우·강태영)이 제목에 있으면 LLM 재심
  ↓
[4] 중복 체크
    → get_article_id(url, title): URL 해시 기반
    → _is_similar_title(): 핵심 단어 3개 이상 겹치면 유사 기사로 차단
```

### 주요 상수 위치

| 상수 | 역할 |
|------|------|
| `TRUSTED_DOMAINS` | LLM 체크 면제 신뢰 도메인 |
| `BLOCKED_DOMAINS` | 즉시 차단 도메인 |
| `BLOCKED_SOURCE_NAMES` | Google RSS 우회 시 소스명 기반 차단 |
| `EXEC_NAMES` | 경영진 실명 (LLM 강제 트리거) |
| `KEYWORDS` | 농협 관련 여부 판단 키워드 |
| `WHITELIST_KEYWORDS` | 통과 조건 키워드 |
| `STRUCTURAL_PROMO_PATTERNS` | 구조적 홍보 패턴 (WHITELIST보다 우선) |
| `PROMO_KEYWORDS` | 블랙리스트 키워드 (WHITELIST보다 우선) |
| `RSS_FEEDS` | 수집 피드 목록 |
| `NAVER_SEARCH_QUERIES` | 네이버 API 검색 쿼리 |

---

## 7. RSS 피드 구성

```python
# Google 뉴스 (메인)
- 농협 / NH농협은행 / 농협중앙회 / 농협금융 검색
- 농협+site:news.mtn.co.kr (MTN 전용 — 딜레이 있음)
- 강호동+농협 / 박서홍+농협 / 이찬우+농협 / 강태영+농협
  → 경영진 실명 기반. site: 제한 없이 전 매체 빠른 수집 목적

# 직접 RSS (언론사별)
- 연합뉴스 (economy/society/industry)
- 뉴시스 (economy/bank)
- 매일경제, 머니투데이, 파이낸셜뉴스, 서울경제, 아시아경제
```

MTN(머니투데이방송)은 자체 RSS 없음 → Google RSS + 경영진 검색 피드로 수집.

---

## 8. LLM 프롬프트 (`_llm_filter`)

모델: `claude-3-5-haiku-20241022`
YES/NO 이진 판단 (max_tokens=5)

**YES 통과 카테고리:**
- 금융 실적 (순이익·영업이익·자산·대출 등)
- 금감원·금융위 규제, 법령 개정, 제재
- 주요 인사 (행장·대표이사 취임·해임·사퇴)
- 비리·수사 (횡령·배임·압수수색·검찰)
- 시장 분석 (금리·부실·연체·건전성)
- **유상증자·자본 확충·출자·자본정책** (CET1·RWA 등 자본 건전성 대응)
- **농협 지배구조·선출제도 변경** (직선제·간선제·선거제도 개편)
- **중앙회장·행장의 주요 정책 공식 발표**
- **경영진 비판 보도** (오락가락·번복)

**NO 차단 카테고리:**
- TV광고·모델·브랜드 홍보
- 감사패·수상·시상식
- 피해농가 지원·농촌 봉사·모내기
- 협약·MOU 체결
- 교육·캠페인·이벤트·행사
- 지역사회 활동
- 전기차·농산물·축산 등 금융 무관 주제
- 지방선거·정치 기사
- 경영 비전 선포 홍보 (X조로 Y한다, 상생성장 등)
- 칼럼·기고 시리즈 (금융기업가정신 등)
- IT 인프라·시스템 기사 (감리원 확충 등)
- 내부통제 행사·회의 개최 (추진계획 점검 등)

---

## 9. 패치 이력

### 2026-05-22 — 농협금융 1조1700억 유상증자 기사 4건 누락 대응

**문제**: einfomax / hankyung / mt / thebell의 '농협금융 1조 증자' 기사가 발송되지 않음.

**근본 원인 두 가지:**

1. **WHITELIST_KEYWORDS에 '증자' 계열 단어 누락** — '유상증자', '자본확충', '출자', '역출자' 등이 화이트리스트에 없어 제목이 농협금융 관련이라도 `is_relevant()`가 False를 반환. mt·einfomax·thebell 기사가 여기서 탈락.

2. **배포 경로 불일치 (구조적 원인)** — 이전 세션에서 작성한 `collector.py` 패치가 저장소 **root**에 위치했지만, `.github/workflows/deploy.yml`은 `news_bot/collector.py`만 서버에 반영. 따라서 직선제·상생성장·금융기업가정신 등 이전 패치가 모두 미반영 상태였음. 추가 LLM 프롬프트 보강분도 무력화.

**패치 내용 (이번 세션):**
- `news_bot/collector.py`에 다음 추가:
  - WHITELIST: `증자`, `유상증자`, `자본확충`, `자본 확충`, `출자`, `역출자`, `CET1`, `보통주자본비율`, `위험가중자산`, `RWA`
  - STRUCTURAL: `상생성장`, `돈길 튼다`, `길을 튼다`, `길 튼다`, `금융기업가정신`, `청렴 추진`, `반부패 추진`, `추진계획 점검`, `감리원`, `정보시스템 감리`, `IT 인프라`
  - BLOCKED_DOMAINS: `insnews.co.kr`
  - BLOCKED_SOURCE_NAMES: `뉴스포스트`
  - LLM YES: 유상증자·자본확충, 지배구조·선출제도, 회장 발표, 경영진 비판
  - LLM NO: 경영비전 홍보, 칼럼 시리즈, IT 인프라, 내부통제 행사
  - RSS_FEEDS: `농협금융` Google 검색 + `강호동/박서홍/이찬우/강태영+농협` 4건
- 저장소 root의 dead `collector.py` 삭제 (앞으로 혼란 방지)

---

## 10. 알려진 한계 및 주의사항

### WHITELIST 오탐 구조적 위험
WHITELIST_KEYWORDS에 등록된 광범위한 키워드(예: '리스크', '내부통제', '자산' 등)가 홍보성 기사에서도 등장할 수 있다.
→ **대응 원칙**: 오탐 발견 시 해당 키워드를 삭제하기보다 STRUCTURAL에 더 구체적인 패턴을 추가하는 것이 안전하다.

### 경영진 실명 WHITELIST의 딜레마
`강호동`, `박서홍`, `이찬우`, `강태영`은 WHITELIST에도 있고 EXEC_NAMES에도 있다.
- WHITELIST: `is_relevant()`에서 통과 판단
- EXEC_NAMES: 신뢰 출처라도 LLM 강제 재심

→ 경영진 이름이 제목에 있으면 반드시 LLM을 통과해야 최종 발송된다. LLM 프롬프트가 이 기사들의 최후 방어선.

### 신뢰 출처(TRUSTED_DOMAINS)의 허점
신뢰 출처는 경영진 실명이 없는 기사에 한해 LLM을 면제한다.
→ 연합뉴스, 뉴시스 등 대형 매체도 홍보성 기사를 내보낼 수 있으므로 STRUCTURAL·PROMO 필터가 충분히 촘촘해야 한다.

### Google RSS 딜레이
발행 직후 Google RSS에 즉시 반영되지 않는 경우가 있다. 특히 MTN은 직접 RSS가 없어 Google에 의존한다.
→ 경영진 실명 기반 RSS 피드를 추가했으나 완벽한 해결책은 아님. 네이버 API 키 설정 시 커버리지 보완 가능.

### 중복 탐지 민감도
`_is_similar_title()`의 `min_matches=3` 설정으로 핵심 단어 3개 이상 겹치면 유사 기사로 처리한다.
→ 너무 민감하면 다른 관점의 기사가 중복으로 차단될 수 있음.

---

## 11. 유지보수 가이드

### 새 홍보성 기사 패치 요청 시

1. 기사 제목을 보고 어느 단계에서 통과했는지 역추적
   - `is_relevant()` → STRUCTURAL·PROMO·WHITELIST 순서로 확인
   - 신뢰 출처 여부, EXEC_NAMES 해당 여부 확인
   - LLM 차단/통과 여부 추정

2. 패치 위치 결정
   - 제목에 명확한 패턴 있음 → STRUCTURAL_PROMO_PATTERNS 추가
   - 키워드 단독 차단 가능 → PROMO_KEYWORDS 추가
   - 매체 자체 문제 → BLOCKED_DOMAINS 또는 BLOCKED_SOURCE_NAMES 추가
   - LLM 오판 → LLM 프롬프트 NO 카테고리 보완

3. 패치 시 주의사항
   - STRUCTURAL 패턴이 정상 기사를 오차단하지 않는지 확인
   - `sentv.co.kr` (서울경제TV)는 BLOCKED에 추가 금지 — 과거 실수로 사용자가 명시 지시
   - WHITELIST에서 키워드 제거 시 해당 키워드에 의존하는 정상 기사가 함께 차단되는지 점검

4. **반드시 `news_bot/collector.py`를 수정. 저장소 root에는 collector.py가 없어야 한다.**

### 배포 후 검증

git push 후 GitHub Actions 워크플로(`deploy.yml`)가 자동 실행. 배포 완료 메시지가 텔레그램으로 전송됨.

직접 확인:
```bash
ssh won3ho@34.50.62.215 "sudo systemctl status news_bot"
```
