import feedparser
import requests
import hashlib
import json
import os
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# 네이버 뉴스 검색 API 설정
NAVER_CLIENT_ID = os.getenv('NAVER_CLIENT_ID', '')
NAVER_CLIENT_SECRET = os.getenv('NAVER_CLIENT_SECRET', '')
NAVER_API_URL = 'https://openapi.naver.com/v1/search/news.json'

# 네이버 API로 검색할 쿼리 목록 (RSS 커버리지 보완용)
NAVER_SEARCH_QUERIES = [
    '농협',
    'NH농협은행',
    '농협중앙회',
    '농협금융',
]

KEYWORDS = ['농협', 'NH농협', '농협은행', '농협중앙회', '농협금융', '농협생명', '농협손해보험', '농협카드']

# ─────────────────────────────────────────────────────────────────
# 필터링 설계 원칙 (v2: 화이트리스트 우선 방식)
#
# 기존 방식(블랙리스트 단독)의 문제:
#   → 홍보성 패턴은 무한히 변형되므로, 새 패턴이 나올 때마다 수동 추가 필요
#
# 개선 방식:
#   1단계: 농협 관련 기사인지 확인 (기존 유지)
#   2단계: 화이트리스트 — 영양가 있는 키워드가 하나라도 있으면 즉시 통과
#   3단계: 구조적 홍보성 패턴 차단 (포토/행사/지역활동 등)
#   4단계: 기존 블랙리스트 (보완용)
#
# 핵심: "막을 것"을 정의하는 대신 "통과시킬 것"을 먼저 정의한다.
# ─────────────────────────────────────────────────────────────────

# 화이트리스트 — 이 중 하나라도 포함되면 영양가 있는 기사로 즉시 통과
# ⚠️ 계열사 명칭 포함 단어 추가 금지 (예: '손해', '생명')
WHITELIST_KEYWORDS = [
    # 금융·경영 실적 및 분석
    '금리', '실적', '순이익', '영업이익', '당기순이익', '자산', '부실', '부실대출',
    '여신', '수신', '대출', '예금', '연체', '적자', '흑자', '매출', '수익성',
    'BIS', '자본비율', '건전성', '유동성',
    # 실적 달성·규모
    '돌파', '달성', '사상 최대', '역대 최대', '최대 실적', '기술금융',
    # 시장 분석 (분석/통계 기사) — 너무 넓은 단어('시장','발표')는 제외
    '상승', '하락', '증가', '감소', '상승률', '증감율', '증감',
    '점유율', '금융시장', '자금시장', '채권시장',
    '공시', '평가', '분석', '전망', '영향',
    '리스크', '회계', '진단',
    # 사건·사고·논란·비리 (뉴스 가치)
    '제재', '처벌', '징계', '조사', '수사', '압수수색', '검찰', '경찰',
    '논란', '의혹', '피해', '소송', '분쟁', '고발', '고소',
    '횡령', '배임', '비리', '부정', '사기', '불법',
    '투서', '내부고발', '감시', '적발',
    '사고', '사건', '장애', '오류', '먹통',
    # 인사·조직
    '행장', '조합장', '대표이사', '취임', '선임', '연임', '해임', '사임', '사퇴',
    '임원', '이사회', '주주총회', '감사',
    # 정책·규제
    '금감원', '금융위', '기획재정부', '규제', '개정', '의무화', '금지',
    '감독', '검사', '제도 변경', '기준금리',
    # 시장·전략
    'IPO', '상장', '합병', '인수', '매각', '분사', '구조조정',
    '금융사고', '내부통제',
]

