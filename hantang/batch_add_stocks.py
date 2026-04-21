"""
GitHub Actions용 종목 일괄 추가 스크립트
─────────────────────────────────────────
텔레그램 없이 직접 Google Sheets에 종목을 추가한다.

환경변수:
  GSHEETS_CREDENTIALS  - 서비스 계정 JSON 문자열
  GSHEETS_ID           - 스프레드시트 ID
  STOCKS               - "이름:종목명,이름:종목명,..." 형식
  REC_DATE             - 추천일 (YYYY-MM-DD), 미입력 시 이번 주 월요일
"""

import os, sys, json, datetime

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def get_worksheet():
    info = json.loads(os.environ["GSHEETS_CREDENTIALS"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(os.environ["GSHEETS_ID"])
    sheets = [s for s in ss.worksheets() if not s.title.startswith("_")]
    return sheets[-1]


def find_person_blocks(all_values):
    header_rows, sogyae_rows = [], []
    for i, row in enumerate(all_values):
        j = row[9] if len(row) > 9 else ""
        p = row[15] if len(row) > 15 else ""
        if j == "종목명":
            header_rows.append(i + 1)
        if "실현수익률 소계" in str(p):
            sogyae_rows.append(i + 1)

    blocks = []
    for h_row in header_rows:
        s_row = next((r for r in sogyae_rows if r > h_row), None)
        if not s_row:
            continue
        i_val = all_values[h_row][8] if len(all_values[h_row]) > 8 else ""
        person = str(i_val).strip().replace("\n", "") if i_val else ""
        blocks.append({"person": person, "row_start": h_row + 1, "row_end": s_row - 1})
    return blocks


def add_stock(ws, all_values, person_name, stock_name, rec_date):
    blocks = find_person_blocks(all_values)
    block = next(
        (b for b in blocks if b["person"] == person_name or person_name in b["person"]),
        None,
    )
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    # 중복 체크 없음 — 재추천 시 새 행으로 추가

    empty_row = next(
        (r for r in range(block["row_start"], block["row_end"] + 1)
         if r - 1 < len(all_values)
         and not (all_values[r - 1][9] if len(all_values[r - 1]) > 9 else "")),
        None,
    )
    if not empty_row:
        return False, f"'{person_name}' 블록에 빈 행 없음"

    cell_j = gspread.Cell(row=empty_row, col=10, value=stock_name)
    cell_k = gspread.Cell(row=empty_row, col=11, value=str(rec_date))
    ws.update_cells([cell_j, cell_k], value_input_option="USER_ENTERED")
    return True, f"{person_name} / {stock_name} 추가 완료"


def get_monday(date_str=None):
    if date_str:
        d = datetime.date.fromisoformat(date_str)
    else:
        d = datetime.date.today()
    return d - datetime.timedelta(days=d.weekday())


def run():
    stocks_raw = os.environ.get("STOCKS", "")
    rec_date = get_monday(os.environ.get("REC_DATE") or None)

    if not stocks_raw:
        print("[오류] STOCKS 환경변수 미설정")
        sys.exit(1)

    pairs = [s.strip().split(":") for s in stocks_raw.split(",") if ":" in s]
    print(f"추천일: {rec_date}")
    print(f"입력 종목: {len(pairs)}건\n")

    ws = get_worksheet()
    all_vals = ws.get_all_values()

    for person, stock in pairs:
        ok, msg = add_stock(ws, all_vals, person.strip(), stock.strip(), rec_date)
        print(f"  {'✅' if ok else '❌'} {msg}")
        if ok:
            all_vals = ws.get_all_values()

    print(f"\n처리 완료")


if __name__ == "__main__":
    run()
