"""
GitHub Actions용 한탕 스터디 데일리 카드뉴스 생성 + 텔레그램 전송
──────────────────────────────────────────────────────────────────
- portfolio.json 읽기 (update_gsheets.py가 매일 업데이트)
- Yahoo Finance로 현재가 갱신 (단독 실행 시)
- Pillow로 16:9 화이트톤 카드뉴스 생성
- 서울 날씨 정보 포함
- 텔레그램으로 파일 전송

환경변수:
  TELEGRAM_TOKEN    - 봇 토큰
  TELEGRAM_CHAT_ID  - 전송할 채팅방 ID (쉼표로 여러 개 가능)
"""

import os, sys, json, re, datetime, urllib.request, subprocess, math
from pathlib import Path

# ── 패키지 설치 ──────────────────────────────────────────────────────────
def _ensure(pkg, import_name=None):
    try:
        __import__(import_name or pkg.replace("-", "_"))
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)

_ensure("Pillow", "PIL")
_ensure("yfinance")
_ensure("requests")

from PIL import Image, ImageDraw, ImageFont
import yfinance as yf
import requests

# ── 설정 ────────────────────────────────────────────────────────────────
BASE_DIR  = Path(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = Path("/tmp/fonts")

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_IDS = [
    int(c.strip())
    for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",")
    if c.strip()
]

# 16:9 가로 비율
CARD_W = 1920
CARD_H = 1080

# ── 화이트톤 컬러 팔레트 ──────────────────────────────────────────────
BG          = (248, 249, 252)
BG_CARD     = (255, 255, 255)
BG_HEADER   = (24, 28, 50)
Q3_BG       = (252, 248, 239)   # 3분기 전용: 살짝 따뜻한 아이보리 (2분기와 구분)
RED         = (220, 53, 69)
GREEN       = (16, 163, 127)
GOLD        = (255, 193, 7)
SILVER      = (173, 181, 189)
BRONZE      = (205, 133, 63)
GREY_TEXT   = (108, 117, 125)
GREY_LIGHT  = (222, 226, 230)
GREY_BORDER = (233, 236, 239)
DARK        = (33, 37, 41)
DARK_SUB    = (73, 80, 87)
WHITE       = (255, 255, 255)
BLUE_BADGE  = (13, 110, 253)
PINK_BADGE  = (214, 51, 132)
SOLD_BADGE  = (255, 153, 51)

# ── 폰트 ────────────────────────────────────────────────────────────────
def _download_fonts():
    FONTS_DIR.mkdir(exist_ok=True)
    files = {
        "NotoSansKR-Regular.otf":
            "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/KR/NotoSansKR-Regular.otf",
        "NotoSansKR-Bold.otf":
            "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/KR/NotoSansKR-Bold.otf",
        "NotoSansKR-Medium.otf":
            "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/KR/NotoSansKR-Medium.otf",
    }
    for fname, url in files.items():
        dst = FONTS_DIR / fname
        if not dst.exists():
            urllib.request.urlretrieve(url, dst)

_download_fonts()

def _font(bold=False, medium=False, size=16):
    if bold:
        fname = "NotoSansKR-Bold.otf"
    elif medium:
        fname = "NotoSansKR-Medium.otf"
    else:
        fname = "NotoSansKR-Regular.otf"
    try:    return ImageFont.truetype(str(FONTS_DIR / fname), size)
    except: return ImageFont.load_default()

# ── 주가 조회 (Yahoo Finance) ───────────────────────────────────────────
KOSDAQ_CODES = {"247540", "356860", "462350", "031330"}

def get_yahoo_price(market: str, code: str) -> float | None:
    try:
        if market == "KR":
            suffix = ".KQ" if code in KOSDAQ_CODES else ".KS"
            ticker_str = code + suffix
        else:
            ticker_str = code
        t = yf.Ticker(ticker_str)
        hist = t.history(period="2d", prepost=False)
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
            return round(price, 2) if market == "US" else int(price)
    except Exception as e:
        print(f"  [가격 조회 실패] {code}: {e}")
    return None

# ── 날씨 조회 (Open-Meteo, 무료 API) ────────────────────────────────────
WEATHER_DESC = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림",
    45: "안개", 48: "안개",
    51: "이슬비", 53: "이슬비", 55: "이슬비",
    61: "비", 63: "비", 65: "강한 비",
    71: "눈", 73: "눈", 75: "강한 눈",
    80: "소나기", 81: "소나기", 82: "강한 소나기",
    95: "뇌우", 96: "뇌우", 99: "뇌우",
}