# 구조적 홍보성 패턴 — 제목 시작 또는 특정 형태로 판별
# (개별 단어가 아닌 "문장 패턴"으로 탐지)
STRUCTURAL_PROMO_PATTERNS = [
    # 포토·영상·동정 기사 (시각 자료 중심·인물 공지)
    '[포토]', '[포토뉴스]', '[영상]', '[동정]',
    # 행사 동사 조합 (지역명 + 행사)
    '행사 실시', '행사 개최', '행사를 개최', '행사를 실시',
    '발대식', '출범식', '창립총회', '기념식',
    # 지역 단위 홍보
    '지부,', '지점,', '센터,', '지역본부,',
    # 운영 공지성
    '왕진버스', '무료가입', '무료 가입',
    # 농산물·지역 소비
    'K-푸드', '농산물 판매', '직거래',
    # 예방 캠페인성 (사고·사기 예방 특강·홍보 등 — 실제 사고 기사가 아님)
    '예방 특강', '피해 예방', '예방 총력', '예방에 앞장', '예방 캠페인',
    '예방 교육', '예방 활동', '예방 홍보',
    # 지역 정례 행사·단체 총회
    '정례조회', '월례회', '도민체전', '정기총회', '건의문 채택',
    # 교육·연수 행사 (여신·수신 등 금융 용어가 포함돼도 행사성이면 차단)
    '교육 실시', '교육을 실시', '교육 진행', '역량 강화 교육', '실무역량',
    '연수 실시', '워크숍 개최', '세미나 개최',
    # 스포츠·친목 행사
    '파크골프', '골프대회', '골프 대회', '체육대회', '체육 대회',
    '등산대회', '마라톤대회',
    # 무료 서비스·지원 (보이스피싱 보험 무료 지원, 무료 배포 등 캠페인성)
    '무료 지원', '무료 제공', '무료 배포', '무료 서비스',
    '취약계층 대상', '고령층 대상', '취약계층에게',
    # 복지·사회공헌 지원 (경로당, 간식, 물품 배부 등)
    '경로당', '간식 지원', '물품 지원', '물품 전달', '경로잔치',
    '쌀 전달', '사료 지원', '지원 온정', '재해 구호', '피해 농가',
    '야광반사판', '농기계 안전', '안전 교육', '사고예방용',
    '저온피해', '저온 피해', '냉해', '고온피해', '병해충',
    # 농가 피해·지원 활동 (저온피해, 가뭄 등 자연재해 대응 공지)
    '저온피해', '저온피해 예방', '저온피해 극복', '저온 피해',
    '가뭄', '냉해', '병해충', '고온피해',
    # 정책·캠페인성 (자율 시행, 차량 2부제 등 정부 정책 PR)
    '차량 2부제', '자율 시행', '자율 참여', '자율 실천',
    # 농산물·지역 홍보 행사
    '농산물 홍보', '홍보 부스', '홍보 행사', '홍보관 운영',
    '축제서 ', '축제에서 ',
]

# 블랙리스트 (기존 유지, 보완용)
# ⚠️ 계열사 명칭 포함 단어 절대 추가 금지
PROMO_KEYWORDS = [
    # 신상품·서비스 출시
    '출시', '론칭', '새로 선보', '새롭게 선보', '신상품', '신규 출시',
    # 마케팅·이벤트
    '이벤트', '캠페인', '프로모션', '할인', '경품',
    # 협약·제휴
    'MOU', '업무협약', '협약 체결',
    # 수상·인증
    '수상', '최우수상', '우수상', '시상식', '인증 획득', 'AWARDS', '어워즈',
    '조합장상', '○○상', '선정', '선정', '수상 기업', '수상 지점',
    # 인물 탐방·칼럼 시리즈
    '파수꾼', '현장의 파수꾼', '사람들', '인물탐방', '인물 탐방',
    # 사회공헌·봉사
    '기부', '봉사', '사회공헌', 'CSR', '나눔', '후원', '일손돕기',
    # 채용
    '채용 설명회', '인턴 모집', '공채',
    # 소비촉진
    '소비촉진', '소비 촉진', '하나로마트',
    # 영농·지역
    '영농지도', '영농지원',
    # 광고
    '모델 발탁',
]

