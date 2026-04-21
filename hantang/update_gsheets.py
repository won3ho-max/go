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

import os, sys, re, json, datetime, subprocess
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
    "키움증권": "039490", "효성티앤씨": "298050",
    "삼성전기": "009150", "케이엔솔": "053080",
}
KOSDAQ_CODES = {"247540","356860","462350","031330"}  # 066970(엘앤에프)는 야후서 .KS로 등록

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
    return get_client().open_by_key(sheet_id)

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

        result = int(price) if market == "KR" else round(price, 2)
        _yf_cache[cache_key] = result
        return result
    except Exception as e:
        print(f"    [가격 조회 실패] {code}: {e}")
        return None


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
def _search_naver_stock(name: str):
    """네이버 금융 검색 API로 종목명 → 종목코드/시장 자동 조회"""
    try:
        url = "https://ac.stock.naver.com/ac"
        resp = requests.get(url, params={"q": name, "target": "stock"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        items = resp.json().get("items", [])
        for item in items:
            if item.get("name") == name or name in item.get("name", ""):
                code = item.get("code", "")
                type_code = item.get("typeCode", "")
                if not code:
                    continue
                # 해외 종목 (NYSE, NASDAQ 등)
                if type_code not in ("KOSPI", "KOSDAQ"):
                    print(f"    [자동매칭] {name} → {code} (US/{type_code})")
                    return "US", code
                # 한국 종목
                KOREAN_CODES[name] = code
                if type_code == "KOSDAQ":
                    KOSDAQ_CODES.add(code)
                print(f"    [자동매칭] {name} → {code} ({type_code})")
                return "KR", code
    except Exception as e:
        print(f"    [네이버 검색 실패] {name}: {e}")
    return None, None


def parse_stock(name: str):
    name = str(name).strip()
    m = re.search(r"\(([A-Z]{1,5})\)\s*$", name)
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

def calc_sell_date(rec_date: datetime.date, market: str) -> datetime.date:
    return prev_trading_day(rec_date + relativedelta(months=1), market)

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
    all_values = ws.get_all_values()

    blocks   = find_person_blocks(all_values)
    updates  = []   # (row, col, value) 배치 업데이트용
    updated, sold, skipped = [], [], []

    # ── 병렬 주가 조회를 위한 사전 스캔 ────────────────────────────────
    price_jobs = []
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
            price_jobs.append((market, code, None))
            try:
                rec_date = datetime.date.fromisoformat(rec_date_s[:10])
                sell_date = calc_sell_date(rec_date, market)
                if sell_date <= today:
                    price_jobs.append((market, code, sell_date))
            except Exception:
                pass
    prefetch_prices(price_jobs)

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

            # 추천일 파싱
            try:
                rec_date = datetime.date.fromisoformat(rec_date_s[:10])
            except Exception:
                skipped.append(f"{person}/{name} (추천일 오류)")
                continue

            # 기준가 미설정 → 오늘 종가로 채움
            if not base_price:
                price = fetch_price(market, code)
                if price:
                    updates.append((row_1, 12, price))   # L
                    updates.append((row_1, 13, price))   # M
                    updates.append((row_1, 14, f"=(M{row_1}-L{row_1})/L{row_1}"))  # N
                    base_price = str(price)
                    updated.append(f"{person}/{name} 기준가 설정: {price:,}")
                    print(f"    [기준가] {person}/{name}: {price:,}")
                else:
                    skipped.append(f"{person}/{name} (기준가 조회 실패)")
                    continue

            try:
                base_f = float(str(base_price).replace(",", ""))
            except Exception:
                skipped.append(f"{person}/{name} (기준가 형식 오류)")
                continue

            sell_date = calc_sell_date(rec_date, market)

            # ── 자동 매도 ─────────────────────────────────────────────
            if sell_date <= today:
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

                sell_price = fetch_price(market, code, sell_date)
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

            # ── 현재가 업데이트 ───────────────────────────────────────
            else:
                price = fetch_price(market, code)
                if price is not None:
                    updates.append((row_1, 13, price))   # M
                    if not cell(14):
                        updates.append((row_1, 14, f"=(M{row_1}-L{row_1})/L{row_1}"))  # N
                    ret = (price - base_f) / base_f * 100
                    updated.append(f"{person}/{name}: {price:,} ({ret:+.1f}%)")
                    print(f"    ✓ {person}/{name} → {price:,} ({ret:+.1f}%)")
                else:
                    print(f"    ✗ {person}/{name} → 조회 실패")

    # ── 배치 업데이트 ────────────────────────────────────────────────────
    if updates:
        print(f"  배치 업데이트: {len(updates)}셀")
        cell_list = []
        for row_1, col_1, val in updates:
            c = gspread.Cell(row=row_1, col=col_1, value=val)
            cell_list.append(c)
        ws.update_cells(cell_list, value_input_option="USER_ENTERED")

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
        ws.update_cells(cell_list, value_input_option="USER_ENTERED")
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
    except Exception as e:
        print(f"[경고] 카드뉴스/텔레그램 실패: {e}")


# ── 메인 ────────────────────────────────────────────────────────────────
def today_kst():
    """KST(UTC+9) 기준 오늘 날짜"""
    from datetime import timezone, timedelta
    return datetime.datetime.now(timezone(timedelta(hours=9))).date()

def main():
    today = today_kst()
    print(f"=== 한탕 스터디 Google Sheets 업데이트 ({today}) ===\n")

    ss = get_spreadsheet()
    # 마지막 시트 = 현재 분기
    sheets = [s for s in ss.worksheets() if not s.title.startswith("_")]
    ws = sheets[-1]
    print(f"[시트] {ws.title}")

    all_values = ws.get_all_values()

    # 1. 대기 종목 추가
    added = process_pending(ws, all_values, today)

    # 2. 현재가 업데이트 + 자동 매도
    if added:
        all_values = ws.get_all_values()   # pending 반영 시에만 재로드
    updated, sold, skipped = process_sheet(ws, today)

    print("\n" + "="*50)
    print(f"현재가 업데이트: {len(updated)}건")
    print(f"자동 매도 처리:  {len(sold)}건")
    if sold:
        for s in sold: print(f"  · {s}")
    if skipped:
        print(f"코드 미인식:     {len(skipped)}건")

    # 3. portfolio.json 내보내기
    all_values = ws.get_all_values()
    export_portfolio_json(all_values, ws.title, today)

    # 4. 카드뉴스 생성 + 텔레그램
    run_card_and_telegram(today)


if __name__ == "__main__":
    main()
