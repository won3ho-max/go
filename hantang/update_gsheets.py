"""
한탕 스터디 Google Sheets 자동 업데이트 스크립트
─────────────────────────────────────────────────
GitHub Actions에서 로컬 컴퓨터 없이 실행 가능

[수행 작업]
  1. 활성 종목 현재가(M열) 갱신 (Yahoo Finance)
  2. 추천일 +1달 도래 종목 자동 매도 (J→P열)
  3. 기준가 미설정 종목 당일 종가로 채움
  4. 추천 대기 종목 추가 (telegram_listener가 저장한 pending_stocks)
  5. portfolio.json 내보내기 (카드뉴스 생성용)

환경변수:
  GSHEETS_CREDENTIALS  - 서비스 계정 JSON 문자열 (GitHub Secret)
  GSHEETS_ID           - 스프레드시트 ID
"""

import os, sys, re, json, time, datetime, subprocess, math
from concurrent.futures import ThreadPoolExecutor, as_completed

def _ensure(pkg, import_name=None):
    try:
        __import__(import_name or pkg.replace("-", "_"))
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)

_ensure("gspread")
_ensure("google-auth", "google.oauth2")
_ensure("yfinance")
_ensure("exchange_calendars")
_ensure("python-dateutil", "dateutil")

import gspread
from google.oauth2.service_account import Credentials
import requests
import yfinance as yf
import pandas as pd
import exchange_calendars as xcals
from dateutil.relativedelta import relativedelta
from pathlib import Path

# ── 설정 ────────────────────────────────────────────────────────────────
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
SCOPES   = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

KOREAN_CODES = {
    "삼성전자":    "005930", "삼성SDI":    "006400",
    "에코프로비엠": "247540", "티엘비":     "356860",
    "엘앤에프":   "066970", "HD건설기계": "267270",
    "이노스페이스": "462350", "에스에이엠티": "031330",
    "한화비전":   "489790", "동성화인텍":  "033500",
    "세아제강지주": "003030", "SK텔레콤": "017670",
    "키움증권": "039490", "효성티앤씨": "298020",  # 298050은 HS효성첨단소재(2026-08 오매핑 발견)
    "삼성전기": "009150", "케이엔솔": "053080",
    "포스코홀딩스": "005490", "POSCO홀딩스": "005490",
    "코스모로보틱스": "439960",
    "네이버": "035420", "NAVER": "035420",
    "한국조선해양": "009540", "HD한국조선해양": "009540",
}
KOSDAQ_CODES = {"247540","356860","462350","031330","439960"}  # 066970(엘앤에프)는 야후서 .KS로 등록

# ── 인증 ────────────────────────────────────────────────────────────────
def get_client() -> gspread.Client:
    creds_json = os.environ.get("GSHEETS_CREDENTIALS", "")
    if creds_json:
        info = json.loads(creds_json)
    else:
        # 로컬 테스트용: credentials.json 파일
        creds_file = BASE_DIR / "credentials.json"
        if not creds_file.exists():
            raise FileNotFoundError("GSHEETS_CREDENTIALS 환경변수 또는 credentials.json 필요")
        info = json.loads(creds_file.read_text())

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

def get_spreadsheet() -> gspread.Spreadsheet:
    sheet_id = os.environ.get("GSHEETS_ID", "")
    if not sheet_id:
        id_file = BASE_DIR / "gsheets_id.txt"
        if id_file.exists():
            sheet_id = id_file.read_text().strip()
    if not sheet_id:
        raise ValueError("GSHEETS_ID 환경변수 또는 gsheets_id.txt 필요")
    # open_by_key는 곧바로 시트 메타데이터를 받아오는 원격 호출이다. 이 한 줄이
    # 재시도 밖에 있던 탓에 2026-09-04 데일리가 503 한 번에 통째로 죽어 카드가
    # 안 나갔다. 뒤의 모든 작업이 여기에 매달려 있으므로 여기부터 감싼다.
    return sheet_retry(lambda: get_client().open_by_key(sheet_id), "스프레드시트 열기")

# ── 구글시트 호출 재시도 ─────────────────────────────────────────────────
def sheet_retry(fn, what="시트 작업", tries=5):
    """구글 시트 API의 일시 장애(5xx)와 분당 쿼터 초과(429)를 재시도한다.

    2026-08-21 데일리는 429('Read requests per minute'), 08-24는 503으로 통째로
    죽어 카드가 이틀 안 나갔다. 429는 분당 창이 열려야 하므로 5xx보다 길게 쉰다."""
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            msg = str(e)
            quota = "[429]" in msg or "Quota exceeded" in msg
            transient = quota or any(c in msg for c in
                                     ("[500]", "[502]", "[503]", "[504]",
                                      "Read timed out", "Connection aborted"))
            if not transient or i == tries:
                raise
            delay = (20, 40, 60, 60)[min(i - 1, 3)] if quota else (3, 6, 12, 20)[min(i - 1, 3)]
            print(f"    [재시도 {i}/{tries - 1}] {what}: {msg[:110]} → {delay}s 대기")
            time.sleep(delay)


# ── 주가 조회 (Yahoo Finance) ────────────────────────────────────────────
_yf_cache: dict = {}

def fetch_price(market: str, code: str, date: datetime.date | None = None) -> float | None:
    cache_key = (market, code, str(date))
    if cache_key in _yf_cache:
        return _yf_cache[cache_key]

    try:
        if market == "KR":
            suffix     = ".KQ" if code in KOSDAQ_CODES else ".KS"
            ticker_str = code + suffix
        else:
            ticker_str = code

        t = yf.Ticker(ticker_str)
        if date:
            hist = t.history(start=str(date),
                             end=str(date + datetime.timedelta(days=4)),
                             prepost=False)
            if not hist.empty:
                price = float(hist["Close"].iloc[0])
            else:
                return None
        else:
            hist = t.history(period="2d", prepost=False)
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
            else:
                return None

        # NaN/Inf 방어
        if math.isnan(price) or math.isinf(price):
            return None

        result = int(price) if market == "KR" else round(price, 2)
        _yf_cache[cache_key] = result
        return result
    except Exception as e:
        print(f"    [가격 조회 실패] {code}: {e}")
        return None


_kr_close_cache: dict = {}

