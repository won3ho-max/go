# 금융권 뉴스 모니터링 텔레그램 봇 — 인수인계 문서 v5

> 최종 업데이트: 2026-06-16 (Claude Opus 4.6 세션 — v4 이후 누적 패치 반영)
> 이전 버전: v4 (2026-06-11). 코드 전수 대조 + 사고방지 체크리스트 도입.
> v5: EXEC_NAMES 48명 확대 + 폴링 30건+sleep + 정책 토픽 KEYWORDS + 농협 동등화 등.

---

## 0. ⛔ 이 문서를 읽는 AI/작업자에게 — 먼저 읽고 시작할 것

**원칙 1: 코드가 진실이다. 이 문서는 지도일 뿐이다.**
패치 전에 반드시 현재 코드를 직접 읽어라. 문서의 상수 개수·목록은 작성 시점 스냅샷이며, 이후 패치로 달라져 있을 수 있다.

```bash
rm -rf /tmp/go_deploy
git clone --depth 1 https://github.com/won3ho-max/go.git /tmp/go_deploy
grep -n "^[A-Z_]* = \|^def " /tmp/go_deploy/news_bot/collector.py   # 구조 파악
```

**원칙 2: 추측으로 커밋하지 마라. 패치 전후로 반드시 시뮬레이션을 돌려라.**
(§14 회귀 테스트 스니펫 — 실행 가능한 코드 제공)

**원칙 3: 수정 대상은 오직 `news_bot/` 하위 파일이다.**
저장소 루트의 `collector.py` 같은 파일을 만들거나 수정하면 배포되지 않는다. 역대 사고 1순위.

### 🚫 절대 금지 (역대 사고 모음)

| # | 금지 사항 | 이유 |
|---|---|---|
| 1 | 저장소 루트에 `collector.py`/`main.py` 두기 | deploy.yml은 `news_bot/**`만 본다. 루트 파일은 영원히 배포 안 됨 |
| 2 | `.github/workflows/*` 수정 시도 | PAT에 workflow scope 없음 → push 거부됨 |
| 3 | `sentv.co.kr` (서울경제TV) 차단 | 과거 실수로 차단했다가 사용자 명시 지시로 복구. **TRUSTED 유지** |
| 4 | 짧은 단어를 KEYWORDS/WHITELIST/PROMO에 추가 | Python `in` substring 매치. `합병`→`종합병원`, `한은`→`한국은행` 미매치 등 substring 함정. §14-3 점검 필수 |
| 5 | `.env`의 `CHECK_INTERVAL_MINUTES` 수정으로 주기 변경 시도 | 무시됨. `main.py`의 `CHECK_INTERVAL = 15` 상수 직접 수정해야 함 |
| 6 | 서버에서 직접 코드 수정 | 다음 배포 때 `git checkout origin/main`으로 덮어써짐. 반드시 git 경유 |
| 7 | push 거부 시 `--force` | 같은 저장소를 한탕(hantang) 등 다른 시스템이 동시 커밋. `git pull --rebase origin main` 후 재푸시 |
| 8 | `seen_articles.json`/`seen_titles.json` 임의 삭제 | 최근 24시간 기사 전부 재발송되는 폭탄. 특정 기사 재수집이 필요하면 해당 항목만 제거 |
| 9 | 서버 `.env`의 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/`ANTHROPIC_API_KEY` 수동 수정 | deploy.yml이 배포마다 GitHub Secrets 값으로 덮어씀. 수정해도 소용없음 (NAVER_* 키만 수동 관리) |
| 10 | 패치 후 검증 생략 | §15 배포 후 검증 절차 필수. "커밋했으니 끝"이 빵꾸의 주범 |
| 11 | EXEC_NAMES에 너무 일반적인 이름 추가 | 동명이인 매치 위험. 예: `김재관` 같은 흔한 이름은 본문 회사명과 함께 검증되도록 RSS로만 수집하고, EXEC_NAMES 추가는 보수적으로 |
| 12 | 농협만 특별 취급 | 2026-06-16 사용자가 명시. 8대 금융지주 + 산하 시중은행장·카드사·보험사 CEO 동등하게 EXEC_NAMES 등록 완료 |

---

## 1. 프로젝트 개요

국내 주요 금융권(시중·지방·인터넷은행, 생·손보, 저축은행, 상호금융, 카드, 농협·수협) 핵심 뉴스를 자동 수집해 텔레그램 채널로 발송하는 봇. **증권사·자산운용사 단독 기사는 의도적으로 제외** (지주 차원 그룹 기사는 포함).

수집: Google 뉴스 RSS(78개) + 언론사 직접 RSS(6개) + 네이버 뉴스 API(13개 쿼리)
필터: 패턴 필터(`is_relevant`) + LLM(Claude Haiku) 이중 필터

---

## 2. 인프라

| 항목 | 값 |
|------|-----|
| 서버 | Google Cloud e2-micro |
| 서버 IP | 34.50.62.215 |
| SSH 사용자 | won3ho |
| 서비스 관리 | `sudo systemctl restart/status news_bot` |
| 서버 로그 | `/home/won3ho/bot.log` (systemd가 stdout/stderr append) |
| 코드 저장소 | GitHub: won3ho-max/go (공개) |
| 배포 방식 | GitHub Actions `.github/workflows/deploy.yml` |
| 배포용 로컬 클론 | `/tmp/go_deploy` (세션마다 새로 클론 권장) |
| PAT 권한 | workflow scope 없음 |

