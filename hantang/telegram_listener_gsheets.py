"""
GitHub Actions용 텔레그램 종목 추천 감지 → Google Sheets 직접 기록
────────────────────────────────────────────────────────────────────
로컬 컴퓨터 불필요. pending_stocks.json 우회 없이 Sheets에 직접 씀.
offset도 Google Sheets '_config' 시트에 저장 → git push 불필요.

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
SCOPES         = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# ── Google Sheets 연결 ────────────────────────────────────────────────────
def open_spreadsheet() -> gspread.Spreadsheet:
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

    return gc.open_by_key(sheet_id)

def get_worksheet(ss: gspread.Spreadsheet) -> gspread.Worksheet:
    sheets = [s for s in ss.worksheets() if not s.title.startswith("_")]
    return sheets[-1]   # 최신 분기 시트 (_config 등 제외)

# ── offset을 Google Sheets '_config' 시트에 저장 ─────────────────────────
def _get_config_sheet(ss: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        return ss.worksheet("_config")
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title="_config", rows=10, cols=2)
        ws.update_cell(1, 1, "telegram_offset")
        ws.update_cell(1, 2, "0")
        return ws

def load_offset(ss: gspread.Spreadsheet) -> int:
    cfg = _get_config_sheet(ss)
    val = cfg.cell(1, 2).value
    return int(val) if val and val.strip().isdigit() else 0

def save_offset(ss: gspread.Spreadsheet, offset: int):
    cfg = _get_config_sheet(ss)
    cfg.update_cell(1, 2, str(offset))

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

    blocks = find_person_blocks(all_values)
    block  = next((b for b in blocks
                   if b["person"] == person_name or person_name in b["person"]),
                  None)
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    # 중복 체크 없음 — 재추천 시 새 행으로 추가

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
def tg_get(method, **params):
    return requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
        params=params, timeout=15).json()

def tg_send(chat_id, text):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                  data={"chat_id": chat_id, "text": text}, timeout=10)

def parse_recommendation(text: str):
    if "#종목추천" not in text and "#매수" not in text:
        return None, None
    clean  = re.sub(r"#\S+", "", text).strip()
    tokens = clean.split()
    return (tokens[0], tokens[1]) if len(tokens) >= 2 else (None, None)

def get_this_monday() -> datetime.date:
    today = datetime.date.today()
    return today - datetime.timedelta(days=today.weekday())

# ── 메인 ─────────────────────────────────────────────────────────────────
def run():
    if not TELEGRAM_TOKEN:
        print("[오류] TELEGRAM_TOKEN 미설정"); sys.exit(1)

    ss     = open_spreadsheet()
    offset = load_offset(ss)
    print(f"현재 offset: {offset}")

    result = tg_get("getUpdates", offset=offset, timeout=0,
                    allowed_updates='["message"]')
    if not result.get("ok"):
        print(f"[오류] {result}"); sys.exit(1)

    updates = result.get("result", [])
    if not updates:
        print("새 메시지 없음"); return

    rec_date  = get_this_monday()
    ws        = get_worksheet(ss)
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
            print(f"[{'추가' if ok else '실패'}] {person} / {stock}: {msg_result}")
            if ok:
                all_vals = ws.get_all_values()
            processed.append((person, stock, ok, msg_result))

        save_offset(ss, update_id + 1)

    if processed:
        lines = [f"📋 종목 추천 접수 ({rec_date} 기준)"]
        for person, stock, ok, msg in processed:
            lines.append(f"  {'✅' if ok else '❌'} {person} / {stock}")
            if not ok: lines.append(f"     └ {msg}")
        lines.append("⏳ 기준가는 오늘 장 마감 후 자동 입력")
        for cid in chat_ids:
            tg_send(cid, "\n".join(lines))

    print(f"\n총 {len(processed)}건 처리 완료")

if __name__ == "__main__":
    run()
