"""수집 규칙 회귀 점검 — 네트워크·API 없이 돈다.

실행: python test_collector.py   (news_bot 폴더에서)
사례는 전부 2026-11 실제 기사 제목이다. 규칙을 고칠 때마다 돌려서 예전 실수가 되살아나지 않는지 본다.
"""
import collector as c

# (제목, 후보여야 하는가) — 후보 = 규칙 단계를 지나 AI 판정으로 넘어감
SCREEN_CASES = [
    # 과거 금지어가 통째로 막던 실제 뉴스
    ("홍콩 ELS 과징금 1조원 넘게 줄었지만…은행권 '소송·환입' 셈법 복잡", True),
    ("은행연합회장 후보 6명 압축… 우리은행장 연임, 연말 금융권 인사 본격화", True),
    ("은행지주 '셀프 연임' 정조준… 당정 '지배구조 개선방안' 발표", True),
    ("[삼성증권 기관경고①] 금감원의 수상한 '반쪽 공개'...논란 확산", True),
    ("KB금융 3분기 순이익 12% 하락", True),
    ("4대 금융지주, 비상경영 들어가나", True),            # '어가'가 '들어가'에 걸리던 문제
    ("이억원 금융위원장, 금융지주 회장단 간담회", True),
    ("워크아웃 건설사 자율협약 연장…은행권 부담", True),
    # 규칙으로 버려야 하는 것
    ("박수치는 강호동 농협 회장 - 뉴시스", False),         # 사진 설명
    ("연세대 학생들 만난 강호동 농협중앙회장 - 뉴스핌", False),
    ("NH농협금융, 김장 나눔 14년째…지역사회 상생 실천", False),
    ("삼남농협, 상호금융 금융자산 6천억원 달성탑 수상", False),
    ("신학기 맞아 학용품 물가 들썩", False),               # 신학기 ≠ 수협은행장
    ("강호동, 새 예능으로 복귀", False),                   # 방송인
    ("영덕 강구농협, 쌀작목반 회의 개최…농가 판로·소득 증대 방안 논의", False),
    # 사진 설명처럼 생겼어도 사건이면 남긴다
    ("사퇴 압박 받는 강호동 농협중앙회장", True),
]

# AI를 쓸 수 없을 때의 엄격 기준 — 허용어가 다른 낱말 속에 들어 있어 잘못 통과하던 것
STRICT_CASES = [
    ("농협중앙회, 채용 신체검사 절차 개선…지원자 부담 줄인다", False),   # '검사'⊂'신체검사'
    ("NH농협은행, 부행장 참석 전주상공회의소 상생행사", False),          # '행장'⊂'부행장'
    ("강호동 농협중앙회장, 연세대서 협동조합 특강", False),              # 실명만으로는 통과 안 함
    ("[단독] 농협금융, 순익 기여도 오류…증권은 높게, 은행은 낮게", True),
    ("수협은행장 선임 또 불발…내달 재논의", True),
]

T0 = 1_000_000.0
HOUR = 3600


def test_screen():
    for title, expected in SCREEN_CASES:
        assert c.screen(title)[0] is expected, f"후보 판정 {not expected} (기대 {expected}): {title}"


def test_strict():
    for title, expected in STRICT_CASES:
        assert c.is_relevant(title) is expected, f"엄격 기준 {not expected} (기대 {expected}): {title}"