### 저장소 동거 시스템 주의

`won3ho-max/go`에는 news_bot 외에 **한탕(hantang) 주식 시스템, telegram_listener, gsheets 스크립트**가 함께 산다. 워크플로도 deploy.yml 외 5개(batch_add, daily_report, keepalive, manual_sell, telegram_listener)가 있다. **news_bot 작업 시 다른 폴더/워크플로를 건드리지 말 것.** push 충돌도 이 동거 때문에 발생한다.

---

## 3. 배포 파이프라인 (deploy.yml 실제 동작)

### 트리거 조건
- main 브랜치 push + 변경 경로가 `news_bot/**` **또는** `.github/workflows/deploy.yml`

### 배포 시 서버에서 일어나는 일 (전부 자동)
1. `git checkout origin/main -- news_bot/main.py`, `news_bot/collector.py` — **이 두 파일만 동기화**. requirements.txt 등 다른 파일은 배포돼도 서버에 반영 안 됨
2. `.env`의 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`(=1633958343), `ANTHROPIC_API_KEY`를 GitHub Secrets 값으로 **덮어씀**
3. systemd 유닛 파일을 매번 새로 생성 (`WorkingDirectory=/home/won3ho/news_bot/news_bot`)
4. `systemctl stop` + `pkill -9 -f 'news_bot/news_bot/main.py'` → 재시작
5. `BOT_RUNNING_OK` / `BOT_START_FAILED` 출력
6. 마지막에 curl로 "배포 완료 테스트" 메시지를 **1633958343 (ADMIN 1:1)** 으로 전송

### 표준 배포 절차

```bash
# 0. 항상 새로 클론 (이전 세션의 /tmp/go_deploy 신뢰 금지)
rm -rf /tmp/go_deploy && git clone https://github.com/won3ho-max/go.git /tmp/go_deploy

# 1. news_bot/ 하위 파일 수정 (절대 루트에 두지 말 것)
#    수정 후 §14 회귀 테스트 통과 확인

# 2. 커밋 & 푸시
cd /tmp/go_deploy
git config user.email "won3ho@gmail.com"
git config user.name "won3ho"
git add news_bot/collector.py        # 수정한 파일만 명시적으로 add
git commit -m "fix: ..."
git push origin main
# 거부되면: git pull --rebase origin main && git push origin main

# 3. §15 배포 후 검증 (생략 금지)
```

---

## 4. 파일 구조

```
won3ho-max/go/
├── news_bot/                  ← 배포 대상 (이 폴더만)
│   ├── main.py                ← 봇·스케줄러·채널 분리 (212줄, v5 기준)
│   ├── collector.py           ← 핵심. 필터 로직 전체 (약 1,403줄)
│   ├── requirements.txt       ← 서버 자동 반영 안 됨
│   ├── news_bot.service       ← 참고용 (실제 유닛은 deploy.yml이 생성)
│   └── setup.sh
├── hantang/ 등                ← ⚠️ 다른 시스템. 건드리지 말 것
├── .github/workflows/deploy.yml
└── HANDOVER.md                ← 이 문서
```

서버 측 (자동 동기화 안 됨):
```
~/news_bot/news_bot/.env                    # NAVER_* 만 수동 관리 영역
~/news_bot/news_bot/seen_articles.json      # URL 해시, 최근 2,000개 유지
~/news_bot/news_bot/seen_titles.json        # 제목, 최근 500개 유지 (v5: 100→500)
~/news_bot/news_bot/pending_articles.json   # 수면시간 큐
~/news_bot/news_bot/.bot.lock               # fcntl 단일 인스턴스 잠금
~/bot.log                                   # 서비스 로그
```

---

## 5. 채널·발송 구조 (v5 변경: 발송 한도 10→30 + sleep)

```python
# main.py 상단
ADMIN_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')    # 1633958343 (won3ho 1:1)
BROADCAST_CHAT_ID = '-1003717850867'              # 금융 뉴스 모니터링(MTN) 채널 — 하드코딩
CHAT_ID = BROADCAST_CHAT_ID                       # 하위 호환 별칭
CHECK_INTERVAL = 15                               # 분. .env 값 무시
```

| 경로 | 대상 | 건수 제한 |
|---|---|---|
| 15분 폴링 새 뉴스 | BROADCAST 채널 | **회당 최대 30건 + 발송 사이 1초 sleep** (v5 변경, 이전 10건 제한 해소) |
| 06:00 일괄 발송 (수면 큐) | BROADCAST 채널 | 제한 없음 |
| 15:00 하트비트 | ADMIN 1:1 | — |
| 배포 완료 알림 | ADMIN 1:1 (deploy.yml 하드코딩) | — |
| `/news` `/start` `/status` | 명령 입력자 채팅창 | /news 최대 30건+sleep |

- 수면시간 22:00~06:00 KST: 발송 대신 `pending_articles.json` 큐잉 → 06:00 일괄 발송
- 단일 인스턴스 잠금(fcntl): 중복 기동 시 두 번째 프로세스 즉시 종료
- **v5 발송 한도 완화**: 텔레그램 채널 rate-limit(분당 20건) 안전 마진 + 도배 방지 위해 `asyncio.sleep(1)` 끼움. 30건 다 발송돼도 약 30초 소요. 평소 1~5건이라 체감 차이 없음.

---

## 6. 환경변수 (서버 `.env`)

```
TELEGRAM_BOT_TOKEN=...       # deploy.yml이 매번 덮어씀 (Secrets: NH_BOT_TOKEN)
TELEGRAM_CHAT_ID=1633958343  # deploy.yml이 매번 덮어씀
ANTHROPIC_API_KEY=...        # deploy.yml이 매번 덮어씀 — LLM 필터용
NAVER_CLIENT_ID=...          # ★수동 관리. 없으면 네이버 수집 조용히 스킵
NAVER_CLIENT_SECRET=...      # ★수동 관리
CHECK_INTERVAL_MINUTES=...   # 무시됨 (코드 상수 우선)
```

**LLM 필터는 fail-open**: `ANTHROPIC_API_KEY` 미설정 또는 API 오류 시 **무조건 통과**. "갑자기 PR이 쏟아진다" → API 키/크레딧 상태부터 의심 (§13-4).

---

## 7. 메시지 포맷 (`format_article`)

```
<제목 (굵게)>
5월 26일 오전 10:30      ← KST 변환 (_to_kst_str)
매체명                    ← _extract_media
링크
```

매체명 우선순위: ① `entry.source.title`(Google RSS) / `feed.feed.title` → ② 제목 뒤 ` - 매체명`·` | 매체명` 패턴 → ③ URL 도메인.

---

## 8. collector.py 필터 파이프라인

```
[수집] RSS 84개 피드 → 네이버 API 13개 쿼리 (이 순서, seen 공유)
  ↓
