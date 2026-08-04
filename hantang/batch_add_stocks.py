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

import os, re, sys, json, datetime

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


def _split_label(label):
    """'코히런트(COHR)' → ('COHR', '코히런트'). 표기 흔들림을 흡수한 비교용."""
    s = str(label or "").strip()
    m = re.search(r"\(([A-Za-z0-9]{1,7})\)\s*$", s)
    code = m.group(1).upper() if m else ""
    base = re.sub(r"[\s\(\)\[\]{}·\-_/\.]", "",
                  re.sub(r"\([^)]*\)\s*$", "", s)).upper()
    return code, base


def _same_stock(a, b):
    ac, ab = _split_label(a)
    bc, bb = _split_label(b)
    if ac and bc:
        return ac == bc
    return bool(ab) and bool(bb) and (ab == bb or ab in bb or bb in ab)


def find_existing(all_values, block, stock_name):
    """해당 블록의 활성(J)·실현(P)에 같은 종목이 이미 있으면 위치 목록 반환."""
    hits = []
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        row = all_values[idx]
        j = row[9] if len(row) > 9 else ""
        p = row[15] if len(row) > 15 else ""
        k = row[10] if len(row) > 10 else ""
        q = row[16] if len(row) > 16 else ""
        if j and _same_stock(j, stock_name):
            hits.append(f"활성 J{r}='{j}' (추천일 {k})")
        if p and _same_stock(p, stock_name):
            hits.append(f"실현 P{r}='{p}' (추천일 {q})")
    return hits


def describe_block(all_values, block):
    """드라이런: 해당 멤버 블록의 현재 활성/실현 상태를 그대로 출력."""
    print(f"    [현재 상태] {block['person']} (행 {block['row_start']}~{block['row_end']})")
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        row = all_values[idx]
        j = row[9] if len(row) > 9 else ""
        k = row[10] if len(row) > 10 else ""
        p = row[15] if len(row) > 15 else ""
        q = row[16] if len(row) > 16 else ""
        if j or p:
            print(f"      행{r:>3} | 활성 J='{j}' K='{k}' | 실현 P='{p}' Q='{q}'")


def add_stock(ws, all_values, person_name, stock_name, rec_date,
              dry_run=False, allow_dup=False):
    blocks = find_person_blocks(all_values)
    block = next(
        (b for b in blocks if b["person"] == person_name or person_name in b["person"]),
        None,
    )
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    if dry_run:
        describe_block(all_values, block)

    dups = find_existing(all_values, block, stock_name)
    if dups:
        msg = f"'{person_name}'에 '{stock_name}' 기존 기록 있음 → " + " / ".join(dups)
        if not allow_dup:
            return False, msg + "  ※ 중복 방지로 추가 안 함(재추천이면 allow_dup=true)"
        print(f"    [경고] {msg}  ※ allow_dup=true → 그대로 추가")

    empty_row = next(
        (r for r in range(block["row_start"], block["row_end"] + 1)
         if r - 1 < len(all_values)
         and not (all_values[r - 1][9] if len(all_values[r - 1]) > 9 else "")),
        None,
    )
    if not empty_row:
        return False, f"'{person_name}' 블록에 빈 행 없음"

    if dry_run:
        return True, (f"[드라이런] {person_name} / {stock_name} → J{empty_row}, "
                      f"K{empty_row}={rec_date} 에 기록 예정 (실제 쓰기 없음)")

    cell_j = gspread.Cell(row=empty_row, col=10, value=stock_name)
    cell_k = gspread.Cell(row=empty_row, col=11, value=str(rec_date))
    ws.update_cells([cell_j, cell_k], value_input_option="USER_ENTERED")
    return True, f"{person_name} / {stock_name} 추가 완료 (J{empty_row})"


