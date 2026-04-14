"""
GitHub Actions용 텔레그램 종목 추천 감지 → Google Sheets 직접 기록
────────────────────────────────────────────────────────────────────
로컬 컴퓨터 불필요. pending_stocks.json 우회 없이 Sheets에 직접 씀.

환경변수:
  TELEGRAM_TOKEN       - 봇 토큰
  GSHEETS_CREDENTIALS  - 서비스 계정 JSON 문자열
  GSHEETS_ID           - 스프레드시트 ID
"""

import os, sys, re, json, datetime, subprocess

def _ensure(pkg, import_name=None):
    try:
        __import__(import_name or pkg.replace("-", "_"))
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)

_ensure("gspread")
_ensure("google-auth", "google.oauth2")
_ensure("requests")

import gspread
from google.oauth2.service_account import Credentials
import requests
from pathlib import Path

# ── 설정 ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
BASE_DIR       = Path(os.path.dirname(os.path.abspath(__file__)))
OFFSET_FILE    = BASE_DIR / ".telegram_offset"
SCOPES         = [
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

# ── Google Sheets 연결 ────────────────────────────────────────────────────
def get_worksheet() -> gspread.Worksheet:
    creds_json = os.environ.get("GSHEETS_CREDENTIALS", "")
    if creds_json:
        info = json.loads(creds_json)
    else:
        creds_file = BASE_DIR / "credentials.json"
        info = json.loads(creds_file.read_text())

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc    = gspread.authorize(creds)

    sheet_id = os.environ.get("GSHEETS_ID", "")
    if not sheet_id:
        sheet_id = (BASE_DIR / "gsheets_id.txt").read_text().strip()

    ss = gc.open_by_key(sheet_id)
    return ss.worksheets()[-1]   # 최신 분기 시트

# ── 블록 파싱 ────────────────────────────────────────────────────────────
def find_person_blocks(all_values: list) -> list:
    header_rows, sogyae_rows = [], []
    for i, row in enumerate(all_values):
        j = row[9]  if len(row) > 9  else ""
        p = row[15] if len(row) > 15 else ""
        if j == "종목명":             header_rows.append(i + 1)
        if "실현수익률 소계" in str(p): sogyae_rows.append(i + 1)

    blocks = []
    for h_row in header_rows:
        s_row  = next((r for r in sogyae_rows if r > h_row), None)
        if not s_row: continue
        i_val  = all_values[h_row][8] if len(all_values[h_row]) > 8 else ""
        person = str(i_val).strip().replace("\n", "") if i_val else ""
        blocks.append({"person": person,
                        "row_start": h_row + 1, "row_end": s_row - 1})
    return blocks

# ── 종목 추가 ─────────────────────────────────────────────────────────────
def add_stock(ws: gspread.Worksheet, all_values: list,
              person_name: str, stock_name: str, rec_date: datetime.date) -> tuple[bool, str]:

    # 종목코드 유효성 확인
    name = stock_name.strip()
    is_us = bool(re.search(r"\([A-Z]{1,5}\)\s*$", name))
    is_kr_etf = bool(re.search(r"\([A-Z0-9]{5,7}\)\s*$", name))
    is_kr = name in KOREAN_CODES or is_kr_etf
    if not is_us and not is_kr:
        return False, f"'{stock_name}' 종목 코드 미인식 (KOREAN_CODES 추가 필요)"

    blocks = find_person_blocks(all_values)
    block  = next((b for b in blocks
                   if b["person"] == person_name or person_name in b["person"]),
                  None)
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    # 중복 체크
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx < len(all_values):
            j = all_values[idx][9] if len(all_values[idx]) > 9 else ""
            if j == stock_name:
                return False, f"{person_name} / {stock_name} 이미 등록됨"

    # 빈 행 탐색
    empty_row = next(
        (r for r in range(block["row_start"], block["row_end"] + 1)
         if r - 1 < len(all_values) and
         not (all_values[r-1][9] if len(all_values[r-1]) > 9 else "")),
        None
    )
    if not empty_row:
        return False, f"'{person_name}' 블록에 빈 행 없음"

    # J, K열 기록 (L/M/N 은 내일 7시 update_gsheets.py가 채움)
    cell_j = ws.cell(empty_row, 10)
    cell_k = ws.cell(empty_row, 11)
    cell_j.value = stock_name
    cell_k.value = str(rec_date)
    ws.update_cells([cell_j, cell_k], value_input_option="USER_ENTERED")

    return True, f"{person_name} / {stock_name} 추가 완료 (기준가는 오늘 장 마감 후 자동 입력)"

# ── 텔레그램 유틸 ─────────────────────────────────────────────────────────
def load_offset() -> int:
    return int(OFFSET_FILE.read_text().strip()) if OFFSET_FILE.exists() else 0

def save_offset(offset: int):
    OFFSET_FILE.write_text(str(offset))

def tg_get(method, **params):
    return requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
        params=params, timeout=15).json()