def _naver_kr_closes(code: str) -> dict:
    """국내 종목 일별 종가 {date: close}. 네이버에서 가져온다.

    야후는 국내 일봉을 늦게 올린다. 2026-08-14 07:00 데일리에서 국내 18종목
    전건이 8/13이 아닌 8/12 종가로 들어갔고, 같은 시각 네이버에는 8/13이
    정상 존재했다. 시장지표에서 겪은 결측과 같은 문제라 소스를 바꾼다.
    pageSize 최대 60 → 2페이지(약 120거래일)면 직전분기까지 커버된다."""
    if code in _kr_close_cache:
        return _kr_close_cache[code]
    out = {}
    try:
        for page in (1, 2):
            rows = requests.get(
                f"https://m.stock.naver.com/api/stock/{code}/price",
                params={"pageSize": 60, "page": page},
                headers={"User-Agent": "Mozilla/5.0",
                         "Referer": "https://m.stock.naver.com/"},
                timeout=8).json()
            if not isinstance(rows, list) or not rows:
                break
            for r in rows:
                d = str(r.get("localTradedAt", ""))[:10]
                try:
                    out[datetime.date.fromisoformat(d)] = \
                        float(str(r.get("closePrice", "")).replace(",", ""))
                except Exception:
                    pass
    except Exception as e:
        print(f"    [네이버 시세 실패] {code}: {e}")
    _kr_close_cache[code] = out
    return out


def close_at(market: str, code: str, date: datetime.date):
    """지정일 종가(휴장이면 직전 거래일). 아직 장이 안 끝난 날짜면 None.

    기준가 산정용. 기존 fetch_price(date)는 start=date의 iloc[0]이라
    그날 시세가 없으면 '다음 거래일' 종가를 집었다(2026-06-03 라이콤 건)."""
    if date > last_completed_session(market):
        return None
    return close_on_or_before(market, code, date)


_last_close_cache: dict = {}

def last_completed_session(market: str) -> datetime.date:
    """'마지막으로 장이 끝난' 날짜(달력 미적용 기준일). KST 실행 시각으로 판단.
      KR: 15:30 마감 → 16시 이후면 오늘, 아니면 어제
      US: 16:00 ET 마감(=익일 05~06시 KST) → 06시 이후면 어제, 아니면 그제
    데일리는 07:00 KST에 도는데, 이때 KR은 어제·US도 어제 세션이 마지막 완료분이다."""
    from datetime import timezone, timedelta
    now = datetime.datetime.now(timezone(timedelta(hours=9)))
    if market == "KR":
        base = now.date() if now.hour >= 16 else now.date() - datetime.timedelta(days=1)
    else:
        base = now.date() - datetime.timedelta(days=1 if now.hour >= 6 else 2)
    return prev_trading_day(base, market)


def latest_close(market: str, code: str):
    """현재가로 쓸 '마지막 완료 세션 종가'.

    기존 fetch_price(date=None)은 yfinance period='2d' + iloc[-1]이라
      · 신규 봉이 아직 안 올라오면 이틀 전 종가를 집고(2026-08-04 국내 종목 하루 밀림)
      · 장중에 돌리면 종가가 아니라 체결가를 집는다.
    거래소 달력으로 기준일을 정하고 '그날 이하 마지막 종가'를 쓴다(미래 데이터 불가)."""
    key = (market, code)
    if key in _last_close_cache:
        return _last_close_cache[key]
    target = last_completed_session(market)
    price, on = close_on_or_before(market, code, target, with_date=True)
    if price is not None and on and on != target:
        print(f"    [주의] {code} 현재가가 {target}가 아닌 {on} 종가 (야후 미게시 추정)")
    _last_close_cache[key] = price
    return price