def get_rec_date(date_str=None):
    """명시적 날짜가 있으면 그대로 사용, 없으면 이번 주 월요일"""
    if date_str:
        return datetime.date.fromisoformat(date_str)
    d = datetime.date.today()
    return d - datetime.timedelta(days=d.weekday())


def rename_stock(ws, all_values, person_name, old_name, new_name, dry_run=False):
    """활성(J열) 종목명 교정. 추천일(K)·수익률은 건드리지 않는다.
    실현(P열)은 확정 과거값이므로 대상에서 제외(절대규칙 4)."""
    blocks = find_person_blocks(all_values)
    block = next(
        (b for b in blocks if b["person"] == person_name or person_name in b["person"]),
        None,
    )
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    if dry_run:
        describe_block(all_values, block)

    targets = []
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        j = all_values[idx][9] if len(all_values[idx]) > 9 else ""
        if j and _same_stock(j, old_name):
            targets.append((r, j))
    if not targets:
        return False, f"'{person_name}' 활성 종목에서 '{old_name}'을 찾을 수 없음"
    if len(targets) > 1:
        return False, (f"'{old_name}' 후보 다수 → " +
                       ", ".join(f"J{r}='{v}'" for r, v in targets) + " (수동 처리 필요)")

    row, cur = targets[0]
    if dry_run:
        return True, f"[드라이런] J{row} '{cur}' → '{new_name}' 로 교정 예정 (실제 쓰기 없음)"

    ws.update_cells([gspread.Cell(row=row, col=10, value=new_name)],
                    value_input_option="USER_ENTERED")
    return True, f"J{row} '{cur}' → '{new_name}' 교정 완료"


def remove_stock(ws, all_values, person_name, stock_name, dry_run=False):
    """활성(J:N) 한 행을 비운다. 오기록 취소용.
    실현(P:U)은 확정 과거값이므로 대상 제외(절대규칙 4). 후보가 여럿이면 중단."""
    blocks = find_person_blocks(all_values)
    block = next(
        (b for b in blocks if b["person"] == person_name or person_name in b["person"]),
        None,
    )
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    if dry_run:
        describe_block(all_values, block)

    targets = []
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        row = all_values[idx]
        j = row[9] if len(row) > 9 else ""
        if j and _same_stock(j, stock_name):
            k = row[10] if len(row) > 10 else ""
            targets.append((r, j, k))
    if not targets:
        return False, f"'{person_name}' 활성 종목에서 '{stock_name}'을 찾을 수 없음"
    if len(targets) > 1:
        return False, (f"'{stock_name}' 후보 다수 → " +
                       ", ".join(f"J{r}='{v}'({k})" for r, v, k in targets) +
                       " (수동 처리 필요)")

    row, cur, k = targets[0]
    if dry_run:
        return True, f"[드라이런] J{row}:N{row} '{cur}' (추천일 {k}) 삭제 예정 (실제 쓰기 없음)"

    ws.batch_update([{"range": f"J{row}:N{row}", "values": [["", "", "", "", ""]]}],
                    value_input_option="USER_ENTERED")
    return True, f"J{row}:N{row} '{cur}' (추천일 {k}) 삭제 완료"


def remove_penalty(ws, all_values, person_name, date_str, dry_run=False):
    """잘못 부과된 '미추천(패널티)' 행(P:U)만 지운다.
    일반 실현 기록은 절대 대상이 아니다 — P열이 정확히 '미추천(패널티)'인 행만 본다."""
    blocks = find_person_blocks(all_values)
    block = next(
        (b for b in blocks if b["person"] == person_name or person_name in b["person"]),
        None,
    )
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    if dry_run:
        describe_block(all_values, block)

    targets = []
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        row = all_values[idx]
        p = (row[15] if len(row) > 15 else "").strip()
        q = (row[16] if len(row) > 16 else "").strip()
        if p == "미추천(패널티)" and q[:10] == date_str:
            targets.append(r)
    if not targets:
        return False, f"'{person_name}'에 {date_str}자 미추천 패널티 행이 없음"
    if len(targets) > 1:
        return False, f"{date_str}자 패널티 행 다수(P{targets}) — 수동 처리 필요"

    row = targets[0]
    if dry_run:
        return True, f"[드라이런] P{row}:U{row} {date_str}자 미추천 패널티 삭제 예정 (실제 쓰기 없음)"

    ws.batch_update([{"range": f"P{row}:U{row}",
                      "values": [["", "", "", "", "", ""]]}],
                    value_input_option="USER_ENTERED")
    return True, f"P{row}:U{row} {date_str}자 미추천 패널티 삭제 완료"


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y")


