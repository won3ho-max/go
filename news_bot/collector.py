import feedparser
import requests
import hashlib
import json
import os
import re
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─── Anthropic API ────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

# 신뢰 출처 — 패턴 필터 통과 시 LLM 체크 없이 그대로 통과
# (주요 뉴스통신사·경제지·종합지·방송 등)
TRUSTED_DOMAINS = {
    # 뉴스통신사
    'yna.co.kr', 'newsis.com', 'news1.kr',
    # 경제·금융 전문지
    'mk.co.kr', 'hankyung.com', 'sedaily.com',
    'mt.co.kr', 'fnnews.com', 'asiae.co.kr',
    'edaily.co.kr', 'heraldcorp.com', 'etoday.co.kr',
    'newspim.com', 'thebell.co.kr', 'businesspost.co.kr',
    'inews24.com',
    # 종합일간지 계열
    'chosun.com', 'biz.chosun.com', 'joongang.co.kr',
    'donga.com', 'hani.co.kr', 'khan.co.kr',
    'kmib.co.kr', 'segye.com', 'munhwa.com',
    # 방송
    'ytn.co.kr', 'mbc.co.kr', 'kbs.co.kr',
    'sbs.co.kr', 'jtbc.co.kr', 'tvchosun.com',
    # IT·전문지
    'etnews.com', 'zdnet.co.kr', 'bloter.net',
    'ddaily.co.kr', 'boannews.com',
    # 기타 주요 매체
    'dailian.co.kr', 'wikileaks-kr.org',
    'sentv.co.kr', 'newsquest.co.kr',
}


def _get_domain(url: str) -> str:
    """URL에서 도메인 추출"""
    try:
        return urlparse(url).netloc.replace('www.', '')
    except Exception:
        return ''


def _is_trusted_source(url: str) -> bool:
    """신뢰 출처 여부 확인"""
    domain = _get_domain(url)
    return any(trusted in domain for trusted in TRUSTED_DOMAINS)


# LLM 결과 캐시 (같은 제목 반복 호출 방지)
_llm_cache: dict[str, bool] = {}