[0] 24시간 컷오프 — 발행 24h 경과 기사 제외 (파싱 실패 시 통과)
  ↓
[1] _is_blocked_source(url, source_name)
    BLOCKED_DOMAINS(도메인 substring) / BLOCKED_SOURCE_NAMES(Google RSS 소스명)
    ⚠️ 네이버 API 경로는 source_name 인자 없이 호출 → 도메인 차단만 적용 (§13-5)
  ↓
[2] is_relevant(title, summary)
    1단계: 제목 KEYWORDS 매치 필수. 예외 fallback 3종:
      a) '금고' 단독(새마을금고 제외) + 본문 KEYWORDS
      b) 제목 CLICKBAIT_PRODUCT_HINTS + 본문 KEYWORDS
      c) 제목 [단독]/[단독보도] + 본문 KEYWORDS
    2단계: STRUCTURAL_PROMO_PATTERNS 매치 → 차단
    3단계: PROMO_KEYWORDS 매치 → 차단
    4단계: [단독]/[단독보도] 태그 → 즉시 통과 ([속보]는 제외)
    5단계: 제목 WHITELIST_KEYWORDS 매치 → 통과
    5-A: fallback 기사는 본문 WHITELIST 또는 본문 KEYWORDS 2개 이상 → 통과
    6단계: 기본 차단
  ↓
[3] LLM 필터 (_llm_filter — claude-3-5-haiku-20241022, YES/NO, max_tokens=5)
    실행 조건: 비신뢰 출처 OR 제목에 EXEC_NAMES OR _is_clickbait_pass(title)
    캐시: 프로세스 메모리 dict (재시작 시 초기화)
    fail-open: 키 없음/오류 시 통과
  ↓
[4] 중복 체크 (v5 강화)
    get_article_id: URL ?쿼리 제거 후 md5(url+title)
    _is_similar_title:
      - 제목 완전일치 즉시 차단
      - 핵심단어(2자 이상, substring 매치 포함) 3개 이상 겹치면 차단 (기본 min_matches=3)
      - **v5 동적 임계값**: 양쪽 제목 모두 _AGENCY_NAMES(금감원/금융위/한은 등) 포함 시 min_matches=2 적용
      - 비교 대상은 최근 500개 제목 (v5: 100→500 확장)
```

### 주요 상수 (2026-06-16 코드 실측값)

| 상수 | 개수 | 역할 |
|------|---|------|
| `TRUSTED_DOMAINS` | 107 | LLM 면제 신뢰 도메인 |
| `BLOCKED_DOMAINS` | 10 | 즉시 차단 도메인 |
| `BLOCKED_SOURCE_NAMES` | 18 | Google RSS 소스명 차단 |
| `EXEC_NAMES` | **48** | **8대 지주 회장 + 산하 시중은행장·인터넷뱅크·카드·생보·손보 사장** — LLM 강제 |
| `KEYWORDS` | 174 | 1차 게이트 (금융사·기관·통칭·정책 토픽) |
| `WHITELIST_KEYWORDS` | 228 | 통과 키워드 (실적·인사·비리·자본·정책 긴급성 등) |
| `STRUCTURAL_PROMO_PATTERNS` | 384 | 구조적 홍보 패턴 |
| `PROMO_KEYWORDS` | 469 | 블랙리스트 |
| `CLICKBAIT_PRODUCT_HINTS` | 18 | 본문 fallback 트리거 |
| `RSS_FEEDS` | **85** | 수집 피드 (지방·인터넷·시중지주+회장 검색 + 단독 + 정책 토픽) |
| `NAVER_SEARCH_QUERIES` | 13 | 네이버 API 쿼리 |
| `_AGENCY_NAMES` (신규) | 9 | 당국명 — dedup 임계값 동적 조정용 |

개수 확인 명령:
```bash
cd /tmp/go_deploy/news_bot && python3 -c "
import collector as c
for n in ['TRUSTED_DOMAINS','BLOCKED_DOMAINS','BLOCKED_SOURCE_NAMES','EXEC_NAMES',
          'KEYWORDS','WHITELIST_KEYWORDS','STRUCTURAL_PROMO_PATTERNS','PROMO_KEYWORDS',
          'RSS_FEEDS','NAVER_SEARCH_QUERIES','CLICKBAIT_PRODUCT_HINTS']:
    print(n, len(getattr(c,n)))"
