import feedparser
import requests
import hashlib
import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

KEYWORDS = ['농협', 'NH농협', '농협은행', '농협중앙회', '농협금융', '농협생명', '농협손해보험', '농협카드']

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=농협&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=NH농협은행&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=농협중앙회&hl=ko&gl=KR&ceid=KR:ko",
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
    text = (title + ' ' + summary).lower()
    return any(kw in text for kw in KEYWORDS)


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

                source = ''
                if hasattr(entry, 'source') and hasattr(entry.source, 'title'):
                    source = entry.source.title

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

    lines = [f"📰 <b>{title}</b>"]
    if source:
        lines.append(f"📌 {source}")
    if published:
        lines.append(f"🕐 {published}")
    lines.append(f"🔗 {url}")

    return '\n'.join(lines)
