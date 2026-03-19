import feedparser
import requests
import hashlib
import json
import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

KEYWORDS = ['농협', 'NH농협', '농협은행', '농협중앙회', '농협금융', '농협생명', '농협손해보험', '농협카드']

# 홍보성 기사를 걸러내는 키워드 — 제목에 이 단어가 있으면 제외
# 설계 원칙: 비위 키워드를 찾는 게 아니라 홍보성 기사를 제거하는 것이 목적.
# 농협 관련 기사 중 홍보·마케팅·수상·협약 등 명백히 광고성인 것만 차단.
PROMO_KEYWORDS = [
    # 신상품·서비스 출시
    '출시', '론칭', '오픈', '새로 선보', '새롭게 선보', '신상품', '신서비스', '신규 출시',
    # 마케팅·이벤트
    '이벤트', '캠페인', '프로모션', '할인', '혜택', '적립', '경품',
    # 협약·제휴
    'MOU', '업무협약', '협약 체결', '업무 협약', '제휴 협약', '공동 협약',
    # 수상·인증·시상
    '수상', '대상 수상', '최우수상', '우수상', '수상자', '시상식', '시상', '선정됐', '선정되',
    '인증', '인증 획득', '인증 받', 'AWARDS', '어워즈',
    # 사회공헌·봉사·지역활동
    '기부', '봉사', '사회공헌', 'CSR', '나눔', '무료 지원', '후원', '일손돕기', '적립기금',
    # 채용·홍보성 인사
    '채용 설명회', '인턴 모집', '공채',
    # 농산물 판매·소비촉진
    '소비촉진', '소비 촉진', '하나로마트',
    # 영농 활동·지역 소식
    '영농지도',
    # 광고·모델
    '모델 발탁',
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
    # 2단계: 제목이 홍보성이면 제외
    # 설계 원칙: 비위 키워드 탐지가 아니라 홍보성 기사 차단이 목적.
    # 농협 관련 기사는 기본적으로 모두 수집하되, 명백한 광고·마케팅성만 제거.
    if any(kw in title for kw in PROMO_KEYWORDS):
        return False
    return True


def fetch_new_articles():
    seen = load_seen()
    new_articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get('title', '').strip()
                url = entry.get('link', '').strip()
                summary = entry.get('summary', '').strip()
                published = entry.get('published', '')

                # 24시간 이상 된 기사 제외
                published_parsed = entry.get('published_parsed')
                if published_parsed:
                    pub_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue

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
                new_articles.append({
                    'title': title,
                    'url': url,
                    'summary': summary,
                    'published': published,
                    'source': source,
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

    lines = [f"🚨 <b>{title}</b>"]
    if source:
        lines.append(f"📌 {source}")
    if published:
        lines.append(f"🕐 {published}")
    lines.append(f"🔗 {url}")

    return '\n'.join(lines)