```

### EXEC_NAMES 48명 분류 (v5 신규)

| 카테고리 | 명단 |
|---|---|
| 농협 (4) | 강호동, 박서홍, 이찬우, 강태영 |
| 수협 (2) | 노동진, 신학기 |
| 8대 지주 회장 (8) | 양종희(KB), 진옥동(신한), 함영주(하나), 임종룡(우리), 이찬우(NH), 빈대인(BNK), 김기홍(JB), 황병우(iM) |
| 5대 시중은행장 (5) | 이환주(KB국민), 정상혁(신한), 이호성(하나), 정진완(우리), 김성태(IBK) |
| 인터넷뱅크 3사 (3) | 윤호영(카카오), 최우형(케이), 이은미(토스) |
| iM뱅크 (1) | 강정훈 |
| 지방은행 5+legacy 2 (7) | 방성빈(BNK부산), 예경탁(BNK경남), 정일선/고병일(JB광주), 박춘원/백종일(JB전북), 이희수(제주) |
| 카드 8사 (8) | 박창훈(신한), 김재관(KB국민), 김이태(삼성), 정태영(현대), 정상호(롯데), 진성원(우리), 성영수(하나), 김영우(BC) |
| 생보 5사 (5) | 홍원학(삼성), 여승주(한화), 신창재(교보), 천상영(신한라이프), 정문철(KB라이프) |
| 손보 5사 + 메리츠금융지주 (6) | 이문화(삼성화재), 정종표(DB), 이석현(현대해상), 구본욱(KB손보), 김중현(메리츠화재), 김용범(메리츠금융지주) |

이찬우 중복 제외하면 실제 unique 48명.

### 수집 범위 (KEYWORDS 분류 — v5 신규 항목 ★ 표시)

농협·수협 계열 전체 / 5대 금융지주 / 시중은행 5사 / 지방 금융지주·은행(BNK·iM(구 DGB)·JB·제주·SC제일·씨티) / 인터넷은행 3사 / 생보 14사 / 손보 10사 / 저축은행 8사+중앙회 / 신협·새마을금고 / 카드 9사 / 규제·정책기관(금감원·금융위·예보·한은·금통위·금결원·KRX·코스콤·예탁원) / 협단체(은행연합회·여신협회·생보협회·손보협회·금융연구원·보험연구원·화보협회) / 통칭(금융권·은행권·보험권·금융사고) / 업종 주식(금융주·은행주·보험주·카드주·저축은행주·지주주) / 정책 프로그램(국민성장펀드·생산적 금융·포용금융·NH 상생성장 프로젝트) / **★ 정책 토픽(서민안정기금·서민금융·DSR·망분리·LTV·DTI·금융 당국·금융당국·대통령 공약·전세대출 규제·주담대 규제·가계대출 규제·대출 규제·대출 한도)** / 칼럼 시리즈 태그([금융 히스토리] 등)

### CLICKBAIT_PRODUCT_HINTS 18종

펀드, 예금, 적금, 대출, 신상품, 금융상품, 카드, 보험, 신탁, 주담대, 주택담보대출, 전세대출, 신용대출, 갈아타기, 회장 복귀, 회장직 복귀, 이사장 후보, 이사장 선출

---

## 9. RSS·네이버 수집 구성 (v5: 85개로 확장)

**Google 뉴스 RSS (78개)**:
- 농협 검색 4 + MTN 전용 1 + 농협 경영진 4
- 수협 검색 3 + 수협 경영진 2
- 5대 지주 검색 4 + 지방 지주 3
- 인터넷뱅크 3
- 5대 지주 회장 검색 4 + 지방 3대 지주 회장 검색 3
- 5대 시중은행장 검색 4 + 인터넷뱅크 대표 검색 3
- 카드 8사 사장 검색 8
- 생보 5사 검색 5
- 손보 5사 검색 5
- 지방은행 5사 행장 검색 5
- 업종 검색(손보/생보 실적·저축은행 건전성·카드사 실적) 4
- 규제기관(금감원·금융위·예보) 3
- 단독 [단독] 검색(은행/금융/보험/카드) 4
- 정책 토픽(DSR·망분리·가계대출 규제·전세대출 규제·서민금융 정책) 5

**직접 RSS (6개사)**: 연합뉴스(economy/society/industry), 뉴시스(economy/bank), 매일경제, 머니투데이, 파이낸셜뉴스(economy/finance), 서울경제(finance), 아시아경제

**네이버 API (13 쿼리)**: 농협 4·수협 3·지주 4·금감원·금융위 — 회당 20건, 최신순. `originallink` 우선 사용

---

## 10. LLM 프롬프트 (`_llm_filter`)

모델 `claude-3-5-haiku-20241022`, 이진 판단. 프롬프트 전문은 코드 144~216행 참조 (수정 시 코드가 기준).

**YES**: 실적 / 규제·제재 / 주요 인사 / 비리·수사 / 시장·건전성 분석 / 증자·자본정책(CET1·RWA) / 농협 지배구조·선출제도 / 중앙회장·행장 정책 발표 / 경영진 비판 보도

**NO**: 광고·모델·브랜드 / 수상·감사패 / 1차산업 현장 활동·봉사 / MOU·협약 / 교육·캠페인·행사 / 지역사회 활동 / 금융 무관(농산물·전기차 등) / 정치·선거 / **증권·운용 단독**(지주 동반 기사는 YES) / 상품 출시 홍보(공적 펀드 시장 반응은 YES) / 지역 단위 농·축·수협 PR / 행장·임원 동정 / ESG 일자리 홍보 / 세미나·머니쇼 / 비전 선포 클리셰 / 칼럼·기고 시리즈 / IT 인프라 행정 / 내부통제 행사 개최

프롬프트 수정 시: NO 카테고리에 구체 예시를 함께 넣는 방식이 효과적.

---

## 11. 차단 목록 (BLOCKED)

**BLOCKED_DOMAINS (10)**: youngnong.co.kr, pinpointnews.co.kr, newsworker.co.kr, thefirstmedia.net, gukjenews.com, jndn.com, woryesanup.co.kr, newsquest.co.kr, insnews.co.kr, aflnews.co.kr

**BLOCKED_SOURCE_NAMES (18)**: 핀포인트뉴스, 영농뉴스, 원예산업신문, 뉴스워커, 더퍼스트미디어, 국제뉴스, 전남도민뉴스, 뉴스퀘스트, 시민행정신문, 경기경제신문, 안전신문, 농수축산신문, 일간경기, gmitoday, 위즈뉴스, 투데이안, 뉴스포스트, 팜인사이트

⚠️ 도메인 차단도 substring 매치. 짧은 도메인 추가 시 다른 도메인에 포함되는지 확인.

---

## 12. 패치 이력 요약 (v4 이후 추가분 + 기존)

### v5 (2026-06-15 ~ 16)

- **06-15**: 당국 보도자료 dedup 강화 — `_is_similar_title`에 동적 임계값 (양쪽 _AGENCY_NAMES 포함 시 min_matches=2). seen_titles 100→500. PR 3건 차단 (케뱅 무신사·BNK 내부통제·새마을금고 반려로봇).
- **06-16 (오전)**: 박영선 정치 발언은 추적 제외. 5대 지주 회장 EXEC_NAMES 동등화 (양종희/진옥동/함영주/임종룡 추가). 단독 검색 RSS 4개 추가. 12건 차단 (모호 시리즈 [금융계 동향]/[오늘의 은행]/[특징주], PR 9건).
- **06-16 (점심)**: EXEC_NAMES 8대 지주 + 산하 금융사 CEO 전수 확대 — 23명 → 48명. 지방은행장 5+legacy 2 / 카드 8 / 생보 5 / 손보 5+1.
- **06-16 (오후)**: 초청 특강·사진 동정·신뢰 재건 3건 차단 (황병우 영남대·이호성 사진=·롯데카드 해킹).
- **06-16 (오후)**: 정책 토픽 KEYWORDS+WHITELIST+RSS 보강 (DSR/망분리/서민안정기금/LTV/DTI/족쇄/데드라인/유예/발등에 불). 6/14 누락 대응.
- **06-16 (오후)**: 폴링 발송 한도 10→30건 + asyncio.sleep(1). PR 3건 차단 (SBI저축은행 시스템 도입·여름 휴가 카드·중도금 무이자).
- **06-16 (오후)**: 도서전 참여·통합치료 특약 2건 차단.

### v4 (2026-06-11) 이전

- **05-22~23**: 농협·수협 → 전 금융권 확장, DGB→iM, 채널/1:1 분리, 폴링 30→15분, 협단체·한은 추가, 클릭베이트 fallback, [속보] SCOOP 제외
- **05-25~27**: aflnews 차단, MTN 칼럼 통과, 묶음 PR 태그 차단, `합병`↔`종합병원` substring 버그 수정, 메시지 포맷 개편, [단독] fallback, 동정·화보 패턴 차단
- **05-31**: 금융주/은행주 KEYWORDS, 12건 누락 PR 종합 패치 (행장 동정·정기예금 PR·LLM NO 카테고리)
- **06-07**: 강원농협 자랑·금감원 점검 클릭베이트·시니어 카드 시리즈 차단

---

## 13. ⚠️ 알려진 한계·함정

### 13-1. ✅ 폴링 발송 10건 제한 해소 (v5에서 해결)
이전 v4까지 `scheduled_check`가 `articles[:10]`로 11번째 이후 영구 미발송. v5에서 **30건 + asyncio.sleep(1)** 로 변경. 텔레그램 rate-limit(분당 20건/채널) 안전 마진 + 도배 방지. 30건 다 발송돼도 약 30초.

### 13-2. `/news` 명령이 채널 발송분을 가로챔
`/news`는 `fetch_new_articles()`를 직접 호출해 seen을 소모. 수집된 기사는 **명령 입력자 채팅창에만** 표시되고(v5: 최대 30건+sleep) 채널에는 영원히 안 나간다. 사용자가 /news 자주 치면 채널 누락처럼 보임.

### 13-3. 두더지 잡기 구조
PR 패턴은 무한 변형. 전 기사 LLM 통과 방안은 검토했으나 **사용자 결정으로 키워드 기반 유지**. 패치는 §16 SOP대로.

### 13-4. LLM fail-open + 휘발성 캐시
API 키 미설정·오류·크레딧 소진 시 LLM은 전부 통과 처리. PR이 갑자기 새면 `~/bot.log`에서 "LLM 필터 오류" 확인. 캐시는 메모리라 재시작 시 초기화 → 재시작 직후 API 호출 급증은 정상.

### 13-5. 네이버 경로는 소스명 차단 미적용
네이버 수집에서 `_is_blocked_source(url)`로 호출 — `BLOCKED_SOURCE_NAMES`가 안 먹는다. 도메인 차단만 유효. 차단했는데 계속 나오는 매체가 있으면 유입 경로가 네이버인지 확인하고 **도메인을 BLOCKED_DOMAINS에 추가**.

### 13-6. substring 매치 전반
KEYWORDS/WHITELIST/PROMO/STRUCTURAL/도메인 전부 `in` 연산. 추가 전 §14-3 점검 필수. 역대 사고:
- `합병` ↔ `종합병원` (해결됨 — `인수합병`/`합병 발표` 등 구체 어구로)
- `한은` ↔ `한국은행` (substring 매치 안 됨 — _AGENCY_NAMES 별칭 처리로 해결)

### 13-7. 중복 탐지 양면성 (v5 완화)
- `_is_similar_title` min_matches=3 (당국명 매치 시 2). 너무 민감하면 다른 관점 후속 기사까지 차단.
- 비교 대상은 **최근 500개 제목** (v5: 100→500). 500건 이상 흐른 뒤 재등장 기사는 통과 (article_id가 같으면 그쪽에서 잡힘).
- **옛 기사 재발송 사례**: JTBC 강호동 뇌물 같은 케이스에서 RSS의 pub_date가 갱신되면 24h 컷오프를 우회. 500개 캐시로 어느 정도 방어.

### 13-8. 24시간 컷오프
발행 24h 경과 기사는 수집 제외. **늦게 발견한 누락 기사는 패치해도 재수집되지 않는다.** 패치는 "다음에 비슷한 기사가 올 때"용. 사용자에게 명확히 할 것.

### 13-9. Google RSS 딜레이
MTN 등 직접 RSS 없는 매체는 Google 의존이라 반영 지연 가능. 경영진 실명 RSS·네이버 API로 보완 중이나 완벽하지 않음.

### 13-10. requirements.txt는 배포로 반영 안 됨
deploy.yml은 main.py·collector.py만 checkout. 새 패키지 의존성 추가 시 서버 SSH로 직접 `pip install` 필요.

### 13-11. EXEC_NAMES 인사 변동 위험 (v5 신규)
48명 등록 후 인사 발표 시 명단 갱신 필요. 임기 만료 패턴:
- 시중은행장: 보통 2년 임기, 1년 연임
- 카드사·보험사 사장: 보통 2~3년
- 금융지주 회장: 3년 임기
JB광주은행(정일선/고병일), JB전북은행(박춘원/백종일)은 인사 과도기라 신임+legacy 둘 다 등록. 향후 정리되면 legacy 제거.

---

## 14. 패치 전 필수 회귀 테스트 (복붙 실행용)

### 14-1. 문제 기사 역추적

```bash
cd /tmp/go_deploy/news_bot && python3 -c "
from collector import *
title = '여기에 문제 기사 제목'
summary = '본문 요약 (없으면 빈 문자열)'
url = 'https://기사URL'
print('blocked_source:', _is_blocked_source(url, ''))
print('title KEYWORDS:', [k for k in KEYWORDS if k in title])
print('CLICKBAIT hit:', [p for p in CLICKBAIT_PRODUCT_HINTS if p in title])
print('STRUCTURAL hit:', [p for p in STRUCTURAL_PROMO_PATTERNS if p in title])
print('PROMO hit:', [k for k in PROMO_KEYWORDS if k in title])
print('WHITELIST hit:', [k for k in WHITELIST_KEYWORDS if k in title])
print('EXEC hit:', _has_exec_name(title))
print('is_relevant:', is_relevant(title, summary))
print('trusted:', _is_trusted_source(url))
print('needs_llm:', not _is_trusted_source(url) or _has_exec_name(title) or _is_clickbait_pass(title))
"
```

### 14-2. 패치 후 회귀

```bash
cd /tmp/go_deploy/news_bot && python3 -c "
from collector import is_relevant
cases = [
    ('차단하려는 PR 기사 제목', False),
    ('NH농협은행, 3분기 순이익 5000억', True),
    ('금감원, 저축은행 PF 부실 점검 착수', True),
    ('[단독] 우리금융 회장 교체 검토', True),
    ('KB금융, 1조원 유상증자 결정', True),
    ('KB증권 1분기 순이익 800억', False),
    ('신한은행, MOU 체결로 ESG 경영 강화', False),
]
fails = [(t, e, is_relevant(t)) for t, e in cases if is_relevant(t) != e]
print('ALL PASS' if not fails else fails)
"
```

### 14-3. 새 키워드 substring 충돌 점검 (필수)

```bash
cd /tmp/go_deploy/news_bot && python3 -c "
new_kw = '추가하려는 키워드'
from collector import KEYWORDS, WHITELIST_KEYWORDS, PROMO_KEYWORDS, STRUCTURAL_PROMO_PATTERNS, EXEC_NAMES
for name, lst in [('KEYWORDS',KEYWORDS),('WHITELIST',WHITELIST_KEYWORDS),
                  ('PROMO',PROMO_KEYWORDS),('STRUCTURAL',STRUCTURAL_PROMO_PATTERNS),
                  ('EXEC',EXEC_NAMES)]:
    hits = [k for k in lst if new_kw in k or k in new_kw]
    if hits: print(name, hits)
