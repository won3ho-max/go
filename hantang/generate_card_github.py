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
                "status": "sold", "rec_date": r.get("rec_date", ""),
                "sell_date": r.get("sell_date", ""),
                "base": r.get("base_price") or 0, "sell_price": r.get("sell_price") or 0,
                "ret": ret,
            })

        active_rets = [s["ret"] for s in stocks if s["ret"] is not None]
        realized_rets = [r["ret"] for r in realized if r["ret"] is not None]
        all_rets = active_rets + realized_rets
        total_ret = sum(all_rets) if all_rets else 0

        if stocks or realized:
            persons.append({
                "person": p["name"], "stocks": stocks,
                "realized": realized, "total_ret": total_ret,
            })

    persons.sort(key=lambda x: x["total_ret"], reverse=True)
    return sheet_name, persons


def shorten_name(name: str) -> str:
    name = str(name).strip()
    m = re.match(r"^(.+?)\s*\([A-Z0-9]+\)\s*$", name)
    if m:
        return m.group(1).strip()
    replacements = {
        "RISE 삼성전자SK하이닉스채권혼합50 (0162Z0)": "RISE 삼성전자SK혼합",
        "TIGER 미국채10년선물 (305080)": "TIGER 미국채10년",
        "TIGER 미국테크TOP10 INDXX (381170)": "TIGER 미국테크TOP10",
        "TIGER 원유선물인버스(H) (217770)": "TIGER 원유인버스",
    }
    return replacements.get(name, name)

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
        text_right(d, wx, 18, f"서울  {weather['desc']}  {weather['temp']}C",
                   _font(bold=True, size=18), WHITE)
        text_right(d, wx, 48,
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
    sy = y + hdr_h + 6
    all_items = person["stocks"] + person.get("realized", [])
    item_h = min(28, (h - hdr_h - 12) // max(len(all_items), 1))
    for j, item in enumerate(all_items):
        iy = sy + j * item_h
        if iy + item_h > y + h - 4:
            break
        is_sold = item.get("status") == "sold"
        if is_sold:
            badge_bg, badge_text, badge_label = (255, 237, 219), SOLD_BADGE, "매도"
        elif item.get("market") == "KR":
            badge_bg, badge_text, badge_label = (219, 234, 254), BLUE_BADGE, "KR"
        else:
            badge_bg, badge_text, badge_label = (248, 219, 237), PINK_BADGE, "US"
        draw_rounded_rect(d, x+10, iy+4, x+40, iy+19, badge_bg, radius=3)
        bf = _font(bold=True, size=9)
        bb = bf.getbbox(badge_label)
        d.text((x+10+(30-(bb[2]-bb[0]))//2, iy+5), badge_label, font=bf, fill=badge_text)
        name_display = item.get("short", item.get("name", ""))[:14]
        d.text((x+46, iy+3), name_display,
               font=_font(medium=True, size=12), fill=GREY_TEXT if is_sold else DARK_SUB)
        text_right(d, x+w-12, iy+3, pct_str(item.get("ret")),
                   _font(bold=True, size=13), pct_color(item.get("ret")))

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
    img = Image.new("RGB", (CARD_W, CARD_H), BG)
    d   = ImageDraw.Draw(img)
    render_header(d, today, sheet_name, weather)
    content_y = 108
    content_h = CARD_H - content_y - 40
    ranking_w = 340
    render_ranking_panel(d, persons, 24, content_y, ranking_w, content_h)
    grid_x = 24 + ranking_w + 16
    grid_w = CARD_W - grid_x - 24
    render_portfolio_grid(d, persons, grid_x, content_y, grid_w, content_h)
    render_footer(d, CARD_H - 32)
    out = BASE_DIR / f"한탕_데일리_{today.strftime('%Y-%m-%d')}.png"
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
    caption = f"한탕 스터디 데일리 리포트 {today}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    for cid in TELEGRAM_CHAT_IDS:
        with open(image_path, "rb") as f:
            r = requests.post(url,
                data={"chat_id": cid, "caption": caption},
                files={"document": f}, timeout=30)
        print(f"  텔레그램 {'OK' if r.ok else 'FAIL'} (chat_id={cid})")

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
