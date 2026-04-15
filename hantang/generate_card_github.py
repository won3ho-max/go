"""
GitHub Actions용 한탕 스터디 데일리 카드뉴스 생성 + 텔레그램 전송
──────────────────────────────────────────────────────────────────
- portfolio.json 읽기 (로컬 스크립트가 매일 업데이트 후 push)
- Yahoo Finance로 현재가 갱신 (Naver IP 차단 우회)
- Pillow로 카드뉴스 이미지 생성
- 텔레그램으로 파일 전송

환경변수:
  TELEGRAM_TOKEN    - 봇 토큰
  TELEGRAM_CHAT_ID  - 전송할 채팅방 ID (쉼표로 여러 개 가능)
"""

import os, sys, json, re, datetime, urllib.request, subprocess
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
_ensure("exchange_calendars")
_ensure("python-dateutil", "dateutil")

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

CARD_W = 480
BG          = (15, 15, 20)
BG_CARD     = (18, 18, 28)
BG_HEADER   = (26, 26, 46)
RED         = (233, 69, 96)
GREEN       = (0, 212, 170)
GOLD        = (244, 196, 48)
SILVER      = (192, 192, 192)
BRONZE      = (205, 127, 50)
GREY_DIM    = (40, 40, 60)
GREY_TEXT   = (80, 90, 110)
GREY_BORDER = (30, 30, 46)
WHITE       = (255, 255, 255)
WHITE_DIM   = (180, 185, 200)
BLUE_BADGE  = (26, 58, 92)
BLUE_TEXT   = (77, 166, 255)
PINK_BADGE  = (58, 26, 44)
PINK_TEXT   = (255, 107, 181)

# ── 폰트 ────────────────────────────────────────────────────────────────
def _download_fonts():
    FONTS_DIR.mkdir(exist_ok=True)
    files = {
        "NotoSansKR-Regular.otf":
            "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/KR/NotoSansKR-Regular.otf",
        "NotoSansKR-Bold.otf":
            "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/KR/NotoSansKR-Bold.otf",
    }
    for fname, url in files.items():
        dst = FONTS_DIR / fname
        if not dst.exists():
            urllib.request.urlretrieve(url, dst)

_download_fonts()

def _font(bold=False, size=16):
    fname = "NotoSansKR-Bold.otf" if bold else "NotoSansKR-Regular.otf"
    try:    return ImageFont.truetype(str(FONTS_DIR / fname), size)
    except: return ImageFont.load_default()

# ── 주가 조회 (Yahoo Finance - 글로벌 접근 가능) ─────────────────────────
# 한국 종목: code.KS (코스피) 또는 code.KQ (코스닥)
KOSDAQ_CODES = {
    "247540", "356860", "462350", "031330",  # 에코프로비엠, 티엘비, 이노스페이스, 에스에이엠티
    # 066970(엘앤에프)는 야후파이낸스에서 .KS로 등록되어 있어 제외
}

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

# ── 포트폴리오 로드 + 가격 갱신 ────────────────────────────────────────
def load_portfolio(skip_price_refresh=False):
    """
    포트폴리오 로드.
    skip_price_refresh=True면 portfolio.json의 가격을 그대로 사용 (update_gsheets에서 호출 시).
    단독 실행 시에는 False로 Yahoo Finance 갱신.
    """
    path = BASE_DIR / "portfolio.json"
    if not path.exists():
        raise FileNotFoundError("portfolio.json 없음 - 로컬에서 먼저 실행해주세요")

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
                "name":     s.get("name", ""),
                "short":    shorten_name(s.get("name", "")),
                "market":   market,
                "rec_date": s.get("rec_date", ""),
                "base":     base_price or 0,
                "current":  cur_price or 0,
                "ret":      ret,
                "sell_date": s.get("sell_date", ""),
            })

        # 실현 종목 추가
        realized = []
        for r in p.get("realized", []):
            ret_pct = r.get("return_pct")
            ret = ret_pct / 100 if ret_pct is not None else None
            realized.append({
                "name":      r.get("name", ""),
                "short":     shorten_name(r.get("name", "")),
                "status":    "sold",
                "rec_date":  r.get("rec_date", ""),
                "sell_date": r.get("sell_date", ""),
                "base":      r.get("base_price") or 0,
                "sell_price": r.get("sell_price") or 0,
                "ret":       ret,
            })

        # 총 수익률: 활성 + 실현 모두 포함
        active_rets = [s["ret"] for s in stocks if s["ret"] is not None]
        realized_rets = [r["ret"] for r in realized if r["ret"] is not None]
        all_rets = active_rets + realized_rets
        total_ret = sum(all_rets) if all_rets else 0

        if stocks or realized:
            persons.append({
                "person":    p["name"],
                "stocks":    stocks,
                "realized":  realized,
                "total_ret": total_ret,
            })

    persons.sort(key=lambda x: x["total_ret"], reverse=True)
    return sheet_name, persons