"
```

머릿속 점검도 병행: `합병`→종합병원, `금고`→새마을금고, `출시`→재출시·첫 출시, 인물명 동명이인 가능성 등.

### 14-4. 문법 오류 최종 확인

```bash
cd /tmp/go_deploy/news_bot && python3 -m py_compile collector.py main.py && echo SYNTAX_OK
```

---

## 15. 배포 후 검증

```bash
# 1. Actions 성공 + BOT_RUNNING_OK 확인 (저장소 공개라 PAT 없이도 조회 가능)
curl -s "https://api.github.com/repos/won3ho-max/go/actions/runs?per_page=1" | python3 -c "
import json,sys; r=json.load(sys.stdin)['workflow_runs'][0]
print(r['name'], r['status'], r['conclusion'], r['head_sha'][:7])"

# 2. 서버 상태
ssh won3ho@34.50.62.215 "sudo systemctl is-active news_bot && tail -5 ~/bot.log"

# 3. 텔레그램: ADMIN 1:1로 '배포 완료 테스트' 메시지 도착 확인
```

추가로, 푸시한 커밋이 실제 main에 반영됐는지 확인 (`git log origin/main -1` — 다른 시스템 커밋에 묻혀 rebase 누락되는 사고 방지).

---

## 16. 유지보수 SOP

### A. "이 PR 기사가 채널에 떴다" (오발송)

1. §14-1로 역추적 → 어느 단계에서 새는지 특정
2. 패치 위치 결정:
   - 매체 자체 → BLOCKED_DOMAINS (+ 네이버 유입이면 도메인 필수)
   - Google RSS 소스명만 잡힘 → BLOCKED_SOURCE_NAMES
   - 제목 PR 어구 → STRUCTURAL_PROMO_PATTERNS
   - 단어 하나로 차단 가능 → PROMO_KEYWORDS
   - LLM 오판 → LLM 프롬프트 NO 카테고리 구체 예시 추가
3. §14-2 + §14-3 + §14-4 통과 후 §3 절차로 배포 → §15 검증

### B. "이 기사가 누락됐다"

1. 먼저 필터 문제인지 확인: §14-1에서 `is_relevant=True`인데 안 왔다면 필터가 아니라 **§13-2(/news 가로채기) / §13-7(유사제목 차단) / §13-9(RSS 딜레이) / 24h 컷오프** 중 하나. 키워드 패치 불필요. (v5에서 §13-1 10건 제한은 해소됨)
2. `is_relevant=False`라면:
   - 제목에 회사명 없음 → KEYWORDS 추가 검토 (substring 점검)
   - 클릭베이트형 제목 → CLICKBAIT_PRODUCT_HINTS 추가
   - KEYWORDS는 맞는데 5단계에서 죽음 → WHITELIST_KEYWORDS 추가
   - STRUCTURAL/PROMO가 오차단 → 해당 패턴을 더 구체적인 어구로 정밀화
3. 사용자에게 고지: 해당 기사 자체는 24h 컷오프로 재수집 불가, 패치는 향후분부터 적용

### C. "같은 보도자료가 여러 번 떴다" (v5 신규)

1. 양쪽 제목 모두 _AGENCY_NAMES(금감원·금융위·한은 등) 포함하는지 확인
2. 그렇다면 _is_similar_title 동적 임계값(min_matches=2) 작동했어야
3. 그래도 새면 토픽이 너무 달라서 단어 매치 자체가 0~1개 — 이 경우 KEYWORDS 통과는 정당. 같은 사건 여러 매체 후속 기사로 간주.

### D. 봇이 멈췄다 / 이상 동작

```bash
ssh won3ho@34.50.62.215
sudo systemctl status news_bot
tail -50 ~/bot.log
# 중복 인스턴스 의심 시: ps aux | grep main.py (락이 있어 정상이면 1개)
# LLM 오류 다발 시: API 키·크레딧 확인 (§13-4)
```

### E. EXEC_NAMES 인사 변동 시 (v5 신규)

1. 새 CEO 실명 확인 (BotFather 같은 외부 기관 인사 발표 페이지)
2. EXEC_NAMES + WHITELIST_KEYWORDS 양쪽에 추가 (제목 매치 통과 + LLM 강제 검증)
3. RSS_FEEDS에 `"<신임 이름>+<회사명>"` Google 검색 추가
4. 동명이인 위험 점검 — §14-3 substring 점검 필수
5. 전임자는 1년 정도 legacy로 유지 (인사 과도기 기사 흡수)

---

## 17. TRUSTED_DOMAINS 명단 (107개 — 분류 요약)

| 분류 | 매체 |
|---|---|
| 통신사 (4) | 연합뉴스, 뉴시스, 뉴스1, 연합인포맥스 |
| 경제 대형 (24) | 매일경제, 한국경제, 서울경제, 머니투데이, 파이낸셜뉴스, 아시아경제, 이데일리, 헤럴드경제, 이투데이, 뉴스핌, 더벨, 비즈니스포스트, 아이뉴스24, 아주뉴스, FN투데이, 한국금융신문, 비즈워치, 머니S, 뉴스토마토, 딜사이트, 인베스트조선, 이코노미스트, 시사저널, 시사인 |
| 경제 중형 (12) | seoulfn, efnews, joseilbo, taxtimes, insure, aitimes, econovill, forbes, thescoop, newdaily, polinews, newsworks |
| 종합일간지 (15) | 조선, 중앙, 동아, 한겨레, 경향, 국민, 세계, 문화, 한국, 내일, 쿠키, 프레시안, 오마이, 미디어오늘, 데일리안 |
| 방송 (18) | YTN, MBC, KBS, SBS, JTBC, TV조선, 채널A, MBN, MTN, 한국경제TV, **서울경제TV(sentv — 차단 금지)**, 매일경제TV, 연합뉴스TV, CBS, 노컷, TBS, OBS, 아리랑 |
| IT 전문 (11) | 전자신문, ZDNet, 블로터, 디지털데일리, 보안뉴스, 디지털타임스, IT데일리, AI타임스, 벤처스퀘어, 플래텀, 아이로봇뉴스 |
| 지역 (17) | 부산일보, 국제, 경남, 영남, 매일, 대전, 충청투데이, 광주, 전남, 전북도민, 도민, 강원, 강원도민, 제주, 한라, 인천, 경기, 중도 |
| 기타 (2) | 더팩트, 뉴스와이어 |

정확한 도메인 목록은 코드 21~66행이 기준.

---

## 18. 이 문서의 유지 규칙

- 패치할 때마다 §12 이력에 한 줄 추가하고, 상수 개수가 바뀌면 §8 표를 갱신할 것
- 새 함정·사고를 발견하면 §0 금지 목록 또는 §13에 **즉시** 추가할 것 — 이 문서의 존재 이유다
- 갱신본은 저장소 루트 `HANDOVER.md`로 커밋 (news_bot/ 밖이므로 배포 트리거 안 됨 — 정상)

---

## 19. v5 핵심 변경 요약 (Opus 4.8 인수 받는 이를 위한 quick reference)

v4 (2026-06-11) 이후 다음이 바뀌었다. 변경 전 가정이 있다면 갱신할 것:

1. **EXEC_NAMES 10명 → 48명**: 8대 지주 회장 전수 + 산하 시중은행장 + 인터넷뱅크 + 카드 8 + 생보 5 + 손보 5 + 메리츠금융지주 부회장. 농협 특별 취급 완전 해소.
2. **폴링 발송 10건 → 30건 + asyncio.sleep(1)**: HANDOVER §13-1 잠재 빵꾸 해소. `/news`도 동일 변경.
3. **seen_titles 캐시 100 → 500**: 옛 기사 RSS 재발송 방지 (강호동 뇌물 의혹 사례).
4. **`_is_similar_title` 동적 임계값**: `_AGENCY_NAMES` 양쪽 매치 시 min_matches=3→2. 별칭(`한국은행`↔`한은`) substring 매치 안 돼도 잡힘.
5. **정책 토픽 KEYWORDS 신규**: DSR, 망분리, LTV, DTI, 서민안정기금, 서민금융, 금융 당국, 금융당국, 대통령 공약, 전세대출/주담대/가계대출/대출 규제, 대출 한도.
6. **정책 긴급성 WHITELIST 신규**: 족쇄, 데드라인, 유예, 발등에 불, 유예 종료, 규제 완화/강화/시행.
7. **RSS_FEEDS 39 → 85**: 단독 검색 4 + 정책 토픽 5 + 지방은행 5 + 카드 8 + 생보 5 + 손보 5 + 지방지주 3 + 시중은행장 4 + 인터넷뱅크 3 등.
8. **STRUCTURAL 다수 신규**: 초청 특강·금융의 사회적 책임·[사진=·사고 딛고·신뢰 재건·서울국제도서전·도서관과·통합치료 특약·진단금 넘어·시스템 도입·어떤 카드·여름 휴가·중도금 무이자·분양 단지 등.
