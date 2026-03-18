import feedparser
import requests
import hashlib
import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

KEYWORDS = ['농협', 'NH농협', '농협은행', '농협중앙회', '농협금융', '농협생명', '농협손해보험', '농협카드']

# 비위·부정·재무 위험 관련 핵심 키워드 — 반드시 제목에 있어야 알림 발송
CRITICAL_KEYWORDS = [
    # 비위·범죄·수사
    '비위', '비리', '횡령', '배임', '사기', '부정', '부패', '불법', '탈세', '뇌물',
    '수사', '기소', '검찰', '경찰', '감사원', '금감원', '금융감독원', '압수수색',
    '고소', '고발', '소송', '재판', '유죄', '혐의', '피의자', '구속', '체포',
    # 징계·인사
    '징계', '해임', '파면', '정직', '강등', '경질', '낙하산', '관치',
    # 재무·경영 리스크
    '손실', '적자', '부실', '부채', '결손', '파산', '부도', '위기', '리스크',
    '과징금', '과태료', '제재', '영업정지',
    # 갈등·논란
    '논란', '갈등', '반발', '항의', '파업', '노조', '임금체불',
    '의혹', '폭로', '내부고발',
]

# 단순 홍보성 기사를 걸러내는 키워드
PROMO_KEYWORDS = [
    '출시', '론칭', '오픈', '이벤트', '캠페인', '프로모션', '할인', '혜택',
    '기부', '봉사', '협약', 'MOU', '업무협약', '사회공헌', 'CSR',
    '수상', '선정', '인증', '대상 수상', '최우수', '우수상',
    '신상품', '신서비스', '새로 출시', '새롭게 선보',
]

RSS_FEEDS = [
    # Google 뉴스 (농협 검색)
    "https://news.google.com/rss/search?q=농협&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=NH농협은행&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=농협중앙회&hl=ko&gl=KR&ceid=KR:ko",
    # 연합뉴스
    "https://www.yna.co.kr/rss/economy.xml",
    "https://www.yna.co.kr/rss/society.xml",
    "https://www.yna.co.kr/rss/industry.xml",
    # 뉴시스
    "https://www.newsis.com/RSS/economy.xml",
    "https://www.newsis.com/RSS/bank.xml",
    # 매일경제
    "https://www.mk.co.kr/rss/30100041/",
    # 머니투데이
    "https://rss.mt.co.kr/mt_news.xml",
    # 파이낸셜뉴스
    "https://www.fnnews.com/rss/r20/fn_realnews_economy.xml",
    "https://www.fnnews.com/rss/r20/fn_realnews_finance.xml",
    # 서울경제
    "https://www.sedaily.com/rss/finance",
    # 아시아경제
    "https://www.asiae.co.kr/rss/economy.htm",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(BASE_DIR, 'seen_articles.json')


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    seen_list = list(seen)[-2000:]
    with open(SEEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(seen_list, f, ensure_ascii=False)


def get_article_id(url, title):
    return hashlib.md5(f"{url}{title}".encode('utf-8')).hexdigest()


def is_relevant(title, summary=''):
    text = title + ' ' + summary
    # 1단계: 농협 관련 기사인지 확인
    if not any(kw in text for kw in KEYWORDS):
        return False
    # 2단계: 비위·부정 키워드가 제목 또는 요약에 있어야 함
    if not any(kw in text for kw in CRITICAL_KEYWORDS):
        return False
    # 3단계: 제목이 홍보성이고 제목에 비위 키워드가 없으면 제외
    # (요약에만 비위 단어가 있는 홍보성 기사 차단)
    if any(kw in title for kw in PROMO_KEYWORDS) and not any(kw in title for kw in CRITICAL_KEYWORDS):
        return False
    return True


def fetch_new_articles():
    seen = load_seen()
    new_articles = []

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get('title', '').strip()
                url = entry.get('link', '').strip()
                summary = entry.get('summary', '').strip()
                published = entry.get('published', '')

                # source: Google 뉴스는 entry.source.title, 언론사 직접 피드는 feed.feed.title
                source = ''
                if hasattr(entry, 'source') and hasattr(entry.source, 'title'):
                    source = entry.source.title
                elif hasattr(feed, 'feed') and hasattr(feed.feed, 'title'):
                    source = feed.feed.title

                if not title or not url:
                    continue

                if not is_relevant(title, summary):
                    continue

                article_id = get_article_id(url, title)
                if article_id in seen:
                    continue

                seen.add(article_id)
                text = title + ' ' + summary
                matched = [kw for kw in CRITICAL_KEYWORDS if kw in text]
                new_articles.append({
                    'title': title,
                    'url': url,
                    'summary': summary,
                    'published': published,
                    'source': source,
                    'matched_keywords': matched[:3],
                })

        except Exception as e:
            logger.error(f"피드 수집 오류 [{feed_url}]: {e}")

    if new_articles:
        save_seen(seen)

    logger.info(f"새 기사 {len(new_articles)}건 수집")
    return new_articles


def format_article(article):
    title = article['title']
    url = article['url']
    source = article.get('source', '')
    published = article.get('published', '')

    matched = article.get('matched_keywords', [])
    lines = [f"🚨 <b>{title}</b>"]
    if matched:
        lines.append(f"🔑 감지: {' · '.join(matched)}")
    if source:
        lines.append(f"📌 {source}")
    if published:
        lines.append(f"🕐 {published}")
    lines.append(f"🔗 {url}")

    return '\n'.join(lines)