RSS_FEEDS = [
    # Google 뉴스 (농협 검색)
    "https://news.google.com/rss/search?q=농협&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=NH농협은행&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=농협중앙회&hl=ko&gl=KR&ceid=KR:ko",
    # Google 뉴스 — RSS 없는 언론사 보완 (MTN 머니투데이방송 등)
    "https://news.google.com/rss/search?q=농협+site:news.mtn.co.kr&hl=ko&gl=KR&ceid=KR:ko",
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
    # 1단계: 제목에 농협 관련 키워드가 있어야 통과
    # (summary에만 '농협카드' 등이 언급되는 업계 일반 기사 차단)
    if not any(kw in title for kw in KEYWORDS):
        return False
    # 2단계: 구조적 홍보성 패턴 즉시 차단 — 화이트리스트보다 우선 적용
    # (예: '조합장' 화이트리스트라도 '수상' 블랙리스트가 있으면 차단)
    if any(pattern in title for pattern in STRUCTURAL_PROMO_PATTERNS):
        return False
    # 3단계: 블랙리스트 차단 — 화이트리스트보다 우선 적용
    if any(kw in title for kw in PROMO_KEYWORDS):
        return False
    # 4단계: 화이트리스트 — 제목에 영양가 있는 키워드가 있어야 통과
    # (요약에만 금융 키워드가 언급되는 복지·홍보성 기사 차단)
    # [단독]/[속보] 취재 기사 즉시 통과
    if any(tag in title for tag in ['[단독]', '[속보]', '[단독보도]']):
        return True
    if any(kw in title for kw in WHITELIST_KEYWORDS):
        return True
    return False


def _strip_html(text: str) -> str:
    """네이버 API 응답의 간단한 HTML 태그 제거"""
    import re
    return re.sub(r'<[^>]+>', '', text).strip()


def fetch_from_naver(seen: set, cutoff: datetime) -> list:
    """네이버 뉴스 검색 API로 기사를 수집해 반환한다.

    NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 없으면 조용히 건너뜀.
    RSS로 커버되지 않는 언론사(MTN 등)의 기사를 보완하는 용도.
    """
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        logger.debug("네이버 API 키 미설정 — 건너뜀")
        return []

    headers = {
        'X-Naver-Client-Id': NAVER_CLIENT_ID,
        'X-Naver-Client-Secret': NAVER_CLIENT_SECRET,
    }
    articles = []

    for query in NAVER_SEARCH_QUERIES:
        try:
            params = {
                'query': query,
                'display': 20,   # 회당 최대 수집 건수
                'sort': 'date',  # 최신순
            }
            resp = requests.get(NAVER_API_URL, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            items = resp.json().get('items', [])

            for item in items:
                title = _strip_html(item.get('title', ''))
                url = item.get('originallink') or item.get('link', '')
                summary = _strip_html(item.get('description', ''))
                pub_date_str = item.get('pubDate', '')  # RFC 822 형식

                # 발행일 파싱 및 24시간 필터
                if pub_date_str:
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_dt = parsedate_to_datetime(pub_date_str).astimezone(timezone.utc)
                        if pub_dt < cutoff:
                            continue
                        published = pub_dt.strftime('%Y-%m-%d %H:%M KST')
                    except Exception:
                        published = pub_date_str
                else:
                    published = ''

                if not title or not url:
                    continue

                # 홍보성 필터 적용 (1단계 키워드 체크는 쿼리 자체가 이미 농협 한정이므로 생략 가능하나 일관성을 위해 유지)
                if not is_relevant(title, summary):
                    continue

                article_id = get_article_id(url, title)
                if article_id in seen:
                    continue

                seen.add(article_id)
                articles.append({
                    'title': title,
                    'url': url,
                    'summary': summary,
                    'published': published,
                    'source': item.get('link', '').split('/')[2] if item.get('link') else '',
                })

        except requests.exceptions.RequestException as e:
            logger.error(f"네이버 API 수집 오류 [query={query}]: {e}")

    logger.info(f"네이버 API — 새 기사 {len(articles)}건 수집")
    return articles


def fetch_new_articles():
    seen = load_seen()
    new_articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # ── 1. RSS 피드 수집 ─────────────────────────────────────────
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

    # ── 2. 네이버 뉴스 검색 API 수집 (RSS 미커버 언론사 보완) ──────
    naver_articles = fetch_from_naver(seen, cutoff)
    new_articles.extend(naver_articles)

    if new_articles:
        save_seen(seen)

    logger.info(f"총 새 기사 {len(new_articles)}건 수집 (RSS + 네이버 API)")
    return new_articles


def _to_kst_str(published: str) -> str:
    """발행 시각을 'M월 D일 오전/오후 H:MM' 형식(KST)으로 변환"""
    if not published:
        return ''
    try:
        from zoneinfo import ZoneInfo
        KST = ZoneInfo('Asia/Seoul')
        # 네이버 API 형식: "2026-04-05 20:02 KST"
        if published.endswith('KST'):
            dt = datetime.strptime(published, '%Y-%m-%d %H:%M KST').replace(tzinfo=KST)
        else:
            # RSS RFC 822 형식: "Sun, 05 Apr 2026 22:00:00 GMT"
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(published).astimezone(KST)
        h, m = dt.hour, dt.minute
        ampm = '오전' if h < 12 else '오후'
        h12 = h % 12 or 12
        return f"{dt.month}월 {dt.day}일 {ampm} {h12}:{m:02d}"
    except Exception:
        return published


def format_article(article):
    title = article['title']
    url = article['url']
    published = article.get('published', '')

    time_str = _to_kst_str(published)

    lines = [f"📰 <b>{title}</b>"]
    if time_str:
        lines.append(time_str)
    lines.append(url)

    return '\n'.join(lines)