def fetch_weather_seoul() -> dict | None:
    try:
        url = ("https://api.open-meteo.com/v1/forecast"
               "?latitude=37.5665&longitude=126.978"
               "&current=temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m"
               "&daily=temperature_2m_max,temperature_2m_min"
               "&timezone=Asia/Seoul&forecast_days=1")
        resp = requests.get(url, timeout=5)
        data = resp.json()
        cur = data["current"]
        daily = data["daily"]
        code = cur["weather_code"]
        return {
            "temp": round(cur["temperature_2m"]),
            "humidity": cur["relative_humidity_2m"],
            "high": round(daily["temperature_2m_max"][0]),
            "low": round(daily["temperature_2m_min"][0]),
            "desc": WEATHER_DESC.get(code, ""),
        }
    except Exception as e:
        print(f"  [날씨 조회 실패] {e}")
        return None

# ── 시장 지표 조회 ───────────────────────────────────────────────────────
# (구) _fetch_naver_index / _fetch_yahoo_index 제거 — 아래 네이버 일별시세 경로로 일원화

# 시장지표는 전부 네이버 일별 시세로 조회한다.
#   Yahoo(^KS11·^N225 등)는 결측일이 생긴다 — 2026-08-05 국내·일본 지수 봉이 통째로
#   빠져 있어 8/6 카드에 이틀 전(8/4) 값이 실렸다. 네이버는 같은 날짜가 정상 존재.
#   또 행마다 fluctuationsRatio(전일 대비)가 같이 오므로 '두 봉 비교'가 필요 없다.
NAVER_INDICES = [
    ("KOSPI",  "KOSPI",     "전일", True),
    ("KOSDAQ", "KOSDAQ",    "전일", True),
    (".INX",   "S&P 500",   "",     False),
    (".IXIC",  "NASDAQ",    "",     False),
    (".N225",  "닛케이225",  "",     False),
    (".HSI",   "항셍",       "",     False),
    (".SSEC",  "상해종합",   "",     False),
]


def _index_target_date(domestic: bool) -> datetime.date:
    """'마지막으로 장이 끝난' 날짜. 장중 값이 섞이지 않게 하는 기준선.
       국내: KST 16시 이후면 당일, 아니면 전일
       해외: KST 6시 이후면 전일, 아니면 그제 (데일리는 07:00 실행)"""
    from datetime import timezone, timedelta
    now = datetime.datetime.now(timezone(timedelta(hours=9)))
    if domestic:
        return now.date() if now.hour >= 16 else now.date() - datetime.timedelta(days=1)
    return now.date() - datetime.timedelta(days=1 if now.hour >= 6 else 2)