def run():
    stocks_raw = os.environ.get("STOCKS", "")
    rename_raw = os.environ.get("RENAME", "")
    remove_raw = os.environ.get("REMOVE", "")
    penalty_raw = os.environ.get("RM_PENALTY", "")
    rec_date = get_rec_date(os.environ.get("REC_DATE") or None)
    dry_run = _flag("DRY_RUN")
    allow_dup = _flag("ALLOW_DUP")

    if not (stocks_raw or rename_raw or remove_raw or penalty_raw):
        print("[오류] STOCKS / RENAME / REMOVE / RM_PENALTY 중 하나는 필요")
        sys.exit(1)

    pairs = [s.strip().split(":", 1) for s in stocks_raw.split(",") if ":" in s]
    renames = []
    for s in rename_raw.split(","):
        s = s.strip()
        if ":" in s and ">" in s:
            person, rest = s.split(":", 1)
            old, new = rest.split(">", 1)
            renames.append((person.strip(), old.strip(), new.strip()))
    print("=" * 60)
    print("모드: 드라이런(읽기전용 — 시트 변경 없음)" if dry_run else "모드: 실제 기록")
    print(f"추천일: {rec_date}")
    removes = [s.strip().split(":", 1) for s in remove_raw.split(",") if ":" in s]
    print(f"입력 종목: {len(pairs)}건 / 교정: {len(renames)}건 / 삭제: {len(removes)}건")
    print("=" * 60 + "\n")

    ws = get_worksheet()
    print(f"대상 시트: {ws.title}\n")
    all_vals = ws.get_all_values()

    fails = 0
    for person, old, new in renames:
        ok, msg = rename_stock(ws, all_vals, person, old, new, dry_run=dry_run)
        print(f"  {'✅' if ok else '❌'} {msg}\n")
        if not ok:
            fails += 1
        if ok and not dry_run:
            all_vals = ws.get_all_values()

    for s2 in penalty_raw.split(","):
        s2 = s2.strip()
        if ":" not in s2:
            continue
        person, d = s2.split(":", 1)
        ok, msg = remove_penalty(ws, all_vals, person.strip(), d.strip(), dry_run=dry_run)
        print(f"  {'✅' if ok else '❌'} {msg}\n")
        if not ok:
            fails += 1
        if ok and not dry_run:
            all_vals = ws.get_all_values()

    for person, stock in removes:
        ok, msg = remove_stock(ws, all_vals, person.strip(), stock.strip(), dry_run=dry_run)
        print(f"  {'✅' if ok else '❌'} {msg}\n")
        if not ok:
            fails += 1
        if ok and not dry_run:
            all_vals = ws.get_all_values()

    for person, stock in pairs:
        ok, msg = add_stock(ws, all_vals, person.strip(), stock.strip(), rec_date,
                            dry_run=dry_run, allow_dup=allow_dup)
        print(f"  {'✅' if ok else '❌'} {msg}\n")
        if not ok:
            fails += 1
        if ok and not dry_run:
            all_vals = ws.get_all_values()

    print(f"처리 완료 (실패 {fails}건)")
    if dry_run:
        print("※ 드라이런이므로 시트는 그대로입니다. 확인 후 dry_run=false로 재실행하세요.")


if __name__ == "__main__":
    run()