def test_dedup():
    scoop = "[단독] 정진완 우리은행장 1년 연임…자추위 다수 찬성 - MTN 머니투데이방송"
    previews = [["우리은행장 13일 최종 후보 결정…정진완 '1년 연임' 유력 - 뉴스1", T0],
                ["우리금융, 오늘 자추위 논의…정진완 우리은행장 연임 촉각 - 뉴시스", T0 + 40 * HOUR]]
    now = T0 + 47 * HOUR
    # 전망 기사가 쌓여 있어도 단독과 첫 결과 기사는 나가야 한다
    assert not c._is_similar_title(scoop, previews, now)
    assert not c._is_similar_title("정진완 우리은행장 연임 확정...임기 1년 연장 - 메트로신문", previews, now)
    # 단독이 나간 뒤 받아쓴 기사는 중복
    sent = previews + [[scoop, now]]
    assert c._is_similar_title("정진완 우리은행장 연임 확정...임기 1년 연장 - 메트로신문", sent, now + HOUR)
    assert c._is_similar_title("정진완 우리은행장 연임…임기 1년 연장 - 코리아리포트", sent, now + 30 * HOUR)
    # 같은 사안의 다른 단독은 중복이 아니다
    assert not c._is_similar_title("[단독]우리금융지주 직원, 법인카드로 4억대 횡령…경찰 고소 - 뉴스1", sent, now + 2 * HOUR)
    # 같은 제목은 언제든 중복
    assert c._is_similar_title(scoop, sent, now + 500 * HOUR)
    # 사흘이 지난 사안은 다시 일어나면 새 기사다
    assert not c._is_similar_title("수협은행장 선임 5번째도 불발…인선 장기화",
                                   [["수협은행장 선임 4번째 불발…내달 재논의", T0]], T0 + 100 * HOUR)


def test_breaking_dedup():
    """같은 발표의 [속보] 여러 건은 하나만 나가야 한다 (2026-11-22 증안펀드 사례)."""
    titles = ["[속보] 정부, 국내외 증시 급락에 '증시안정펀드' 가동 준비",
              "[속보]금감원·거래소, 3개월간 불법 공매도 특별감시기간 운영",
              "[속보] 검은 월요일 공포에… 정부 \"증안펀드 가동 준비작업 착수\"",
              "[속보] 정부 '증안펀드' 즉시 가동 준비·공매도 3개월 특별감시·자사주…",
              "[속보] 정부, 증권시장안정펀드 가동 준비 착수…상장기업 자기주식 매…"]
    sent = []
    for t in titles:
        if not c._is_similar_title(t, sent, T0):
            sent.append([t, T0])
    # 규칙만으로는 줄임말(증안펀드=증권시장안정펀드)을 몰라 최대 2건까지 남을 수 있다. AI 판정이 나머지를 거른다.
    assert len(sent) <= 2, [s[0] for s in sent]


def test_stale_breaking():
    """몇 시간 지나 들어온 [속보]는 후보에 넣지 않는다."""
    import time
    from email.utils import formatdate
    cands = []
    old = formatdate(time.time() - 5 * HOUR)
    fresh = formatdate(time.time() - 10 * 60)
    c._consider(cands, set(), [], "[속보] 금융위, 가계대출 규제 강화", 'https://example.com/a', '', '', old)
    c._consider(cands, set(), [], "[속보] 금융위, 가계대출 규제 강화 발표", 'https://example.com/b', '', '', fresh)
    assert [x['url'] for x in cands] == ['https://example.com/b']


def test_feeds():
    urls = c._feed_urls()
    google = [u for u in urls if 'news.google.com/rss/search' in u]
    assert google and all('when:1d' in u or 'when:1h' in u for u in google), "시간 조건 없는 구글 검색 피드가 있다"


def test_select_without_ai():
    """AI를 못 쓰면 엄격 기준만 통과시키고, 상태를 고장으로 남긴다 (조용히 전부 통과시키지 않는다)."""
    c.ANTHROPIC_API_KEY = ''
    c._llm_cache.clear()
    cands = []
    seen, titles = set(), []
    for t in ("[단독] 농협금융, 순익 기여도 오류…증권은 높게, 은행은 낮게", "강호동 농협중앙회장, 연세대서 협동조합 특강"):
        c._consider(cands, seen, titles, t, 'https://example.com/' + str(len(cands)), '', '', '')
    assert len(cands) == 2
    picked = c._select(cands, seen, titles)
    assert [p['title'] for p in picked] == ["[단독] 농협금융, 순익 기여도 오류…증권은 높게, 은행은 낮게"]
    assert c.LLM_STATE['ok'] is False
    assert c._llm_dedup([{'title': 'x'}], []) == [None]   # AI 없으면 규칙으로 넘긴다


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok ', name)