def prefetch_last_closes(pairs):
    """현재가(마지막 완료 세션 종가)를 병렬로 미리 채운다."""
    uniq = {(m, c) for m, c in pairs if (m, c) not in _last_close_cache}
    if not uniq:
        return
    print(f"  병렬 종가 조회: {len(uniq)}건...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(as_completed([pool.submit(latest_close, m, c) for m, c in uniq]))


def prefetch_prices(jobs: list[tuple[str, str, datetime.date | None]]):
    """병렬로 주가를 미리 조회해서 캐시에 채운다."""
    unique = {(m, c, str(d)): (m, c, d) for m, c, d in jobs
              if (m, c, str(d)) not in _yf_cache}
    if not unique:
        return
    print(f"  병렬 주가 조회: {len(unique)}건...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch_price, m, c, d): (m, c, d)
                for (m, c, d) in unique.values()}
        for f in as_completed(futs):
            pass  # fetch_price가 알아서 캐시에 저장

# ── 종목명 파싱 ──────────────────────────────────────────────────────────
_naver_cache: dict = {}   # {검색어: (official_name, code, market)}

def _search_naver_stock(name: str):
    """네이버 금융 검색 API로 종목명 → 종목코드/시장 자동 조회"""
    if name in _naver_cache:
        cached = _naver_cache[name]
        return cached[2], cached[1]   # market, code

    # KOREAN_CODES에 등록된 종목은 즉시 반환 (정식명칭도 조회 시도)
    if name in KOREAN_CODES:
        code = KOREAN_CODES[name]
        official = name
        try:
            url = f"https://m.stock.naver.com/api/stock/{code}/basic"
            r = requests.get(url, timeout=3, headers={"User-Agent": "Mozilla/5.0"})
            sn = r.json().get("stockName", "")
            if sn:
                official = sn
        except Exception:
            pass
        _naver_cache[name] = (official, code, "KR")
        print(f"    [사전매칭] {name} → {official}({code}) (KR)")
        return "KR", code

    try:
        url = "https://ac.stock.naver.com/ac"
        resp = requests.get(url, params={"q": name, "target": "stock"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        items = resp.json().get("items", [])

        # 매칭되는 결과를 한국/해외로 분리 (한국 우선)
        # 2패스: 완전일치·티커일치를 먼저 보고, 없을 때만 부분포함을 본다.
        #   네이버는 'KODEX WTI원유선물'에 인버스(271050)를 정방향(261220)보다 앞에 준다.
        #   1패스 + break 구조라 normalize_name이 정방향을 인버스로 바꿔버렸다.
        kr_match = None
        us_match = None
        name_nsp = name.replace(" ", "")

        for exact_only in (True, False):
            for item in items:
                item_name = item.get("name", "")
                item_code = item.get("code", "")
                item_nsp = item_name.replace(" ", "")
                if not item_code:
                    continue
                exact = (item_name == name or name_nsp == item_nsp
                         or name.upper() == item_code.upper())
                if not exact:
                    if exact_only:
                        continue
                    if name_nsp not in item_nsp:
                        continue
                    # 후보가 종목명의 절반도 설명 못하면 우연한 부분일치로 본다
                    if len(name_nsp) * 2 < len(item_nsp):
                        continue
                type_code = item.get("typeCode", "")
                official_name = item.get("name", name)

                if type_code in ("KOSPI", "KOSDAQ"):
                    # 부분일치일 땐 군더더기가 가장 적은 이름을 고른다
                    if kr_match is None or len(item_nsp) < len(kr_match[0].replace(" ", "")):
                        kr_match = (official_name, item_code, type_code)
                elif us_match is None or len(item_nsp) < len(us_match[0].replace(" ", "")):
                    us_match = (official_name, item_code, type_code)
            if kr_match or us_match:
                break

        # 이름 매칭 실패했지만 검색 결과가 1건뿐이면 해당 종목으로 간주
        if not kr_match and not us_match and len(items) == 1:
            item = items[0]
            item_code = item.get("code", "")
            type_code = item.get("typeCode", "")
            official_name = item.get("name", name)
            if item_code:
                if type_code in ("KOSPI", "KOSDAQ"):
                    kr_match = (official_name, item_code, type_code)
                else:
                    us_match = (official_name, item_code, type_code)

        # 한국 종목 우선
        if kr_match:
            official_name, code, type_code = kr_match
            KOREAN_CODES[name] = code
            if type_code == "KOSDAQ":
                KOSDAQ_CODES.add(code)
            _naver_cache[name] = (official_name, code, "KR")
            print(f"    [자동매칭] {name} → {official_name}({code}) ({type_code})")
            return "KR", code
        elif us_match:
            official_name, code, type_code = us_match
            _naver_cache[name] = (official_name, code, "US")
            print(f"    [자동매칭] {name} → {official_name}({code}) (US/{type_code})")
            return "US", code
    except Exception as e:
        print(f"    [네이버 검색 실패] {name}: {e}")
    return None, None


def normalize_name(name: str) -> str:
    """종목명을 네이버 금융 정식 명칭으로 정규화"""
    clean = name.strip()
    # 이미 티커가 괄호로 붙어있으면 괄호 앞 이름만 추출해서 검색
    m = re.search(r"^(.+?)\s*\(([A-Z0-9]{1,7})\)\s*$", clean)
    search_name = m.group(1).strip() if m else clean

    # 네이버 검색
    if search_name not in _naver_cache:
        _search_naver_stock(search_name)

    if search_name in _naver_cache:
        official, code, market = _naver_cache[search_name]
        if market == "US":
            return f"{official}({code})"
        return official

    return clean  # 검색 실패 시 원본 유지


# 국내 ETF의 환헤지 표기. 티커가 아니다.
_HEDGE_SUFFIX = re.compile(r"\(\s*(?:H|UH|합성|합성\s*H|환헤지|언헤지)\s*\)\s*$", re.I)


def is_adjustment(name: str) -> bool:
    """실현 섹션의 '점수 조정' 행 판별. 종목이 아니므로 시세 조회·추천 인정 대상이 아니다.
    미추천 패널티(-10%)와 보너스(예: 결혼 보너스 +10%)가 여기 해당한다."""
    t = str(name or "")
    return ("미추천" in t) or ("패널티" in t) or ("보너스" in t)


def parse_stock(name: str):
    name = str(name).strip()
    # 'KODEX WTI원유선물(H)'가 US 티커 'H'(하얏트)로 잡혀 178달러가 현재가로
    # 들어갔다(2026-08-19). 1글자 티커는 오탐이 커서 US 인식에서 뺀다.
    if not _HEDGE_SUFFIX.search(name):
        m = re.search(r"\(([A-Z]{2,5})\)\s*$", name)
        if m: return "US", m.group(1)
    m = re.search(r"\(([A-Z0-9]{5,7})\)\s*$", name)
    if m: return "KR", m.group(1)
    if name in KOREAN_CODES: return "KR", KOREAN_CODES[name]
    # KOREAN_CODES에 없으면 네이버 검색으로 자동 매칭
    return _search_naver_stock(name)

# ── 영업일 계산 ──────────────────────────────────────────────────────────
_cals: dict = {}
def _cal(market):
    key = "XKRX" if market == "KR" else "XNYS"
    if key not in _cals:
        _cals[key] = xcals.get_calendar(key)
    return _cals[key]

def prev_trading_day(target: datetime.date, market: str) -> datetime.date:
    cal  = _cal(market)
    ts   = pd.Timestamp(target)
    sess = cal.sessions_in_range(ts - pd.Timedelta(days=14), ts)
    return sess[-1].date() if len(sess) > 0 else target

_round_cache: dict = {}

def round_day(d: datetime.date) -> datetime.date:
    """그 주(월~일)의 첫 국내 거래일. 주간 라운드 기준일.
    월요일이 휴장이면 화요일이 그 주 라운드가 된다."""
    monday = d - datetime.timedelta(days=d.weekday())
    if monday in _round_cache:
        return _round_cache[monday]
    try:
        sess = _cal("KR").sessions_in_range(pd.Timestamp(monday),
                                            pd.Timestamp(monday + datetime.timedelta(days=6)))
        out = sess[0].date() if len(sess) > 0 else monday
    except Exception:
        out = monday
    _round_cache[monday] = out
    return out


def calc_sell_date(rec_date: datetime.date, market: str) -> datetime.date:
    # 추천일+1달 미만의 마지막 거래일 (1달 되는 날 제외)
    one_month_later = rec_date + relativedelta(months=1)
    day_before = one_month_later - datetime.timedelta(days=1)
    return prev_trading_day(day_before, market)

# ── 블록 파싱 ────────────────────────────────────────────────────────────
def find_person_blocks(all_values: list) -> list:
    """
    all_values = ws.get_all_values() 결과 (0-indexed 리스트)
    J열 = index 9, P열 = index 15, I열 = index 8
    반환: [{'person': str, 'row_start': int, 'row_end': int}, ...]  (1-indexed)
    """
    header_rows, sogyae_rows = [], []
    for i, row in enumerate(all_values):
        j_val = row[9] if len(row) > 9 else ""
        p_val = row[15] if len(row) > 15 else ""
        if j_val == "종목명":
            header_rows.append(i + 1)   # 1-indexed
        if "실현수익률 소계" in str(p_val):
            sogyae_rows.append(i + 1)

    blocks = []
    for h_row in header_rows:
        s_row = next((r for r in sogyae_rows if r > h_row), None)
        if s_row is None:
            continue
        person_row = all_values[h_row]   # h_row는 1-indexed → 0-indexed = h_row
        i_val = person_row[8] if len(person_row) > 8 else ""
        person = str(i_val).strip().replace("\n", "") if i_val else ""
        blocks.append({
            "person":    person,
            "row_start": h_row + 1,
            "row_end":   s_row - 1,
        })
    return blocks

# ── 시트 처리 ────────────────────────────────────────────────────────────
def process_sheet(ws: gspread.Worksheet, today: datetime.date):
    print(f"  데이터 로드 중...")
    all_values = sheet_retry(ws.get_all_values, "시트 읽기")

    blocks   = find_person_blocks(all_values)
    updates  = []   # (row, col, value) 배치 업데이트용
    updated, sold, skipped = [], [], []

    # ── 병렬 주가 조회를 위한 사전 스캔 ────────────────────────────────
    price_jobs, close_jobs = [], []
    for block in blocks:
        for row_1 in range(block["row_start"], block["row_end"] + 1):
            idx = row_1 - 1
            if idx >= len(all_values):
                continue
            row_data = all_values[idx]
            name = row_data[9] if len(row_data) > 9 else ""
            rec_date_s = row_data[10] if len(row_data) > 10 else ""
            if not name or not isinstance(name, str) or not name.strip():
                continue
            market, code = parse_stock(name.strip())
            if not market:
                continue
            close_jobs.append((market, code))
            try:
                rec_date = datetime.date.fromisoformat(rec_date_s[:10])
                sell_date = calc_sell_date(rec_date, market)
                if sell_date <= today:
                    price_jobs.append((market, code, sell_date))
            except Exception:
                pass
    prefetch_prices(price_jobs)
    prefetch_last_closes(close_jobs)

    for block in blocks:
        person    = block["person"]
        row_start = block["row_start"]
        row_end   = block["row_end"]

        for row_1 in range(row_start, row_end + 1):
            idx   = row_1 - 1   # 0-indexed
            if idx >= len(all_values):
                continue
            row_data = all_values[idx]

            def cell(col_1):
                c = col_1 - 1
                return row_data[c] if c < len(row_data) else ""

            name       = cell(10)   # J
            rec_date_s = cell(11)   # K
            base_price = cell(12)   # L
            cur_price  = cell(13)   # M

            if not name or not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()

            market, code = parse_stock(name)
            if not market:
                skipped.append(f"{person}/{name}")
                continue

            # 종목명 정규화 (네이버 정식 명칭으로 보정)
            official = normalize_name(name)
            if official != name:
                updates.append((row_1, 10, official))  # J열 정식 명칭으로 갱신
                print(f"    [정규화] {name} → {official}")
                name = official

            # 추천일 파싱
            try:
                rec_date = datetime.date.fromisoformat(rec_date_s[:10])
            except Exception:
                skipped.append(f"{person}/{name} (추천일 오류)")
                continue

            # 매 실행마다 기준가=추천일 종가 검증, 현재가=실행 시점 가격
            correct_base = close_at(market, code, rec_date)
            cur_price_now = latest_close(market, code)

            if correct_base:
                updates.append((row_1, 12, correct_base))  # L: 추천일 종가
                base_price = str(correct_base)
            elif not base_price:
                if cur_price_now:
                    updates.append((row_1, 12, cur_price_now))  # fallback
                    base_price = str(cur_price_now)
                else:
                    skipped.append(f"{person}/{name} (기준가 조회 실패)")
                    continue

            if cur_price_now:
                updates.append((row_1, 13, cur_price_now))  # M: 현재가
            updates.append((row_1, 14, f"=(M{row_1}-L{row_1})/L{row_1}"))  # N: 수익률

            try:
                base_f = float(str(base_price).replace(",", ""))
            except Exception:
                skipped.append(f"{person}/{name} (기준가 형식 오류)")
                continue

            sell_date = calc_sell_date(rec_date, market)

            # ── 자동 매도 (sell_date < today: 종가 확정된 날만) ────────
            if sell_date < today:
                # P열 빈 행 탐색
                p_row = None
                for r in range(row_start, row_end + 1):
                    ri = r - 1
                    if ri >= len(all_values): break
                    p_val = all_values[ri][15] if len(all_values[ri]) > 15 else ""
                    if not p_val:
                        p_row = r
                        break

                if p_row is None:
                    print(f"    [오류] {person} P열 빈 행 없음")
                    continue

                sell_price = close_at(market, code, sell_date)
                if sell_price is None:
                    sell_price = fetch_price(market, code)
                if sell_price is None:
                    print(f"    [오류] {person}/{name} 매도가 조회 실패")
                    continue

                updates += [
                    (p_row, 16, name),                                 # P
                    (p_row, 17, str(rec_date)),                        # Q
                    (p_row, 18, str(sell_date)),                       # R
                    (p_row, 19, base_f),                               # S
                    (p_row, 20, sell_price),                           # T
                    (p_row, 21, f"=(T{p_row}-S{p_row})/S{p_row}"),    # U
                ]
                # J-N 초기화
                for col in range(10, 15):
                    updates.append((row_1, col, ""))

                ret = (sell_price - base_f) / base_f * 100
                sold.append(f"{person}/{name}: {sell_date} 매도 ({ret:+.1f}%)")
                print(f"    [매도] {person}/{name}: {sell_date}, {sell_price:,} ({ret:+.1f}%)")

            # ── 현재가 로그 ───────────────────────────────────────
            else:
                if cur_price_now is not None:
                    ret = (cur_price_now - base_f) / base_f * 100
                    updated.append(f"{person}/{name}: {cur_price_now:,} ({ret:+.1f}%)")
                    print(f"    ✓ {person}/{name} → {cur_price_now:,} ({ret:+.1f}%)")
                else:
                    print(f"    ✗ {person}/{name} → 조회 실패")

    # ── 배치 업데이트 ────────────────────────────────────────────────────
    if updates:
        print(f"  배치 업데이트: {len(updates)}셀")
        cell_list = []
        for row_1, col_1, val in updates:
            c = gspread.Cell(row=row_1, col=col_1, value=val)
            cell_list.append(c)
        sheet_retry(lambda: ws.update_cells(cell_list, value_input_option="USER_ENTERED"), "셀 갱신")

    return updated, sold, skipped


# ── pending_stocks 처리 ──────────────────────────────────────────────────
def process_pending(ws: gspread.Worksheet, all_values: list, today: datetime.date):
    pending_path = BASE_DIR / "pending_stocks.json"
    if not pending_path.exists():
        return []

    try:
        items = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not items:
        return []

    print(f"\n[대기 종목 처리] {len(items)}건")
    blocks  = find_person_blocks(all_values)
    updates = []
    added   = []

    for item in items:
        person_name = item.get("person", "")
        stock_name  = item.get("stock", "")
        rec_date_s  = item.get("rec_date", "")

        try:
            rec_date = datetime.date.fromisoformat(rec_date_s)
        except Exception:
            rec_date = today

        block = next((b for b in blocks
                      if b["person"] == person_name or person_name in b["person"]),
                     None)
        if not block:
            print(f"  [스킵] '{person_name}' 블록 없음")
            continue

        already = any(
            (all_values[r-1][9] if len(all_values[r-1]) > 9 else "") == stock_name
            for r in range(block["row_start"], block["row_end"] + 1)
            if r - 1 < len(all_values)
        )
        if already:
            print(f"  [중복] {person_name} / {stock_name}")
            continue

        empty_row = next(
            (r for r in range(block["row_start"], block["row_end"] + 1)
             if r - 1 < len(all_values) and
             not (all_values[r-1][9] if len(all_values[r-1]) > 9 else "")),
            None
        )
        if not empty_row:
            print(f"  [스킵] '{person_name}' 블록 꽉 참")
            continue

        updates += [
            (empty_row, 10, stock_name),     # J
            (empty_row, 11, str(rec_date)),  # K
        ]
        added.append(f"{person_name}/{stock_name}")
        print(f"  ✅ {person_name} / {stock_name} (추천일: {rec_date})")

    if updates:
        cell_list = []
        for row_1, col_1, val in updates:
            c = gspread.Cell(row=row_1, col=col_1, value=val)
            cell_list.append(c)
        sheet_retry(lambda: ws.update_cells(cell_list, value_input_option="USER_ENTERED"), "셀 갱신")
        pending_path.write_text("[]", encoding="utf-8")
        print(f"  pending_stocks.json 초기화 완료")

    return added


# ── portfolio.json 내보내기 ──────────────────────────────────────────────
def export_portfolio_json(all_values: list, sheet_name: str, today: datetime.date):
    blocks  = find_person_blocks(all_values)
    persons = []

    for block in blocks:
        person = block["person"]
        stocks = []
        for row_1 in range(block["row_start"], block["row_end"] + 1):
            idx = row_1 - 1
            if idx >= len(all_values): continue
            row = all_values[idx]

            def cell(c): return row[c-1] if len(row) >= c else ""

            name       = cell(10)
            rec_date_s = cell(11)
            base_price = cell(12)
            cur_price  = cell(13)

            if not name: continue
            name = str(name).strip()
            market, code = parse_stock(name)
            if not market: continue

            try:
                rec_date  = datetime.date.fromisoformat(rec_date_s[:10])
                sell_date = str(calc_sell_date(rec_date, market))
            except Exception:
                rec_date, sell_date = None, None

            try:
                bp = float(str(base_price).replace(",", ""))
                cp = float(str(cur_price).replace(",", ""))
                ret = round((cp - bp) / bp * 100, 2)
            except Exception:
                bp = cp = ret = None

            stocks.append({
                "name": name, "code": code, "market": market,
                "rec_date":      str(rec_date) if rec_date else None,
                "base_price":    bp,
                "current_price": cp,
                "return_pct":    ret,
                "sell_date":     sell_date,
            })

        # ── 실현 종목 (P-U열) ────────────────────────────────────
        realized = []
        for row_1 in range(block["row_start"], block["row_end"] + 1):
            idx = row_1 - 1
            if idx >= len(all_values): continue
            row = all_values[idx]

            def cell_r(c): return row[c-1] if len(row) >= c else ""

            p_name     = cell_r(16)   # P: 종목명
            p_rec      = cell_r(17)   # Q: 추천일
            p_sell_dt  = cell_r(18)   # R: 매도일
            p_base     = cell_r(19)   # S: 추천일 기준가
            p_sell_pr  = cell_r(20)   # T: 매도일 기준가
            p_ret      = cell_r(21)   # U: 수익률

            if not p_name: continue

            # 시장 판별 (US/KR)
            p_market, p_code = parse_stock(str(p_name).strip())

            try:
                ret_val = float(str(p_ret).replace("%", "").replace(",", ""))
                # U열이 소수(0.05 = 5%)인지 백분율(5.0)인지 판별
                if -1 < ret_val < 1 and ret_val != 0:
                    ret_val = round(ret_val * 100, 2)
                else:
                    ret_val = round(ret_val, 2)
            except Exception:
                ret_val = None

            try:
                bp = float(str(p_base).replace(",", ""))
                sp = float(str(p_sell_pr).replace(",", ""))
            except Exception:
                bp = sp = None

            realized.append({
                "name": str(p_name).strip(), "status": "sold",
                "market": p_market or "KR",
                "rec_date":    p_rec[:10] if p_rec else None,
                "sell_date":   p_sell_dt[:10] if p_sell_dt else None,
                "base_price":  bp,
                "sell_price":  sp,
                "return_pct":  ret_val,
            })

        # 총 수익률: 활성 + 실현 모두 포함
        all_rets = [s["return_pct"] for s in stocks if s["return_pct"] is not None] + \
                   [r["return_pct"] for r in realized if r["return_pct"] is not None]
        total = round(sum(all_rets), 2) if all_rets else 0.0

        if person:
            persons.append({"name": person, "stocks": stocks,
                            "realized": realized, "total_return": total})

    data = {"date": str(today), "sheet": sheet_name, "persons": persons}
    out  = BASE_DIR / "portfolio.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ portfolio.json 내보내기 완료")


# ── 카드뉴스 생성 + 텔레그램 전송 ────────────────────────────────────────
def run_card_and_telegram(today: datetime.date):
    # 재실행 안전장치: 데일리는 카드를 보낸 뒤에도 패널티·직전분기 처리를 이어간다.
    # 그 뒤에서 죽어 워크플로가 재시도하면 카드가 두 번 나갈 수 있으므로,
    # 발송에 성공하면 표식을 남기고 같은 날 재실행에서는 건너뛴다.
    sent_flag = BASE_DIR / f".card_sent_{today}"
    if sent_flag.exists():
        print("  · 카드 이미 발송됨 — 재발송 건너뜀")
        return
    try:
        import importlib.util
        mod_path = BASE_DIR / "generate_card_github.py"
        spec = importlib.util.spec_from_file_location("generate_card_github", mod_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sheet_name = json.loads((BASE_DIR / "portfolio.json").read_text())["sheet"]
        persons = mod.load_portfolio(skip_price_refresh=True)[1]  # 이미 갱신된 가격 사용
        card_path = mod.generate_image(sheet_name, persons, today)
        mod.send_telegram(card_path, today)
        sent_flag.write_text("sent")
    except Exception as e:
        print(f"[경고] 카드뉴스/텔레그램 실패: {e}")


# ── 실현 종목 매도일 소급 수정 ─────────────────────────────────────────────
def fix_realized_sell_dates(ws: gspread.Worksheet, today: datetime.date):
    """
    자동매도 원칙 변경에 따른 소급 수정:
    기존: prev_trading_day(rec_date + 1month)
    신규: prev_trading_day(rec_date + 1month - 1day)

    수동 매도 건은 건드리지 않음 (기존 공식 결과와 다른 날짜면 수동으로 판단)
    """
    all_values = sheet_retry(ws.get_all_values, "시트 읽기")
    blocks = find_person_blocks(all_values)
    updates = []
    fixed = []

    for block in blocks:
        person = block["person"]
        for row_1 in range(block["row_start"], block["row_end"] + 1):
            idx = row_1 - 1
            if idx >= len(all_values):
                continue
            row = all_values[idx]

            p_name    = row[15] if len(row) > 15 else ""  # P: 종목명
            p_rec     = row[16] if len(row) > 16 else ""  # Q: 추천일
            p_sell_dt = row[17] if len(row) > 17 else ""  # R: 매도일
            p_base    = row[18] if len(row) > 18 else ""  # S: 기준가
            p_sell_pr = row[19] if len(row) > 19 else ""  # T: 매도가

            if not p_name or not p_rec or not p_sell_dt:
                continue

            try:
                rec_date = datetime.date.fromisoformat(str(p_rec).strip()[:10])
                cur_sell = datetime.date.fromisoformat(str(p_sell_dt).strip()[:10])
            except (ValueError, TypeError):
                continue

            # 종목의 시장 판별
            market, code = parse_stock(str(p_name).strip())
            if not market:
                continue

            # 기존 공식으로 계산한 매도일 (rec_date + 1month 당일 포함)
            old_sell = prev_trading_day(rec_date + relativedelta(months=1), market)

            # 현재 매도일이 기존 공식 결과와 같으면 → 자동매도 건 → 소급 수정
            if cur_sell != old_sell:
                continue  # 수동 매도 건이므로 패스

            # 새 공식으로 재계산
            new_sell = calc_sell_date(rec_date, market)
            if new_sell == cur_sell:
                continue  # 이미 동일하면 패스
            if new_sell >= today:
                print(f"    [소급수정 대기] {person}/{p_name}: {new_sell}은 아직 종가 미확정")
                continue  # 종가 미확정 날짜는 수정하지 않음

            # 새 매도일 종가 조회
            new_price = close_at(market, code, new_sell)
            if new_price is None:
                new_price = fetch_price(market, code)
            if new_price is None:
                print(f"    [소급수정 실패] {person}/{p_name}: 매도가 조회 불가")
                continue

            # R열(매도일), T열(매도가) 수정, U열(수익률) 수식 유지
            updates.append({"range": f"R{row_1}", "values": [[str(new_sell)]]})
            updates.append({"range": f"T{row_1}", "values": [[new_price]]})
            updates.append({"range": f"U{row_1}", "values": [[f"=(T{row_1}-S{row_1})/S{row_1}"]]})

            fixed.append(f"{person}/{p_name}: {cur_sell}→{new_sell} ({new_price:,})")
            print(f"    [소급수정] {person}/{p_name}: {cur_sell} → {new_sell}, 매도가={new_price:,}")

    if updates:
        sheet_retry(lambda: ws.batch_update(updates, value_input_option="USER_ENTERED"), "일괄 갱신")
        print(f"  소급 수정 완료: {len(fixed)}건")
    else:
        print(f"  소급 수정 대상 없음")

    return fixed


# ── 실현 종목 매도가 검증 ──────────────────────────────────────────────────
def verify_realized_prices(ws: gspread.Worksheet, today: datetime.date):
    """
    실현(매도) 종목의 매도가(T열)가 매도일(R열) 종가와 일치하는지 검증.
    5% 이상 차이나면 Yahoo 종가로 수정한다.
    """
    all_values = sheet_retry(ws.get_all_values, "시트 읽기")
    blocks = find_person_blocks(all_values)
    updates = []
    fixed = []

    for block in blocks:
        person = block["person"]
        for row_1 in range(block["row_start"], block["row_end"] + 1):
            idx = row_1 - 1
            if idx >= len(all_values):
                continue
            row = all_values[idx]

            p_name    = row[15] if len(row) > 15 else ""  # P: 종목명
            p_sell_dt = row[17] if len(row) > 17 else ""  # R: 매도일
            p_base    = row[18] if len(row) > 18 else ""  # S: 기준가
            p_sell_pr = row[19] if len(row) > 19 else ""  # T: 매도가

            if not p_name or not p_sell_dt or not p_sell_pr:
                continue
            if is_adjustment(p_name):
                continue   # 패널티·보너스 행은 종목이 아니다

            try:
                sell_date = datetime.date.fromisoformat(str(p_sell_dt).strip()[:10])
            except (ValueError, TypeError):
                continue

            if sell_date >= today:
                continue  # 아직 종가 미확정

            market, code = parse_stock(str(p_name).strip())
            if not market or not code:
                continue

            try:
                recorded = float(str(p_sell_pr).replace(",", ""))
            except (ValueError, TypeError):
                continue

            # Yahoo에서 해당 날짜(휴장이면 직전 거래일) 종가 조회
            correct = close_on_or_before(market, code, sell_date)
            if correct is None:
                continue

            # 5% 이상 차이나면 수정
            diff = abs(correct - recorded) / max(recorded, 1)
            if diff < 0.05:
                continue

            # ⚠️ 자동 덮어쓰기 금지 (보고만).
            # 실현 기록은 확정된 과거값이다. Yahoo 수정주가는 증자·액면분할 권리락 때
            # 과거 시세를 소급 조정하므로, 매도가(T)만 새 값으로 덮으면 매수가(S)와
            # 기준이 어긋나 수익률이 무너진다. (2026-07-16~17 티엘비 무·유상증자 사고)
            fixed.append(f"{person}/{p_name}: 기록 {recorded:,.0f} vs 조회 {correct:,} ({sell_date}) — 확인 필요")
            print(f"    [매도가 의심] {person}/{p_name}: 기록 {recorded:,.0f} vs 조회 {correct:,} ({sell_date})")

    if fixed:
        print(f"  매도가 검증: 의심 {len(fixed)}건 (자동수정 안 함 — 권리락 소급조정일 수 있음)")
    else:
        print(f"  매도가 검증: 이상 없음")

    return fixed


# ── 메인 ────────────────────────────────────────────────────────────────
def today_kst():
    """KST(UTC+9) 기준 오늘 날짜"""
    from datetime import timezone, timedelta
    return datetime.datetime.now(timezone(timedelta(hours=9))).date()

def close_on_or_before(market: str, code: str, date: datetime.date, with_date: bool = False):
    """매도일 종가. 그날이 휴장이면 '직전 거래일' 종가를 쓴다(관례).
    fetch_price(date)는 다음 거래일 종가를 집어오므로 매도가 확정엔 이 함수를 쓸 것.
    with_date=True면 (종가, 그 종가의 날짜) 튜플을 준다."""
    fail = (None, None) if with_date else None
    # 국내는 네이버 일별시세 우선(야후 국내 일봉 지연 회피). 실패 시 야후 폴백.
    if market == "KR":
        ser = _naver_kr_closes(code)
        days = sorted(d for d in ser if d <= date)
        if days:
            val = round(ser[days[-1]])
            return (val, days[-1]) if with_date else val
    try:
        if market == "KR":
            suffix = ".KQ" if code in KOSDAQ_CODES else ".KS"
            ticker_str = code + suffix
        else:
            ticker_str = code
        hist = yf.Ticker(ticker_str).history(
            start=str(date - datetime.timedelta(days=10)),
            end=str(date + datetime.timedelta(days=1)), prepost=False)
        hist = hist["Close"].dropna()
        if hist.empty:
            return fail
        price = float(hist.iloc[-1])   # 매도일 이하 마지막 종가
        if math.isnan(price) or math.isinf(price):
            return fail
        val = round(price, 2) if market == "US" else round(price)
        if with_date:
            try:
                return val, hist.index[-1].date()
            except Exception:
                return val, None
        return val
    except Exception:
        return fail


def fix_pending_sells(ss, today: datetime.date):
    """리스너가 자동매도한 행(_pending_sell)의 매도가(T)를 매도일 종가로 확정한다.
    과거 데이터는 건드리지 않고, 목록에 있는 행만 정정 후 목록에서 제거."""
    try:
        # sheet_retry는 5xx만 재시도한다. 시트가 없으면 그대로 올라와 아래에서 잡힌다.
        wp = sheet_retry(lambda: ss.worksheet("_pending_sell"), "_pending_sell 열기")
    except gspread.WorksheetNotFound:
        return []
    rows = sheet_retry(wp.get_all_values, "시트 읽기")
    if len(rows) < 2:
        return []

    header, keep, fixed = rows[0], [], []
    for r in rows[1:]:
        if len(r) < 4:
            continue
        title, row_s, name, sd = r[0], r[1], r[2], r[3]
        try:
            row_i = int(str(row_s).strip())
            sell_date = datetime.date.fromisoformat(str(sd).strip()[:10])
        except Exception:
            continue
        if sell_date >= today:
            keep.append(r)          # 아직 종가 미확정 → 다음 실행에 처리
            continue
        try:
            ws2 = sheet_retry(lambda: ss.worksheet(title), f"'{title}' 열기")
        except Exception:
            continue
        market, code = parse_stock(str(name).strip())
        if not market or not code:
            continue
        correct = close_on_or_before(market, code, sell_date)
        if correct is None:
            keep.append(r)          # 조회 실패 → 다음 실행에 재시도
            continue
        sheet_retry(lambda: ws2.batch_update([
            {"range": f"T{row_i}", "values": [[correct]]},
            {"range": f"U{row_i}", "values": [[f"=(T{row_i}-S{row_i})/S{row_i}"]]},
        ], value_input_option="USER_ENTERED"), "매도가 확정")
        fixed.append(f"{name} {title} R{row_i} 매도가 → {correct:,} ({sell_date} 종가 확정)")

    wp.clear()
    sheet_retry(lambda: wp.append_rows([header] + keep), "_pending_sell 갱신")
    return fixed


def apply_missed_recommendation_penalties(ws: gspread.Worksheet, today: datetime.date):
    """주간 라운드 미추천자에게 -10% 패널티 자동 부여.
    규칙: 시트에 존재하는 추천일을 주 단위로 묶어 '그 주 첫 거래일'을 라운드로 보고,
          D<today 인 라운드마다 그날 활성/실현 추천이 없는 멤버에게
          실현섹션에 -10%(S=100,T=90) 기록. 이미 있으면 건너뜀(중복 방지).

    월요일로 못박아뒀더니 2026-08-17(광복절 대체공휴일)처럼 월요일이 휴장이면
    라운드 자체가 인식되지 않아 8/18 화요일 라운드의 패널티가 통째로 누락됐다."""
    def is_pen(p): return is_adjustment(p)   # 패널티·보너스 모두 추천이 아니다

    vals = sheet_retry(ws.get_all_values, "시트 읽기")
    blocks = find_person_blocks(vals)

    # 라운드 = 데이터에 존재하는 '월요일' 추천일(패널티 제외), 오늘보다 이전
    round_days = set()
    for b in blocks:
        for r in range(b["row_start"], b["row_end"] + 1):
            row = vals[r - 1]
            j = row[9]  if len(row) > 9  else ""
            k = row[10] if len(row) > 10 else ""
            pp = row[15] if len(row) > 15 else ""
            qq = row[16] if len(row) > 16 else ""
            for d, ok in [(k, bool(j)), (qq, bool(pp) and not is_pen(pp))]:
                if not ok:
                    continue
                try:
                    dd = datetime.date.fromisoformat(str(d).strip()[:10])
                    if dd == round_day(dd) and dd < today:
                        round_days.add(dd)
                except Exception:
                    pass

    applied = []
    for D in sorted(round_days):
        Ds = str(D)
        for b in blocks:
            person = b["person"]
            if not person:
                continue
            recommended = has_penalty = False
            empty_p = None
            # 예전엔 멤버마다 시트를 통째로 다시 읽었다. 라운드가 쌓이면서
            # (라운드 수 × 8명) 읽기가 분당 쿼터를 넘겨 2026-08-21 데일리가
            # 429로 죽었다. 이제 한 번만 읽고 쓴 내용은 로컬에 반영한다.
            for r in range(b["row_start"], b["row_end"] + 1):
                if r - 1 >= len(vals):
                    break
                row = vals[r - 1]
                j = row[9]  if len(row) > 9  else ""
                k = row[10] if len(row) > 10 else ""
                pp = row[15] if len(row) > 15 else ""
                qq = row[16] if len(row) > 16 else ""
                if j and Ds in str(k):
                    recommended = True
                if pp and Ds in str(qq) and not is_pen(pp):
                    recommended = True          # 실현(매도)된 종목도 그날 추천한 것
                if pp and Ds in str(qq) and is_pen(pp):
                    has_penalty = True
                if (not pp or not str(pp).strip()) and empty_p is None:
                    empty_p = r
            if recommended or has_penalty or empty_p is None:
                continue
            pr = empty_p
            sheet_retry(lambda: ws.batch_update([
                {"range": f"P{pr}", "values": [["미추천(패널티)"]]},
                {"range": f"Q{pr}", "values": [[Ds]]},
                {"range": f"R{pr}", "values": [[Ds]]},
                {"range": f"S{pr}", "values": [[100]]},
                {"range": f"T{pr}", "values": [[90]]},
                {"range": f"U{pr}", "values": [[f"=(T{pr}-S{pr})/S{pr}"]]},
            ], value_input_option="USER_ENTERED"), "패널티 기록")
            # 방금 쓴 행을 로컬 vals에 반영 → 다음 라운드 판정에 그대로 쓴다
            row_local = vals[pr - 1]
            while len(row_local) < 21:
                row_local.append("")
            row_local[15], row_local[16], row_local[17] = "미추천(패널티)", Ds, Ds
            row_local[18], row_local[19] = "100", "90"
            applied.append(f"{person} {Ds} 미추천 -10% (실현 R{pr})")
    return applied


def has_active_positions(ws: gspread.Worksheet) -> bool:
    """활성(미매도) 종목이 J열에 하나라도 있으면 True."""
    vals = sheet_retry(ws.get_all_values, "시트 읽기")
    for b in find_person_blocks(vals):
        for r in range(b["row_start"], b["row_end"] + 1):
            if r - 1 < len(vals):
                j = vals[r - 1][9] if len(vals[r - 1]) > 9 else ""
                if str(j).strip():
                    return True
    return False


def process_quarter(ws: gspread.Worksheet, today: datetime.date, is_current: bool):
    """한 분기 시트 처리: (현재 분기만 pending 추가) 현재가·자동매도·검증·portfolio·카드/텔레그램."""
    print(f"\n[시트] {ws.title}  (current={is_current})")
    all_values = sheet_retry(ws.get_all_values, "시트 읽기")

    # 1. 대기 종목 추가 (현재 분기 한정)
    if is_current:
        added = process_pending(ws, all_values, today)
        if added:
            all_values = sheet_retry(ws.get_all_values, "시트 읽기")

    # 2. 현재가 업데이트 + 자동 매도
    updated, sold, skipped = process_sheet(ws, today)
    print(f"  현재가 업데이트: {len(updated)}건 / 자동매도: {len(sold)}건")
    for s in sold:
        print(f"    · {s}")
    if skipped:
        print(f"  코드 미인식: {len(skipped)}건")

    # 3. 실현 매도일 소급 수정 + 매도가 검증
    for f in (fix_realized_sell_dates(ws, today) or []):
        print(f"    · {f}")
    for pf in (verify_realized_prices(ws, today) or []):
        print(f"    · {pf}")

    # 4. portfolio.json 내보내기 + 카드/텔레그램
    all_values = sheet_retry(ws.get_all_values, "시트 읽기")
    export_portfolio_json(all_values, ws.title, today)
    run_card_and_telegram(today)


def main():
    today = today_kst()
    print(f"=== 한탕 스터디 Google Sheets 업데이트 ({today}) ===")

    ss = get_spreadsheet()
    sheets = [s for s in sheet_retry(ss.worksheets, "시트 목록 조회")
              if not s.title.startswith("_")]
    current = sheets[-1]

    # 리스너 자동매도분 매도가를 종가로 확정
    for f in (fix_pending_sells(ss, today) or []):
        print(f"  · {f}")

    # 현재 분기 처리
    process_quarter(current, today, is_current=True)

    # 당일 미추천자 -10% 패널티 자동 부여 (월요일 라운드)
    for f in (apply_missed_recommendation_penalties(current, today) or []):
        print(f"  · [미추천패널티] {f}")

    # 직전 분기(예: 2분기)만, 활성 종목이 남아있는 동안 갱신 (모두 매도되면 자동 제외)
    if len(sheets) >= 2:
        prev = sheets[-2]
        if "분기" in prev.title and "테스트" not in prev.title and has_active_positions(prev):
            print(f"\n=== 직전 분기 갱신: {prev.title} (활성 종목 잔존) ===")
            process_quarter(prev, today, is_current=False)


if __name__ == "__main__":
    main()
