# 농협 뉴스 모니터링 봇 — 개발 가이드

## 프로젝트 개요
농협 관련 뉴스를 수집해 텔레그램으로 알림을 보내는 봇.

## 핵심 설계 원칙 (변경 금지)

### 필터링 철학
**비위·부정 키워드를 찾는 것이 목적이 아니다.**
농협 관련 기사를 최대한 수집하되, **명백히 홍보성인 기사만 제거**하는 것이 목적이다.

- ❌ 잘못된 접근: "비위 키워드가 있어야 통과"
- ✅ 올바른 접근: "홍보성 키워드가 없으면 통과"

과거에 CRITICAL_KEYWORDS를 만들어 이 중 하나가 있어야 통과하도록 했지만,
이는 오히려 관련 기사를 놓치게 만든다. 이 방식으로 되돌리지 말 것.

### `is_relevant()` 로직 (collector.py)
```
1. 농협 관련 키워드가 제목/요약에 있는가? → 없으면 탈락
2. 제목에 홍보성 키워드가 있는가? → 있으면 탈락
3. 나머지는 모두 통과
```

## 스케줄 (KST 기준)
- 매 30분: 뉴스 수집 및 발송
- 22:00~06:00: 수집은 하되 발송 안 함 → pending_articles.json에 저장
- 06:00: 밤새 쌓인 기사 일괄 발송
- 09:00, 12:00, 15:00, 20:00: "✅ 정상 작동 중입니다" 하트비트 메시지

## RSS 피드
- Google 뉴스 RSS 3개 (농협, NH농협은행, 농협중앙회 검색)
- 국내 언론사 직접 RSS: 연합뉴스, 뉴시스, 매일경제, 머니투데이, 파이낸셜뉴스, 서울경제, 아시아경제

## 서버 정보
- OS: Ubuntu, 유저: ubuntu
- 경로: /home/ubuntu/news_bot/
- 실행: systemd 서비스 (news_bot)
- 로그: /home/ubuntu/news_bot/bot.log
- 환경변수: /home/ubuntu/news_bot/.env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CHECK_INTERVAL_MINUTES)

## 배포 방법
```bash
ssh ubuntu@<서버IP>
cd /home/ubuntu/news_bot
git pull origin claude/rebuild-news-monitoring-Ir4CE
sudo systemctl restart news_bot
sudo systemctl status news_bot
```

## Git
- 저장소: won3ho-max/go
- 개발 브랜치: claude/rebuild-news-monitoring-Ir4CE
