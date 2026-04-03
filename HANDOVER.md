# 인수인계 문서 — 농협 뉴스 모니터링 봇

## 현재 상태 요약

| 항목 | 상태 |
|------|------|
| 코드 | ✅ 정상 (브랜치에 push 완료) |
| 서버 SSH | ❌ 불가 (SSH 데몬 다운) |
| 봇 서비스 | ❓ SSH 불가로 확인 못함 |
| 다음 작업 | 네이버 뉴스 API 연동 (미완) |

---

## 저장소 / 브랜치

```
저장소: won3ho-max/go
개발 브랜치: claude/rebuild-news-monitoring-Ir4CE
서버 경로: /home/ubuntu/news_bot/
```

로컬 세팅:
```bash
git clone <repo>
git checkout claude/rebuild-news-monitoring-Ir4CE
```

---

## 파일 구조

```
news_bot/
├── main.py          # 텔레그램 봇, 스케줄러
├── collector.py     # 뉴스 수집, 필터링 (핵심 로직)
├── .env             # 환경변수 (서버에만 존재)
├── .env.example     # 환경변수 템플릿
├── requirements.txt
└── news_bot.service # systemd 서비스 파일
```

---

## 필터링 설계 원칙 (변경 금지)

**목적: 홍보성 기사 제거. 비위 기사 탐지가 목적이 아님.**

```python
# collector.py > is_relevant()
1. 농협 관련 키워드가 제목/요약에 있는가? → 없으면 탈락
2. 제목에 PROMO_KEYWORDS가 있는가?       → 있으면 탈락
3. 나머지는 모두 통과
```

**⚠️ 절대 금지**: 농협 계열사 명칭에 포함된 단어를 PROMO_KEYWORDS에 추가하지 말 것.
- 예) `'손해'` 추가 시 → 농협손해보험 관련 기사 전부 차단됨
- 계열사: 농협은행, 농협생명, **농협손해보험**, 농협카드, 농협금융지주

---

## 스케줄 (KST)

| 시각 | 동작 |
|------|------|
| 매 30분 | 뉴스 수집 및 발송 |
| 22:00~06:00 | 수집하되 발송 안 함 → pending_articles.json 저장 |
| 06:00 | 밤새 쌓인 기사 일괄 발송 |
| 09·12·15·20시 | "✅ 정상 작동 중" 하트비트 메시지 |

---

## 환경변수 (.env)

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
CHECK_INTERVAL_MINUTES=30
NAVER_CLIENT_ID=M7NrZp_MQsCnK8BcysAg   ← 발급 완료, 코드 미반영
NAVER_CLIENT_SECRET=...                  ← 서버 .env에 직접 입력 필요
```

---

## 즉시 해야 할 작업 (미완료)

### 🔴 1순위: 서버 SSH 복구
서버(140.245.66.213)의 SSH 데몬이 꺼져 있어 접속 불가.

**해결 방법:**
1. https://cloud.oracle.com 로그인
2. Compute → Instances → 인스턴스 클릭
3. **Reboot** 버튼 클릭 → 2~3분 대기
4. 재부팅 후 SSH 재시도:
   ```powershell
   ssh -i C:\Users\USER\.ssh\oracle_vm.pem ubuntu@140.245.66.213
   ```
5. 접속 성공 시 SSH 영구 활성화:
   ```bash
   sudo systemctl enable ssh
   sudo systemctl start ssh
   ```

### 🟡 2순위: 네이버 뉴스 API 연동 (코드 작업)
**배경**: RSS 피드에 없는 언론사(MTN 등) 기사 수집 누락 문제.
**해결책**: 네이버 뉴스 검색 API로 키워드 기반 수집 추가.

- API 키 발급: ✅ 완료 (Client ID: `M7NrZp_MQsCnK8BcysAg`)
- Client Secret: 별도 확인 필요 (네이버 개발자 콘솔)
- 구현 위치: `collector.py`에 `fetch_from_naver()` 함수 추가 후 `fetch_new_articles()`에 통합
- API 엔드포인트: `https://openapi.naver.com/v1/search/news.json`
- 검색 키워드: KEYWORDS 리스트 순회 (`농협`, `NH농협은행`, `농협중앙회` 등)
- 중복 제거: 기존 `seen_articles.json` 그대로 활용

### 🟢 3순위: 서버 .env에 네이버 API 키 추가
SSH 복구 후:
```bash
nano /home/ubuntu/news_bot/.env
# 아래 두 줄 추가
NAVER_CLIENT_ID=M7NrZp_MQsCnK8BcysAg
NAVER_CLIENT_SECRET=<시크릿값>
```

---

## 배포 방법

```bash
ssh -i C:\Users\USER\.ssh\oracle_vm.pem ubuntu@140.245.66.213
cd /home/ubuntu/news_bot
git pull origin claude/rebuild-news-monitoring-Ir4CE
sudo systemctl restart news_bot
sudo systemctl status news_bot
```

---

## 최근 커밋 이력

| 커밋 | 내용 |
|------|------|
| cc75235 | MTN 기사 수집 추가 + 계열사 키워드 오용 방지 주석 |
| 2970fe1 | 홍보성 키워드 추가 (협약·시상·농산물홍보 등) |
| 21b9d40 | 24시간 이상 된 기사 필터링 + CRITICAL_KEYWORDS 버그 수정 |
| 2d610b8 | 필터 설계 원칙 전환: 비위 탐지 → 홍보성 제거 |
</content>
</invoke>