def _naver_rows(url: str):
    r = requests.get(url, timeout=8,
                     headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://m.stock.naver.com/"})
    rows = r.json()
    return rows if isinstance(rows, list) else []


def _pick_row(rows, target: datetime.date):
    """기준일 이하의 가장 최근 행. 장중 행(오늘)이 섞여도 걸러진다."""
    for row in rows:
        d = str(row.get("localTradedAt", ""))[:10]
        try:
            if datetime.date.fromisoformat(d) <= target:
                return row, d
        except ValueError:
            continue
    return None, ""


def _fetch_naver_index_row(code: str, name: str, tag: str, domestic: bool):
    try:
        base = ("https://m.stock.naver.com/api/index/{}/price"
                if domestic else "https://api.stock.naver.com/index/{}/price")
        rows = _naver_rows(base.format(code) + "?pageSize=8&page=1")
        target = _index_target_date(domestic)
        row, on = _pick_row(rows, target)
        if not row:
            print(f"  [지수 없음] {code}: 기준일 {target} 이하 데이터 없음")
            return None
        price = float(str(row["closePrice"]).replace(",", ""))
        ratio = float(str(row["fluctuationsRatio"]).replace(",", "")) / 100
        if on != str(target):
            print(f"  [주의] {name} {target} 아닌 {on} 종가 사용(휴장 또는 미게시)")
        return {"name": name, "tag": tag, "price": price, "change": ratio}
    except Exception as e:
        print(f"  [네이버 지수 실패] {code}: {e}")
        return None


def _fetch_usdkrw():
    try:
        rows = _naver_rows("https://api.stock.naver.com/marketindex/exchange/"
                           "FX_USDKRW/prices?page=1&pageSize=8")
        row, on = _pick_row(rows, _index_target_date(False))
        if not row:
            return None
        return {"name": "USD/KRW", "tag": "",
                "price": float(str(row["closePrice"]).replace(",", "")),
                "change": float(str(row["fluctuationsRatio"]).replace(",", "")) / 100}
    except Exception as e:
        print(f"  [환율 조회 실패] {e}")
        return None


def fetch_market_data() -> list:
    """국내·해외 지수 + 환율. 전부 네이버 일별 시세의 '마지막 완료 세션' 행을 쓴다."""
    results = []
    for code, name, tag, domestic in NAVER_INDICES:
        r = _fetch_naver_index_row(code, name, tag, domestic)
        if r:
            results.append(r)
    fx = _fetch_usdkrw()
    if fx:
        results.append(fx)
    if results:
        print("  시장지표: " + " | ".join(
            f"{r['name']} {r['price']:,.2f} {r['change']*100:+.2f}%" for r in results))
    return results

# ── 포트폴리오 로드 ─────────────────────────────────────────────────────
def load_portfolio(skip_price_refresh=False):
    path = BASE_DIR / "portfolio.json"
    if not path.exists():
        raise FileNotFoundError("portfolio.json 없음")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    sheet_name = data.get("sheet", "")
    persons_raw = data.get("persons", [])

    persons = []
    for p in persons_raw:
        stocks = []
        for s in p.get("stocks", []):
            market = s.get("market")
            code   = s.get("code")
            if not market or not code:
                continue
            base_price = s.get("base_price")
            if skip_price_refresh:
                cur_price = s.get("current_price")
            else:
                cur_price = get_yahoo_price(market, code)
            ret = None
            if base_price and cur_price:
                ret = (cur_price - base_price) / base_price
            stocks.append({
                "name": s.get("name", ""), "short": shorten_name(s.get("name", "")),
                "market": market, "rec_date": s.get("rec_date", ""),
                "base": base_price or 0, "current": cur_price or 0,
                "ret": ret, "sell_date": s.get("sell_date", ""),
            })

        realized = []
        for r in p.get("realized", []):
            ret_pct = r.get("return_pct")
            ret = ret_pct / 100 if ret_pct is not None else None
            realized.append({
                "name": r.get("name", ""), "short": shorten_name(r.get("name", "")),
                "status": "sold", "market": r.get("market", "KR"),
                "rec_date": r.get("rec_date", ""),
                "sell_date": r.get("sell_date", ""),
                "base": r.get("base_price") or 0, "sell_price": r.get("sell_price") or 0,
                "ret": ret,
            })

        active_rets = [s["ret"] for s in stocks if s["ret"] is not None]
        realized_rets = [r["ret"] for r in realized if r["ret"] is not None]
        all_rets = active_rets + realized_rets
        total_ret = sum(all_rets) if all_rets else 0

        # 추천일 오름차순 정렬 (오래된 것 위, 최근 것 아래)
        stocks.sort(key=lambda x: x.get("rec_date", ""))

        if stocks or realized:
            persons.append({
                "person": p["name"], "stocks": stocks,
                "realized": realized, "total_ret": total_ret,
            })

    persons.sort(key=lambda x: x["total_ret"], reverse=True)
    return sheet_name, persons


def shorten_name(name: str) -> str:
    name = str(name).strip()
    # 괄호 안 종목코드/티커 제거: "종목명 (CODE)" → "종목명"
    m = re.match(r"^(.+?)\s*\([A-Z0-9]+\)\s*$", name)
    if m:
        name = m.group(1).strip()

    # 긴 ETF/종목명 축약 규칙 (부분 매칭)
    shorten_rules = [
        ("삼성전자SK하이닉스채권혼합", "삼성전자SK혼합"),
        ("미국채10년선물", "미국채10년"),
        ("미국테크TOP10 INDXX", "미국테크TOP10"),
        ("원유선물인버스", "원유인버스"),
        ("2차전지&원자재", "2차전지원자재"),
    ]
    for pattern, short in shorten_rules:
        if pattern in name:
            name = name.replace(pattern, short)
            break

    return name

# ── 드로잉 헬퍼 ─────────────────────────────────────────────────────────
def draw_rounded_rect(d, x0, y0, x1, y1, fill, radius=0, outline=None):
    if radius == 0:
        d.rectangle([x0, y0, x1, y1], fill=fill, outline=outline)
    else:
        d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill, outline=outline)