def _llm_filter(title: str, summary: str = '') -> bool:
    """Claude Haiku로 기사 뉴스 가치 판단.
    True = 통과 (뉴스 가치 있음)
    False = 차단 (홍보·행사·마케팅성)
    """
    if not ANTHROPIC_API_KEY:
        return True  # API 키 미설정 시 통과

    cache_key = title.strip()
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        prompt = f"""NH농협 관련 기사 제목을 보고 뉴스 가치를 판단하세요.

【통과 YES】금융 실적·정책·규제·사건사고·인사·시장분석·논란·의혹·수사 등 실질적 뉴스
【차단 NO】상품 출시·홍보·지역 행사·사회공헌·수상·캠페인·직원 미담·협약 체결·교육·봉사 등

제목: {title}
요약: {summary[:150] if summary else '없음'}

YES 또는 NO로만 답하세요."""

        msg = client.messages.create(
            model='claude-3-5-haiku-20241022',
            max_tokens=5,
            messages=[{'role': 'user', 'content': prompt}],
        )
        result = msg.content[0].text.strip().upper()
        passed = result.startswith('Y')
        _llm_cache[cache_key] = passed
        if not passed:
            logger.info(f"LLM 차단: {title[:60]}")
        return passed

    except Exception as e:
        logger.error(f"LLM 필터 오류: {e}")
        return True  # 오류 시 통과 (안전 방향)

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
    # 지자체 금고·계약 (수주전, 금고 심사 등 뉴스 가치 높음)
    '금고', '수의계약', '금고 운영', '금고 선정',
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
    '발대식', '발대', '출범식', '창립총회', '기념식',
    # 지역 단위 홍보
    '지부,', '지점,', '센터,', '지역본부,',
    # 운영 공지성
    '왕진버스', '무료가입', '무료 가입',
    # 농산물·지역 소비
    'K-푸드', '농산물 판매', '직거래',
    # 예방 캠페인성 (사고·사기 예방 특강·홍보 등 — 실제 사고 기사가 아님)
    '예방 특강', '피해 예방', '피해예방', '예방 총력', '예방에 앞장', '예방 캠페인',
    '예방 교육', '예방 활동', '예방 홍보', '예방영상', '홍보영상',
    # 보이스피싱·금융사기 차단 미담 (PR 기사) — 어미 변형 포함
    '피해 막아', '피해 막은', '피해 막았',
    '피싱 막아', '피싱 막은', '피싱 막았', '피싱 피해 방지',
    '금융사기 막아', '금융사기 막은', '금융사기 막았',
    '사기 막아', '사기 막은', '사기 막았',
    # 지역 정례 행사·단체 총회
    '정례조회', '월례회', '도민체전', '정기총회', '건의문 채택', '건의문 전달',
    # 교육·연수 행사 (여신·수신 등 금융 용어가 포함돼도 행사성이면 차단)
    '교육 실시', '교육을 실시', '교육 진행', '역량 강화 교육', '실무역량',
    '예방교육', '예방 교육 성료', '교육 성료',
    '연수 실시', '워크숍 개최', '워크숍 진행', '세미나 개최', '아카데미 개최', '아카데미 운영',
    # 스포츠·친목 행사
    '파크골프', '골프대회', '골프 대회', '체육대회', '체육 대회',
    '등산대회', '마라톤대회', '챔피언십',
    # 무료 서비스·지원 (보이스피싱 보험 무료 지원, 무료 배포 등 캠페인성)
    '무방문 대출', '무방문 신청',
    '무료 지원', '무료 제공', '무료 배포', '무료 서비스',
    '취약계층 대상', '고령층 대상', '취약계층에게',
    # 복지·사회공헌 지원 (경로당, 간식, 물품 배부 등)
    '경로당', '간식 지원', '물품 지원', '물품 전달', '경로잔치',
    '쌀 전달', '사료 지원', '지원 온정', '재해 구호', '피해 농가',
    '야광반사판', '농기계 안전', '안전 교육', '사고예방용',
    # 농가 피해·지원 활동 (저온피해, 가뭄 등 자연재해 대응 공지)
    '저온피해', '저온피해 예방', '저온피해 극복', '저온 피해',
    '가뭄', '냉해', '병해충', '고온피해',
    # 인물 동정 (현장 방문·점검)
    '현장 방문', '현장점검', '현장 점검', '격려 방문', '방문·격려',
    '현장경영', '현장 경영',
    '투자기업 방문', '투자기업 점검', '투자기업 지원 강화',
    '펀드 투자기업',
    # 지역 보조·지원 사업
    '보조사업 실시', '보조사업 추진', '지원사업 실시', '지원사업 추진',
    '경영지원사업', '바우처 사업',
    # 업계 순위·최초 홍보
    '업계 첫', '업계 최초', '점유율 선도', '점유율 1위',
    # 캠페인 활동 전파
    '농심천심', '운동 전파', '운동 확산',
    # 보험·상품 홍보성
    '보장 강화', '안전망 확대', '혜택 강화', '서비스 강화', '보장 확대',
    # 지역단위 성과 홍보
    '성과 달성', '최고 성과', '조기 달성', '달성 총력', '성과 가시화',
    '지역밀착 금융', '트리플 달성', '드문 성과', '체제 성장세',
    # 정책·캠페인성 (자율 시행, 차량 2부제 등 정부 정책 PR)
    '차량 2부제', '자율 시행', '자율 참여', '자율 실천',
    # 농산물·지역 홍보 행사
    '농산물 홍보', '홍보 부스', '홍보 행사', '홍보관 운영',
    '축제서 ', '축제에서 ',
    # 봉사·복지 동행 행사
    '동행식탁', '선한영향력',
    # 행사성 협의회
    '업무협의회 개최', '업무협의회를 개최',
    # 상품·지역 금융 홍보
    '지방 정착 금융', '지방정착 금융', '주택대출 지원',
    # 지역 농산물 수출 성과 홍보
    '농산물 수출',
    # 내부 예금 거래성 (계열사 간 거래 홍보)
    '예금 거래',
    # 내부 조직 협력 홍보 (농협사료 등 계열사 내부 운영)
    '계통공장',
    # 사회공헌성 투자 (ESG·장애인 사업장 등)
    '장애인 표준사업장', '장애예술인',
    # 지자체 협업 상품 홍보 (신혼 적금 등)
    '신혼 적금', '원금 추가 지원',
    # 대출 절차 간소화·디지털 전환 홍보
    '대출 절차',
    # 고객 감사·문화 행사
    '우수고객', '문화탐방', '감사 행사',
    # 비전·목표 선포식
    '비전 선포', '선포식',
    # 인터뷰 시리즈 기사
    '[AFL Interview]', '[Interview]', '(Interview)',
    # 내부 위원회 개최 (계열사 리스크관리위원회 등)
    '관리위원회 개최', '관리위원회를 개최',
    # 뱅크 NOW 상품 비교·홍보 시리즈
    '[뱅크 NOW]', '[뱅크NOW]',
    # 홍보성 총력 대응 클리셰
    '총력 대응', '관리 총력', '대응 총력',
    # 지역 농협 기원·안전 행사
    '기원제', '무사고 안전 기원',
    # 내부 경영 결의·선언 행사
    '윤리경영', '성과달성 결의', '결의대회',
    # 지역 친목·종친·봉사 단체 임원 취임 (농협 임직원의 외부 단체 활동)
    '화수회', '종친회', '향우회', '동문회', '동창회',
    '로타리클럽', '라이온스클럽', '라이온스',
    '신임회장 취임', '신임 회장 취임',
    '씨 문중', '씨 종친',
]