def tg_send(chat_id, text):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                  data={"chat_id": chat_id, "text": text}, timeout=10)

def parse_recommendation(text: str):
    if "#종목추천" not in text or "#매수" not in text:
        return None, None
    clean  = re.sub(r"#\S+", "", text).strip()
    tokens = clean.split()
    return (tokens[0], tokens[1]) if len(tokens) >= 2 else (None, None)

def get_this_monday() -> datetime.date:
    today = datetime.date.today()
    return today - datetime.timedelta(days=today.weekday())

# ── git push (offset 파일 동기화) ────────────────────────────────────────
def git_push():
    try:
        subprocess.run(["git", "config", "user.email", "github-actions@github.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name",  "GitHub Actions"],
                       check=True, capture_output=True)
        subprocess.run(["git", "add", ".telegram_offset"],
                       check=True, capture_output=True)
        result = subprocess.run(["git", "commit", "-m",
                                 f"[bot] offset update {datetime.date.today()}"],
                                capture_output=True, text=True)
        if "nothing to commit" not in result.stdout:
            subprocess.run(["git", "push"], check=True, capture_output=True)
            print("✅ offset git push 완료")
    except Exception as e:
        print(f"[경고] git push 실패: {e}")

# ── 메인 ─────────────────────────────────────────────────────────────────
def run():
    if not TELEGRAM_TOKEN:
        print("[오류] TELEGRAM_TOKEN 미설정"); sys.exit(1)

    offset = load_offset()
    result = tg_get("getUpdates", offset=offset, timeout=0,
                    allowed_updates='["message"]')
    if not result.get("ok"):
        print(f"[오류] {result}"); sys.exit(1)

    updates = result.get("result", [])
    if not updates:
        print("새 메시지 없음"); return

    rec_date  = get_this_monday()
    ws        = get_worksheet()
    all_vals  = ws.get_all_values()
    chat_ids  = set()
    processed = []

    for upd in updates:
        update_id = upd["update_id"]
        msg  = upd.get("message", {})
        text = msg.get("text", "")
        cid  = msg.get("chat", {}).get("id")
        if cid: chat_ids.add(cid)

        person, stock = parse_recommendation(text)
        if person and stock:
            ok, msg_result = add_stock(ws, all_vals, person, stock, rec_date)
            status = "✅" if ok else "❌"
            print(f"[{'추가' if ok else '실패'}] {person} / {stock}: {msg_result}")
            if ok:
                # 추가 후 all_values 갱신 (중복 방지)
                all_vals = ws.get_all_values()
            processed.append((person, stock, ok, msg_result))

        save_offset(update_id + 1)

    if processed:
        # 그룹 알림
        lines = [f"📋 종목 추천 접수 ({rec_date} 기준)"]
        for person, stock, ok, msg in processed:
            lines.append(f"  {'✅' if ok else '❌'} {person} / {stock}")
            if not ok: lines.append(f"     └ {msg}")
        lines.append("⏳ 기준가는 오늘 장 마감 후 자동 입력")
        for cid in chat_ids:
            tg_send(cid, "\n".join(lines))

    git_push()
    print(f"\n총 {len(processed)}건 처리 완료")

if __name__ == "__main__":
    run()