def shorten_name(name: str) -> str:
    name = str(name).strip()
    replacements = {
        "RISE 삼성전자SK하이닉스채권혼합50 (0162Z0)": "RISE 삼성전자SK하닉스 혼합",
        "TIGER 미국채10년선물 (305080)": "TIGER 미국채10년선물",
        "TIGER 미국테크TOP10 INDXX (381170)": "TIGER 미국테크TOP10",
        "TIGER 원유선물인버스(H) (217770)": "TIGER 원유선물인버스",
    }
    return replacements.get(name, name)

# ── 드로잉 헬퍼 ─────────────────────────────────────────────────────────
def draw_rect(d, x0, y0, x1, y1, fill, radius=0):
    if radius == 0:
        d.rectangle([x0, y0, x1, y1], fill=fill)
    else:
        d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)

def draw_text_right(d, x, y, text, font, fill):
    bbox = font.getbbox(text)
    d.text((x - (bbox[2]-bbox[0]), y), text, font=font, fill=fill)

def pct_str(v):
    if v is None: return "—"
    return f"+{v*100:.2f}%" if v >= 0 else f"{v*100:.2f}%"

def price_str(v, market):
    if not v: return "—"
    return f"${v:,.2f}" if market == "US" else f"{int(v):,}원"

# ── 렌더러 (generate_card.py와 동일) ────────────────────────────────────
def render_header(d, y, today, sheet_name):
    H = 130
    draw_rect(d, 0, y, CARD_W, y+H, BG_HEADER)
    d.rectangle([0, y+H-3, CARD_W, y+H], fill=RED)
    tag_font = _font(bold=True, size=10)
    tag_w = tag_font.getbbox("DAILY REPORT")[2] + 20
    draw_rect(d, 24, y+18, 24+tag_w, y+34, RED, radius=2)
    d.text((24+10, y+20), "DAILY REPORT", font=tag_font, fill=WHITE)
    d.text((24, y+44), "한탕 스터디", font=_font(bold=True, size=28), fill=WHITE)
    d.text((24, y+82), sheet_name, font=_font(bold=True, size=11), fill=RED)
    d.text((24, y+100), f"직전영업일 종가 기준  ·  {today.strftime('%Y.%m.%d')}",
           font=_font(size=10), fill=GREY_TEXT)
    return y + H

def render_section_title(d, y, label, subtitle):
    H = 40
    draw_rect(d, 0, y, CARD_W, y+H, BG_CARD)
    d.rectangle([0, y+8, 3, y+H-8], fill=RED)
    d.text((24, y+6),  label,    font=_font(bold=True, size=10), fill=RED)
    d.text((24, y+20), subtitle, font=_font(bold=True, size=15), fill=WHITE)
    return y + H