# 블랙리스트 (기존 유지, 보완용)
# ⚠️ 계열사 명칭 포함 단어 절대 추가 금지
PROMO_KEYWORDS = [
    # 신상품·서비스 출시
    '출시', '론칭', '새로 선보', '새롭게 선보', '신상품', '신규 출시',
    '신설', '출시·신설', '특약 신설', '신규 도입',
    # 지수연동·구조화 상품 홍보 (ELD, ELS 등)
    '지수연동예금', 'ELD', 'ELS', '원금 지키며', '원금보장',
    # 마케팅·이벤트
    '이벤트', '캠페인', '프로모션', '할인', '경품',
    # 협약·제휴
    'MOU', '업무협약', '협약 체결',
    # 수상·인증·순위 홍보
    '수상', '최우수상', '우수상', '시상식', '인증 획득', 'AWARDS', '어워즈',
    '조합장상', '○○상', '선정', '수상 기업', '수상 지점',
    '연도대상', '○○대상', '관왕', '최상위권', '전국 1위', '전국 2위', '전국 3위',
    '그룹 1위', '그룹 2위', '그룹 3위', '지역 1위', '지역 2위',
    'CEO상', 'CEO대상', '베스트CEO', '베스트-CEO',
    # 홍보성 클리셰 표현
    '눈길', '화제', '주목',
    # 인물 탐방·칼럼 시리즈
    '파수꾼', '현장의 파수꾼', '사람들', '인물탐방', '인물 탐방',
    # 사회공헌·봉사
    '기부', '봉사', '사회공헌', 'CSR', '나눔', '후원', '일손돕기', '일손 돕기',
    # 표창·감사
    '감사장', '감사패', '표창장', '표창패',
    # 지역 임원 선출 홍보
    '재선출', '운영협의회장',
    # 채용
    '채용 설명회', '인턴 모집', '공채',
    # 소비촉진
    '소비촉진', '소비 촉진', '하나로마트',
    # 정부 바우처·지원 사업
    '바우처',
    # ESG·사회공헌 홍보
    'ESG', '지분투자', '지분 투자',
    # 마케팅 이벤트성 금리 (야구 승부예측 우대금리 등)
    '우대금리', '승부예측', '승부 예측', '쏠쏠',
    # 홍보성 선제 대응 클리셰
    '선제 대응',
    # 사업 확대 홍보 클리셰
    '확대 박차', '박차를 가',
    # 보호 앞장 홍보 클리셰
    '보호 앞장', '앞장서',
    # 성과달성 붙여쓰기 변형
    '성과달성',
    # 행사 완료 홍보 (성료)
    '성료',
    # 인물 탐방·성공 비결 홍보
    '성공 비결', '성공비결',
    # 스폰서십·지역경제 협력 홍보
    '손잡고', '지역경제 활성화', '경제 활성화 나선',
    # 판로·수출 전략 홍보
    '판로 다변화', '다변화 모색',
    # 상품 홍보성 반응 기사
    '고객관심', '고객 관심', '고객반응', '관심 급증', '관심 쑥',
    # 영농·지역
    '영농지도', '영농지원',
    # 광고
    '모델 발탁',
    # 연속 수상·순위 홍보
    '연속 1위', '연속 최우수', '연속 우수',
    # 브랜드·제품 돌파구 홍보
    '정면돌파',
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
SEEN_TITLES_FILE = os.path.join(BASE_DIR, 'seen_titles.json')


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    seen_list = list(seen)[-2000:]
    with open(SEEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(seen_list, f, ensure_ascii=False)


def load_seen_titles() -> list:
    if os.path.exists(SEEN_TITLES_FILE):
        with open(SEEN_TITLES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_seen_titles(titles: list):
    with open(SEEN_TITLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(titles[-100:], f, ensure_ascii=False)


def get_article_id(url, title):
    # 쿼리파라미터 제거 후 해시 — ?input=xxx 등 트래킹 파라미터가 달라도 동일 기사 처리
    normalized_url = url.split('?')[0]
    return hashlib.md5(f"{normalized_url}{title}".encode('utf-8')).hexdigest()


def _extract_key_words(title: str) -> set:
    """특수문자 제거 후 2글자 이상 단어 추출 — 한글 2글자 단어도 포함"""
    normalized = re.sub(r'[^\w\s]', ' ', title)
    return {w for w in normalized.split() if len(w) >= 2}


def _normalize_title(title: str) -> str:
    """언론사명 제거 — ' - 언론사' 패턴 삭제"""
    return re.sub(r'\s*-\s*[^-]+$', '', title).strip()


def _is_similar_title(title: str, recent_titles: list, min_matches: int = 3) -> bool:
    """핵심 키워드 3개 이상 겹치면 유사 기사로 판단.
    부분 문자열도 매칭 — 예: '농협은행장' ↔ 'NH농협은행장'
    언론사명(' - 언론사') 제거 후 비교.
    완전히 같은 제목은 즉시 중복 처리.
    """
    title_clean = _normalize_title(title)
    # 완전 일치 제목 즉시 차단
    for prev in recent_titles:
        if _normalize_title(prev) == title_clean:
            logger.debug(f"완전 일치 중복 차단: '{title[:30]}'")
            return True
    words_new = _extract_key_words(title_clean)
    if len(words_new) < 2:
        return False
    for prev in recent_titles:
        prev_clean = _normalize_title(prev)
        words_prev = _extract_key_words(prev_clean)
        matched_prev = set()
        count = 0
        for wn in words_new:
            for wp in words_prev:
                if wp not in matched_prev and (wn == wp or wn in wp or wp in wn):
                    count += 1
                    matched_prev.add(wp)
                    break
        if count >= min_matches:
            logger.debug(f"유사 기사 차단: '{title[:30]}' ↔ '{prev[:30]}'")
            return True
    return False


def is_relevant(title, summary=''):
    text = title + ' ' + summary
    # 1단계: 제목에 농협 관련 키워드가 있어야 통과
    # 예외: 지자체 '금고' 기사는 제목에 농협이 없어도 요약에 농협이 있으면 통과
    # (예: "전남·광주 통합금고, 수의계약으로 운영" — 본문에 NH농협은행 등장)
    if not any(kw in title for kw in KEYWORDS):
        if not ('금고' in title and any(kw in summary for kw in KEYWORDS)):
            return False
    # 2단계: 구조적 홍보성 패턴 즉시 차단 — 화이트리스트보다 우선 적용
    # (예: '조합장' 화이트리스트라도 '수상' 블랙리스트가 있으면 차단)
    if any(pattern in title for pattern in STRUCTURAL_PROMO_PATTERNS):
        return False
    # 3단계: 블랙리스트 차단 — 화이트리스트보다 우선 적용
    if any(kw in title for kw in PROMO_KEYWORDS):
        return False
    # 4단계: [단독] / [속보] 취재 기사 즉시 통과
    # (홍보성 필터를 통과한 단독·속보 기사는 영양가 있는 취재 기사로 판단)
    SCOOP_TAGS = ['[단독]', '[속보]', '[단독보도]']
    if any(tag in title for tag in SCOOP_TAGS):
        return True
    # 5단계: 화이트리스트 — 제목에 영양가 있는 키워드가 있어야 통과
    # (요약에만 금융 키워드가 언급되는 복지·홍보성 기사 차단)
    if any(kw in title for kw in WHITELIST_KEYWORDS):
        return True
    # 6단계: 화이트리스트 키워드 없으면 기본 차단
    return False


def _strip_html(text: str) -> str:
    """네이버 API 응답의 간단한 HTML 태그 제거"""
    return re.sub(r'<[^>]+>', '', text).strip()


def fetch_from_naver(seen: set, seen_titles: list, cutoff: datetime) -> list:
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

                # 비신뢰 출처는 LLM 추가 검증
                if not _is_trusted_source(url) and not _llm_filter(title, summary):
                    continue

                article_id = get_article_id(url, title)
                if article_id in seen:
                    continue
                if _is_similar_title(title, seen_titles):
                    continue

                seen.add(article_id)
                seen_titles.append(title)
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
    seen_titles = load_seen_titles()
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
                else:
                    # published_parsed 없으면 published 문자열로 직접 파싱 시도
                    published_str = entry.get('published', '')
                    if published_str:
                        try:
                            from email.utils import parsedate_to_datetime
                            pub_dt = parsedate_to_datetime(published_str).astimezone(timezone.utc)
                            if pub_dt < cutoff:
                                continue
                        except Exception:
                            pass  # 파싱 실패 시 통과 허용

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

                # 비신뢰 출처는 LLM 추가 검증
                if not _is_trusted_source(url) and not _llm_filter(title, summary):
                    continue

                article_id = get_article_id(url, title)
                if article_id in seen:
                    continue
                if _is_similar_title(title, seen_titles):
                    continue

                seen.add(article_id)
                seen_titles.append(title)
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
    naver_articles = fetch_from_naver(seen, seen_titles, cutoff)
    new_articles.extend(naver_articles)

    if new_articles:
        save_seen(seen)
        save_seen_titles(seen_titles)

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
    lines.append(f'<a href="{url}">링크</a>')

    return '\n'.join(lines)
