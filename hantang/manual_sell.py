"""
수동 매도 처리 스크립트
─────────────────────
Google Sheets에서 특정 인물의 종목을 매도 처리합니다.
J-N열(활성) → P-U열(실현)로 이동

환경변수:
  GSHEETS_CREDENTIALS  - 서비스 계정 JSON (GitHub Secret)
  GSHEETS_ID           - 스프레드시트 ID
  SELL_PERSON           - 매도 대상자 이름
  SELL_STOCK            - 매도 종목명
  SELL_DATE             - 매도일 (YYYY-MM-DD)
  SELL_PRICE            - 매도가
"""

import os, sys, json, datetime, subprocess

def _ensure(pkg, import_name=None):
    try:
        __import__(import_name or pkg.replace("-", "_"))
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)

_ensure("gspread")
_ensure("google-auth", "google.oauth2")

import gspread
from google.oauth2.service_account import Credentials
from pathlib import Path

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def get_client() -> gspread.Client:
    creds_json = os.environ.get("GSHEETS_CREDENTIALS", "")
    if creds_json:
        info = json.loads(creds_json)
    else:
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


def find_person_blocks(all_values):
    header_rows, sogyae_rows = [], []
    for i, row in enumerate(all_values):
        j_val = row[9] if len(row) > 9 else ""
        p_val = row[15] if len(row) > 15 else ""
        if j_val == "종목명":
            header_rows.append(i + 1)
        if "실현수익률 소계" in str(p_val):
            sogyae_rows.append(i + 1)

    blocks = []
    for h_row in header_rows:
        s_row = next((r for r in sogyae_rows if r > h_row), None)
        if s_row is None:
            continue
        person_row = all_values[h_row]
        i_val = person_row[8] if len(person_row) > 8 else ""
        person = str(i_val).strip().replace("\n", "") if i_val else ""
        blocks.append({
            "person": person,
            "row_start": h_row + 1,
            "row_end": s_row - 1,
        })
    return blocks


def manual_sell(person_name: str, stock_name: str, sell_date: str, sell_price: float):
    ss = get_spreadsheet()
    ws = ss.worksheets()[-1]
    print(f"[시트] {ws.title}")

    all_values = ws.get_all_values()
    blocks = find_person_blocks(all_values)

    # 대상자 블록 찾기
    target_block = None
    for b in blocks:
        if person_name in b["person"]:
            target_block = b
            break

    if not target_block:
        print(f"[오류] '{person_name}' 블록을 찾을 수 없습니다.")
        print(f"  가능한 이름: {[b['person'] for b in blocks]}")
        sys.exit(1)

    print(f"[대상] {target_block['person']} (행 {target_block['row_start']}~{target_block['row_end']})")

    # 종목 행 찾기 (J열 = index 9)
    stock_row = None
    for row_1 in range(target_block["row_start"], target_block["row_end"] + 1):
        idx = row_1 - 1
        if idx >= len(all_values):
            continue
        j_val = all_values[idx][9] if len(all_values[idx]) > 9 else ""
        if stock_name in str(j_val):
            stock_row = row_1
            break

    if stock_row is None:
        print(f"[오류] '{stock_name}' 종목을 찾을 수 없습니다.")
        active_stocks = []
        for row_1 in range(target_block["row_start"], target_block["row_end"] + 1):
            idx = row_1 - 1
            if idx < len(all_values):
                j_val = all_values[idx][9] if len(all_values[idx]) > 9 else ""
                if j_val and j_val.strip():
                    active_stocks.append(j_val.strip())
        print(f"  활성 종목: {active_stocks}")
        sys.exit(1)

    idx = stock_row - 1
    row_data = all_values[idx]
    orig_name = row_data[9] if len(row_data) > 9 else ""
    rec_date = row_data[10] if len(row_data) > 10 else ""
    base_price = row_data[11] if len(row_data) > 11 else ""

    print(f"[종목] {orig_name} (행 {stock_row})")
    print(f"  추천일: {rec_date}, 기준가: {base_price}")

    # P열 빈 행 찾기
    p_row = None
    for r in range(target_block["row_start"], target_block["row_end"] + 1):
        ri = r - 1
        if ri >= len(all_values):
            break
        p_val = all_values[ri][15] if len(all_values[ri]) > 15 else ""
        if not p_val or not p_val.strip():
            p_row = r
            break

    if p_row is None:
        print(f"[오류] P열에 빈 행이 없습니다.")
        sys.exit(1)

    print(f"[매도] → P열 행 {p_row}에 기록")

    try:
        base_f = float(str(base_price).replace(",", ""))
    except:
        print(f"[오류] 기준가 파싱 실패: {base_price}")
        sys.exit(1)

    ret_pct = (sell_price - base_f) / base_f * 100

    # 배치 업데이트
    updates = []

    # P-U열에 매도 정보 기록
    updates.append({"range": f"P{p_row}", "values": [[orig_name]]})
    updates.append({"range": f"Q{p_row}", "values": [[rec_date]]})
    updates.append({"range": f"R{p_row}", "values": [[sell_date]]})
    updates.append({"range": f"S{p_row}", "values": [[base_f]]})
    updates.append({"range": f"T{p_row}", "values": [[sell_price]]})
    updates.append({"range": f"U{p_row}", "values": [[f"=(T{p_row}-S{p_row})/S{p_row}"]]})

    # J-N열 초기화
    updates.append({"range": f"J{stock_row}:N{stock_row}", "values": [["", "", "", "", ""]]})

    ws.batch_update(updates, value_input_option="USER_ENTERED")

    print(f"\n✅ 매도 완료!")
    print(f"  {target_block['person']} / {orig_name}")
    print(f"  추천일: {rec_date}")
    print(f"  매도일: {sell_date}")
    print(f"  기준가: {base_f:,.0f}")
    print(f"  매도가: {sell_price:,.0f}")
    print(f"  수익률: {ret_pct:+.2f}%")


if __name__ == "__main__":
    person = os.environ.get("SELL_PERSON", "")
    stock = os.environ.get("SELL_STOCK", "")
    sell_date = os.environ.get("SELL_DATE", "")
    sell_price_s = os.environ.get("SELL_PRICE", "")

    if not all([person, stock, sell_date, sell_price_s]):
        print("필수 환경변수: SELL_PERSON, SELL_STOCK, SELL_DATE, SELL_PRICE")
        sys.exit(1)

    sell_price = float(sell_price_s.replace(",", ""))
    manual_sell(person, stock, sell_date, sell_price)