def render_ranking(d, y, persons):
    RANK_COLORS = [GOLD, SILVER, BRONZE]
    max_abs = max(abs(p["total_ret"]) for p in persons) if persons else 1
    ROW_H, PAD = 36, 24
    draw_rect(d, 0, y, CARD_W, y + len(persons)*ROW_H + 10, BG_CARD)
    for i, p in enumerate(persons):
        ry = y + 5 + i * ROW_H
        if i > 0:
            d.line([PAD, ry, CARD_W-PAD, ry], fill=GREY_BORDER, width=1)
        # 순위 원
        cx, cy = PAD+12, ry+ROW_H//2
        rc = RANK_COLORS[i] if i < 3 else GREY_DIM
        fc = (0,0,0) if i < 3 else GREY_TEXT
        d.ellipse([cx-11, cy-11, cx+11, cy+11], fill=rc)
        rn = str(i+1)
        rf = _font(bold=True, size=11)
        rb = rf.getbbox(rn)
        d.text((cx-(rb[2]-rb[0])//2, cy-(rb[3]-rb[1])//2-1), rn, font=rf, fill=fc)
        # 이름
        d.text((PAD+30, ry+8), p["person"], font=_font(bold=True, size=14), fill=WHITE)
        # 수익률 + 바
        ret = p["total_ret"]
        ret_color = GREEN if ret >= 0 else RED
        draw_text_right(d, CARD_W-PAD, ry+8, pct_str(ret), _font(bold=True, size=14), ret_color)
    return y + len(persons)*ROW_H + 10

def render_person_detail(d, y, person_data, rank):
    p = person_data
    PAD = 24
    rank_color = [GOLD, SILVER, BRONZE][rank] if rank < 3 else GREY_TEXT
    HDR_H = 34
    draw_rect(d, 0, y, CARD_W, y+HDR_H, BG_CARD)
    d.line([0, y+HDR_H-1, CARD_W, y+HDR_H-1], fill=GREY_BORDER, width=1)
    rank_str = f"{rank+1}위"
    d.text((PAD, y+8), rank_str, font=_font(bold=True, size=10), fill=rank_color)
    rw = _font(bold=True, size=10).getbbox(rank_str)[2] + 8
    d.text((PAD+rw, y+6), p["person"], font=_font(bold=True, size=15), fill=WHITE)
    total_color = GREEN if p["total_ret"] >= 0 else RED
    draw_text_right(d, CARD_W-PAD, y+7, pct_str(p["total_ret"]), _font(bold=True, size=15), total_color)
    y += HDR_H
    for si, s in enumerate(p["stocks"]):
        ROW_H = 44
        draw_rect(d, 0, y, CARD_W, y+ROW_H, BG_CARD)
        if si > 0:
            d.line([PAD, y, CARD_W-PAD, y], fill=GREY_BORDER, width=1)
        badge_bg   = BLUE_BADGE if s["market"] == "KR" else PINK_BADGE
        badge_text = BLUE_TEXT  if s["market"] == "KR" else PINK_TEXT
        draw_rect(d, PAD, y+8, PAD+30, y+20, badge_bg, radius=2)
        bfont = _font(bold=True, size=8)
        bb = bfont.getbbox(s["market"])
        d.text((PAD+(30-(bb[2]-bb[0]))//2, y+9), s["market"], font=bfont, fill=badge_text)
        d.text((PAD+36, y+5), s["short"][:20], font=_font(bold=True, size=12), fill=WHITE)
        meta = f"{s['rec_date'][5:]}  ·  {price_str(s['base'], s['market'])}"
        d.text((PAD+36, y+23), meta, font=_font(size=9), fill=GREY_TEXT)
        ret_color = GREEN if (s["ret"] or 0) >= 0 else RED
        draw_text_right(d, CARD_W-PAD, y+5, price_str(s["current"], s["market"]), _font(size=11), WHITE_DIM)
        draw_text_right(d, CARD_W-PAD, y+23, pct_str(s["ret"]), _font(bold=True, size=13), ret_color)
        y += ROW_H

    # ── 실현 종목 ─────────────────────────────────────
    SOLD_BG = (58, 36, 26)
    SOLD_TEXT = (255, 160, 80)
    for ri, r in enumerate(p.get("realized", [])):
        ROW_H = 44
        draw_rect(d, 0, y, CARD_W, y+ROW_H, BG_CARD)
        d.line([PAD, y, CARD_W-PAD, y], fill=GREY_BORDER, width=1)
        draw_rect(d, PAD, y+8, PAD+30, y+20, SOLD_BG, radius=2)
        bfont = _font(bold=True, size=8)
        bb = bfont.getbbox("매도")
        d.text((PAD+(30-(bb[2]-bb[0]))//2, y+9), "매도", font=bfont, fill=SOLD_TEXT)
        d.text((PAD+36, y+5), shorten_name(r["name"])[:20], font=_font(bold=True, size=12), fill=(120, 120, 140))
        sell_dt = r.get("sell_date", "")
        meta = f"매도 {sell_dt[5:] if sell_dt else ''}  ·  {price_str(r['base'], 'KR')}"
        d.text((PAD+36, y+23), meta, font=_font(size=9), fill=GREY_TEXT)
        ret_color = GREEN if (r["ret"] or 0) >= 0 else RED
        draw_text_right(d, CARD_W-PAD, y+5, price_str(r.get("sell_price", 0), "KR"), _font(size=11), (120, 120, 140))
        draw_text_right(d, CARD_W-PAD, y+23, pct_str(r["ret"]), _font(bold=True, size=13), ret_color)
        y += ROW_H

    return y

def render_upcoming(d, y, persons):
    from dateutil.relativedelta import relativedelta
    import exchange_calendars as xcals
    import pandas as pd
    _cals = {}
    def prev_td(dt, market):
        k = "XKRX" if market=="KR" else "XNYS"
        if k not in _cals: _cals[k] = xcals.get_calendar(k)
        cal = _cals[k]; ts = pd.Timestamp(dt)
        sess = cal.sessions_in_range(ts - pd.Timedelta(days=14), ts)
        return sess[-1].date() if len(sess) > 0 else dt
    sell_map = {}
    for p in persons:
        for s in p["stocks"]:
            if not s["rec_date"]: continue
            try:
                rd = datetime.date.fromisoformat(s["rec_date"])
                sell_dt = prev_td(rd + relativedelta(months=1), s["market"])
                sell_map.setdefault(str(sell_dt), []).append(s["short"][:10])
            except: continue
    if not sell_map: return y
    H_TITLE = 52
    draw_rect(d, 0, y, CARD_W, y+H_TITLE, BG_CARD)
    d.rectangle([0, y+10, 4, y+H_TITLE-10], fill=RED)
    d.text((36, y+8),  "UPCOMING",    font=_font(bold=True, size=11), fill=RED)
    d.text((36, y+24), "매도 예정 일정", font=_font(bold=True, size=18), fill=WHITE)
    y += H_TITLE
    for dt_str in sorted(sell_map.keys()):
        ROW_H = 52
        draw_rect(d, 0, y, CARD_W, y+ROW_H, BG_CARD)
        d.line([36, y, CARD_W-36, y], fill=GREY_BORDER, width=1)
        dt = datetime.date.fromisoformat(dt_str)
        d.text((36, y+8), dt.strftime("%Y.%m.%d"), font=_font(bold=True, size=15), fill=GOLD)
        day_ko = ["월","화","수","목","금","토","일"][dt.weekday()]
        d.text((36+130, y+11), f"({day_ko})", font=_font(size=11), fill=GREY_TEXT)
        items = sell_map[dt_str]
        stocks_str = "  ·  ".join(items[:4]) + (f"  외 {len(items)-4}건" if len(items) > 4 else "")
        d.text((36, y+30), stocks_str, font=_font(size=11), fill=WHITE_DIM)
        y += ROW_H
    d.line([36, y, CARD_W-36, y], fill=GREY_BORDER, width=1)
    d.text((36, y+8), "휴장일 시 시장별 직전 영업일 자동 조정", font=_font(size=10), fill=GREY_DIM)
    return y + 28

def render_footer(d, y):
    H = 44
    draw_rect(d, 0, y, CARD_W, y+H, (12, 12, 20))
    d.rectangle([0, y, CARD_W, y+2], fill=RED)
    d.text((CARD_W//2, y+12), "한탕 스터디  ·  매일 오전 7:00 자동 업데이트",
           font=_font(size=9), fill=(50,50,70), anchor="mm")
    d.text((CARD_W//2, y+28), "KRX · NYSE  |  Yahoo Finance",
           font=_font(size=9), fill=(40,40,56), anchor="mm")
    return y + H

# ── 이미지 생성 ──────────────────────────────────────────────────────────
def generate_image(sheet_name: str, persons: list, today: datetime.date) -> str:
    total_stock_rows = sum(len(p["stocks"]) + len(p.get("realized", [])) for p in persons)
    estimated_h = (
        200 + 52 + len(persons)*64 + 16
        + 52 + len(persons)*44 + total_stock_rows*56
        + 52 + 6*52 + 28 + 72 + 100
    )
    img = Image.new("RGB", (CARD_W, estimated_h), BG)
    d   = ImageDraw.Draw(img)
    y = 0
    y = render_header(d, y, today, sheet_name);         y += 3
    y = render_section_title(d, y, "RANKING", "수익률 순위"); y += 3
    y = render_ranking(d, y, persons);                  y += 16
    y = render_section_title(d, y, "PORTFOLIO", "개인별 보유 종목"); y += 3
    for rank, p in enumerate(persons):
        y = render_person_detail(d, y, p, rank);        y += 3
    y += 10
    y = render_footer(d, y)
    img = img.crop((0, 0, CARD_W, y))
    out = BASE_DIR / f"한탕_데일리_{today.strftime('%Y-%m-%d')}.png"
    img.save(str(out), "PNG", optimize=True)
    print(f"✅ 카드뉴스 저장: {out.name}")
    return str(out)

# ── 텔레그램 전송 ────────────────────────────────────────────────────────
def send_telegram(image_path: str, today: datetime.date):
    if not TELEGRAM_TOKEN:
        print("[텔레그램] TELEGRAM_TOKEN 미설정")
        return
    if not TELEGRAM_CHAT_IDS:
        print("[텔레그램] TELEGRAM_CHAT_ID 미설정")
        return
    caption = f"📊 한탕 스터디 데일리 리포트 {today}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    for cid in TELEGRAM_CHAT_IDS:
        with open(image_path, "rb") as f:
            r = requests.post(url,
                data={"chat_id": cid, "caption": caption},
                files={"document": f}, timeout=30)
        print(f"  텔레그램 {'✅' if r.ok else '❌'} (chat_id={cid})")

# ── 메인 ────────────────────────────────────────────────────────────────
def today_kst():
    """KST(UTC+9) 기준 오늘 날짜"""
    from datetime import timezone, timedelta
    return datetime.datetime.now(timezone(timedelta(hours=9))).date()

if __name__ == "__main__":
    today = today_kst()
    print(f"=== 한탕 데일리 리포트 생성 ({today}) ===")
    sheet_name, persons = load_portfolio()
    print(f"  포트폴리오 로드: {len(persons)}명")
    image_path = generate_image(sheet_name, persons, today)
    send_telegram(image_path, today)