def text_right(d, x, y, text, font, fill):
    bbox = font.getbbox(text)
    d.text((x - (bbox[2] - bbox[0]), y), text, font=font, fill=fill)

def text_center(d, x, y, text, font, fill):
    bbox = font.getbbox(text)
    d.text((x - (bbox[2] - bbox[0]) // 2, y), text, font=font, fill=fill)

def pct_str(v):
    if v is None: return "—"
    return f"+{v*100:.1f}%" if v >= 0 else f"{v*100:.1f}%"

def pct_color(v):
    if v is None: return GREY_TEXT
    return GREEN if v >= 0 else RED

def price_str(v, market):
    if not v: return "—"
    return f"${v:,.2f}" if market == "US" else f"{int(v):,}"

# ── 헤더 ────────────────────────────────────────────────────────────────
def render_header(d, today, sheet_name, weather):
    draw_rounded_rect(d, 0, 0, CARD_W, 90, BG_HEADER)
    tag_font = _font(bold=True, size=13)
    draw_rounded_rect(d, 32, 16, 155, 36, RED, radius=3)
    d.text((42, 17), "DAILY REPORT", font=tag_font, fill=WHITE)
    d.text((170, 12), "한탕 스터디", font=_font(bold=True, size=30), fill=WHITE)
    day_names = ["월", "화", "수", "목", "금", "토", "일"]
    day_ko = day_names[today.weekday()]
    date_str = f"{sheet_name}  |  {today.strftime('%Y.%m.%d')} ({day_ko})"
    d.text((32, 55), date_str, font=_font(size=14), fill=(150, 155, 175))
    if weather:
        wx = CARD_W - 32
        text_right(d, wx, 8, "오늘의 날씨", _font(bold=True, size=10), (100, 110, 140))
        text_right(d, wx, 26, f"서울  {weather['desc']}  {weather['temp']}C",
                   _font(bold=True, size=20), WHITE)
        text_right(d, wx, 56,
                   f"최고 {weather['high']} / 최저 {weather['low']}  습도 {weather['humidity']}%",
                   _font(size=12), (150, 155, 175))
    d.rectangle([0, 90, CARD_W, 93], fill=RED)

# ── 랭킹 패널 (좌측) ────────────────────────────────────────────────────
def render_ranking_panel(d, persons, x0, y0, w, h):
    draw_rounded_rect(d, x0, y0, x0+w, y0+h, BG_CARD, radius=12, outline=GREY_BORDER)
    d.rectangle([x0, y0+12, x0+4, y0+36], fill=RED)
    d.text((x0+16, y0+10), "RANKING", font=_font(bold=True, size=11), fill=RED)
    d.text((x0+16, y0+26), "수익률 순위", font=_font(bold=True, size=18), fill=DARK)
    RANK_COLORS = [GOLD, SILVER, BRONZE]
    ROW_H = 52
    sy = y0 + 58
    for i, p in enumerate(persons):
        ry = sy + i * ROW_H
        if i > 0:
            d.line([x0+16, ry, x0+w-16, ry], fill=GREY_BORDER, width=1)
        cx, cy = x0 + 34, ry + ROW_H // 2
        rc = RANK_COLORS[i] if i < 3 else GREY_LIGHT
        fc = WHITE if i < 3 else GREY_TEXT
        d.ellipse([cx-14, cy-14, cx+14, cy+14], fill=rc)
        rn_font = _font(bold=True, size=14)
        rn = str(i + 1)
        rb = rn_font.getbbox(rn)
        d.text((cx - (rb[2]-rb[0])//2, cy - (rb[3]-rb[1])//2 - 1), rn, font=rn_font, fill=fc)
        d.text((x0+60, ry+8), p["person"], font=_font(bold=True, size=17), fill=DARK)
        n_stocks = len(p["stocks"])
        n_realized = len(p.get("realized", []))
        count_str = f"{n_stocks}종목"
        if n_realized:
            count_str += f" +{n_realized}매도"
        d.text((x0+60, ry+30), count_str, font=_font(size=11), fill=GREY_TEXT)
        ret = p["total_ret"]
        text_right(d, x0+w-20, ry+12, pct_str(ret), _font(bold=True, size=20), pct_color(ret))

# ── 포트폴리오 그리드 (우측 4x2) ─────────────────────────────────────────
def render_portfolio_grid(d, persons, x0, y0, w, h):
    cols, rows, gap = 4, 2, 12
    card_w = (w - gap * (cols - 1)) // cols
    card_h = (h - gap * (rows - 1)) // rows
    for i, p in enumerate(persons):
        if i >= cols * rows:
            break
        col, row = i % cols, i // cols
        cx = x0 + col * (card_w + gap)
        cy = y0 + row * (card_h + gap)
        render_person_card(d, p, i, cx, cy, card_w, card_h)

def render_person_card(d, person, rank, x, y, w, h):
    RANK_COLORS = [GOLD, SILVER, BRONZE]
    draw_rounded_rect(d, x, y, x+w, y+h, BG_CARD, radius=10, outline=GREY_BORDER)
    hdr_h = 36
    hdr_fill = RANK_COLORS[rank] if rank < 3 else (245, 247, 250)
    draw_rounded_rect(d, x, y, x+w, y+hdr_h, hdr_fill, radius=10)
    d.rectangle([x, y+hdr_h-10, x+w, y+hdr_h], fill=hdr_fill)
    name_color = WHITE if rank < 3 else DARK
    rank_str = f"{rank+1}위"
    d.text((x+12, y+7), rank_str, font=_font(bold=True, size=12), fill=name_color)
    rw = _font(bold=True, size=12).getbbox(rank_str)[2] + 6
    d.text((x+12+rw, y+5), person["person"], font=_font(bold=True, size=16), fill=name_color)
    ret = person["total_ret"]
    ret_color = WHITE if rank < 3 else pct_color(ret)
    text_right(d, x+w-12, y+6, pct_str(ret), _font(bold=True, size=17), ret_color)
    sy = y + hdr_h + 4
    active_items = [s for s in person["stocks"]]
    sold_items = person.get("realized", [])
    avail_h = h - hdr_h - 10

    # 매도 종목은 1행(22px), 활성 종목은 2행(나머지 균등 배분)
    sold_h = 22
    total_sold_h = len(sold_items) * sold_h
    active_space = avail_h - total_sold_h
    active_h = min(48, active_space // max(len(active_items), 1)) if active_items else 0

    # 활성 종목 먼저 렌더링
    iy = sy
    for j, item in enumerate(active_items):
        if iy + active_h > y + h - 2:
            break
        mkt = item.get("market", "KR")
        if j > 0:
            d.line([x+10, iy, x+w-10, iy], fill=GREY_BORDER, width=1)

        # 뱃지
        if mkt == "KR":
            badge_bg, badge_text, badge_label = (219, 234, 254), BLUE_BADGE, "KR"
        else:
            badge_bg, badge_text, badge_label = (248, 219, 237), PINK_BADGE, "US"
        draw_rounded_rect(d, x+10, iy+4, x+40, iy+19, badge_bg, radius=3)
        bf = _font(bold=True, size=9)
        bb = bf.getbbox(badge_label)
        d.text((x+10+(30-(bb[2]-bb[0]))//2, iy+5), badge_label, font=bf, fill=badge_text)

        # 수익률
        ret_text = pct_str(item.get("ret"))
        ret_font = _font(bold=True, size=13)
        ret_w = ret_font.getbbox(ret_text)[2] - ret_font.getbbox(ret_text)[0]

        # 종목명 (동적 폰트)
        name_display = item.get("short", item.get("name", ""))
        name_x = x + 46
        available_w = (x + w - 12) - name_x - ret_w - 8
        name_font_size = 12
        name_font = _font(medium=True, size=name_font_size)
        name_w = name_font.getbbox(name_display)[2] - name_font.getbbox(name_display)[0]
        if name_w > available_w and name_font_size > 9:
            name_font_size = 10
            name_font = _font(medium=True, size=name_font_size)
            name_w = name_font.getbbox(name_display)[2] - name_font.getbbox(name_display)[0]
        if name_w > available_w:
            while len(name_display) > 4 and name_font.getbbox(name_display + "…")[2] - name_font.getbbox(name_display + "…")[0] > available_w:
                name_display = name_display[:-1]
            name_display = name_display + "…"
        d.text((name_x, iy+3), name_display, font=name_font, fill=DARK_SUB)
        text_right(d, x+w-12, iy+3, ret_text, ret_font, pct_color(item.get("ret")))

        # 2행: 추천일 · 가격
        if active_h >= 38:
            rec = item.get("rec_date", "")
            rec_short = rec[5:] if rec and len(rec) >= 10 else rec
            meta = f"{rec_short}  {price_str(item.get('base',0), mkt)} → {price_str(item.get('current',0), mkt)}"
            d.text((x+46, iy+22), meta, font=_font(size=10), fill=GREY_TEXT)

        iy += active_h

    # 매도 종목: 1행 컴팩트 (종목명 수익률만)
    if sold_items and iy < y + h - 2:
        d.line([x+10, iy+2, x+w-10, iy+2], fill=GREY_LIGHT, width=1)
        iy += 4
    for j, item in enumerate(sold_items):
        if iy + sold_h > y + h - 2:
            break
        mkt = item.get("market", "KR")
        # 매도/기타 뱃지 (작게)
        stock_name_raw = item.get("name", "")
        if "미추천" in stock_name_raw or "패널티" in stock_name_raw:
            badge_label = "기타"
            badge_bg = (219, 229, 255)       # 연한 파랑
            badge_fg = (70, 100, 180)        # 진한 파랑
        else:
            badge_label = "매도"
            badge_bg = (255, 237, 219)       # 기존 연한 주황
            badge_fg = SOLD_BADGE
        draw_rounded_rect(d, x+10, iy+3, x+34, iy+15, badge_bg, radius=2)
        sf = _font(bold=True, size=8)
        sb = sf.getbbox(badge_label)
        d.text((x+10+(24-(sb[2]-sb[0]))//2, iy+3), badge_label, font=sf, fill=badge_fg)

        # 종목명 + 매수일→매도일 (작은 폰트)
        name_display = item.get("short", item.get("name", ""))
        rec_dt = item.get("rec_date", "")
        rec_short = rec_dt[5:] if rec_dt and len(rec_dt) >= 10 else rec_dt
        sell_dt = item.get("sell_date", "")
        sell_short = sell_dt[5:] if sell_dt and len(sell_dt) >= 10 else sell_dt
        date_range = f"{rec_short}→{sell_short}" if rec_short else sell_short
        label = f"{name_display} {date_range}"
        d.text((x+38, iy+2), label, font=_font(size=9), fill=GREY_TEXT)

        # 수익률 (우측)
        ret_text = pct_str(item.get("ret"))
        text_right(d, x+w-12, iy+2, ret_text, _font(bold=True, size=10), pct_color(item.get("ret")))
        iy += sold_h

# ── 시장 지표 패널 (랭킹 아래) ────────────────────────────────────────────
def render_market_panel(d, market_data, x0, y0, w, h):
    draw_rounded_rect(d, x0, y0, x0+w, y0+h, BG_CARD, radius=12, outline=GREY_BORDER)
    d.rectangle([x0, y0+12, x0+4, y0+36], fill=(13, 110, 253))
    d.text((x0+16, y0+10), "MARKET", font=_font(bold=True, size=11), fill=(13, 110, 253))
    d.text((x0+16, y0+26), "시장 지표", font=_font(bold=True, size=18), fill=DARK)

    if not market_data:
        d.text((x0+16, y0+58), "데이터 조회 실패", font=_font(size=12), fill=GREY_TEXT)
        return

    ROW_H = 34
    sy = y0 + 50
    for i, m in enumerate(market_data):
        ry = sy + i * ROW_H
        if ry + ROW_H > y0 + h - 4:
            break
        if i > 0:
            d.line([x0+16, ry, x0+w-16, ry], fill=GREY_BORDER, width=1)

        # 이름 + 태그
        d.text((x0+16, ry+6), m["name"], font=_font(bold=True, size=14), fill=DARK)
        if m["tag"]:
            nw = _font(bold=True, size=14).getbbox(m["name"])[2] + 6
            d.text((x0+16+nw, ry+9), m["tag"], font=_font(size=9), fill=GREY_TEXT)

        # 가격
        price = m["price"]
        is_fx = "USD" in m["name"]
        if is_fx:
            price_text = f"{price:,.1f}"
        elif price > 10000:
            price_text = f"{price:,.0f}"
        else:
            price_text = f"{price:,.2f}"
        text_right(d, x0+w-16, ry+4, price_text, _font(bold=True, size=15), DARK)

        # 등락률
        chg = m.get("change")
        if chg is not None:
            chg_str = f"+{chg*100:.2f}%" if chg >= 0 else f"{chg*100:.2f}%"
            text_right(d, x0+w-16, ry+22, chg_str, _font(bold=True, size=11), pct_color(chg))

# ── 푸터 ────────────────────────────────────────────────────────────────
def render_footer(d, y):
    d.line([32, y, CARD_W-32, y], fill=GREY_BORDER, width=1)
    text_center(d, CARD_W//2, y+8,
                "한탕 스터디  |  매일 오전 7:00 자동 업데이트  |  KRX / NYSE  |  Yahoo Finance",
                _font(size=11), GREY_TEXT)

# ── 이미지 생성 ──────────────────────────────────────────────────────────
def generate_image(sheet_name: str, persons: list, today: datetime.date) -> str:
    weather = fetch_weather_seoul()
    if weather:
        print(f"  날씨: 서울 {weather['desc']} {weather['temp']}C")

    # 시장 지표 조회
    market_data = fetch_market_data()
    print(f"  시장 지표: {len(market_data)}건")

    page_bg = Q3_BG if "3분기" in str(sheet_name) else BG
    img = Image.new("RGB", (CARD_W, CARD_H), page_bg)
    d   = ImageDraw.Draw(img)
    render_header(d, today, sheet_name, weather)

    content_y = 108
    content_h = CARD_H - content_y - 40
    ranking_w = 340
    left_x = 24

    # 랭킹 패널 (상단)
    ranking_h = 58 + len(persons) * 52 + 12  # 헤더 + 행들 + 패딩
    render_ranking_panel(d, persons, left_x, content_y, ranking_w, ranking_h)

    # 시장 지표 패널 (랭킹 아래)
    market_y = content_y + ranking_h + 12
    market_h = content_y + content_h - market_y
    if market_h > 80:
        render_market_panel(d, market_data, left_x, market_y, ranking_w, market_h)

    # 우측: 포트폴리오 그리드
    grid_x = left_x + ranking_w + 16
    grid_w = CARD_W - grid_x - 24
    render_portfolio_grid(d, persons, grid_x, content_y, grid_w, content_h)

    render_footer(d, CARD_H - 32)
    m_q = re.search(r"(\d+\s*분기)", str(sheet_name))
    q_tag = m_q.group(1).replace(" ", "") if m_q else ""
    suffix = f"_{q_tag}" if q_tag else ""
    out = BASE_DIR / f"한탕_데일리{suffix}_{today.strftime('%Y-%m-%d')}.png"
    img.save(str(out), "PNG", optimize=True)
    print(f"카드뉴스 저장: {out.name} ({CARD_W}x{CARD_H})")
    return str(out)

# ── 텔레그램 전송 ────────────────────────────────────────────────────────
def send_telegram(image_path: str, today: datetime.date):
    if not TELEGRAM_TOKEN:
        print("[텔레그램] TELEGRAM_TOKEN 미설정")
        return
    if not TELEGRAM_CHAT_IDS:
        print("[텔레그램] TELEGRAM_CHAT_ID 미설정")
        return
    import os as _os
    m_qc = re.search(r"(\d+분기)", _os.path.basename(image_path))
    q_txt = f" ({m_qc.group(1)})" if m_qc else ""
    caption = f"한탕 스터디 데일리 리포트{q_txt} {today}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    for cid in TELEGRAM_CHAT_IDS:
        with open(image_path, "rb") as f:
            r = requests.post(url,
                data={"chat_id": cid, "caption": caption},
                files={"document": f}, timeout=30)
        print(f"  텔레그램 {'OK' if r.ok else 'FAIL'} (chat_id={cid})")
        if not r.ok:
            try:
                print(f"    └ 오류 {r.status_code}: {r.text[:300]}")
            except Exception:
                pass

# ── 메인 ────────────────────────────────────────────────────────────────
def today_kst():
    from datetime import timezone, timedelta
    return datetime.datetime.now(timezone(timedelta(hours=9))).date()

if __name__ == "__main__":
    today = today_kst()
    print(f"=== 한탕 데일리 리포트 생성 ({today}) ===")
    sheet_name, persons = load_portfolio()
    print(f"  포트폴리오 로드: {len(persons)}명")
    image_path = generate_image(sheet_name, persons, today)
    send_telegram(image_path, today)
