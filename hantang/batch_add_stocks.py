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

# 시세 로직은 update_gsheets 것을 그대로 쓴다(규칙을 두 벌 두지 않기 위해).
from update_gsheets import parse_stock, close_at, sheet_retry

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def get_worksheet():
    info = json.loads(os.environ["GSHEETS_CREDENTIALS"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ss = sheet_retry(lambda: gc.open_by_key(os.environ["GSHEETS_ID"]),
                     "스프레드시트 열기")
    sheets = [s for s in sheet_retry(ss.worksheets, "시트 목록 조회")
              if not s.title.startswith("_")]
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


_HEDGE_SUFFIX = re.compile(r"\(\s*(?:H|UH|합성|합성\s*H|환헤지|언헤지)\s*\)\s*$", re.I)


def _split_label(label):
    """'코히런트(COHR)' → ('COHR', '코히런트'). 표기 흔들림을 흡수한 비교용.
    '(H)' 등 환헤지 표기는 티커가 아니다(정방향/인버스가 같은 코드로 잡히던 문제)."""
    s = str(label or "").strip()
    m = None if _HEDGE_SUFFIX.search(s) else re.search(r"\(([A-Za-z0-9]{2,7})\)\s*$", s)
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


def refix_realized(ws, all_values, person_name, stock_name, dry_run=False):
    """실현 행의 기준가(S)·매도가(T)를 추천일/매도일 종가로 재계산한다.

    절대규칙 4는 실현 기록의 '자동' 덮어쓰기를 금지한다. 이 함수는 자동이 아니라
    명시적 지시로만 도는 교정 도구다. 종목 오분류로 엉뚱한 시세가 박힌 건을
    되돌리기 위해 만들었다(2026-08 KODEX WTI원유선물(H)가 US 티커 H로 잡힌 건)."""
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
        p = row[15] if len(row) > 15 else ""
        if p and p.strip() != "미추천(패널티)" and _same_stock(p, stock_name):
            targets.append((r, p, row[16] if len(row) > 16 else "",
                            row[17] if len(row) > 17 else "",
                            row[18] if len(row) > 18 else "",
                            row[19] if len(row) > 19 else ""))
    if not targets:
        return False, f"'{person_name}' 실현 기록에서 '{stock_name}'을 찾을 수 없음"
    if len(targets) > 1:
        return False, (f"'{stock_name}' 실현 후보 다수 → " +
                       ", ".join(f"P{r}(추천일 {q})" for r, _, q, _, _, _ in targets) +
                       " (수동 처리 필요)")

    row, name, q, rr, s_old, t_old = targets[0]
    market, code = parse_stock(name)
    if not market:
        return False, f"종목 해석 실패: {name}"
    try:
        rec_d = datetime.date.fromisoformat(str(q).strip()[:10])
        sell_d = datetime.date.fromisoformat(str(rr).strip()[:10])
    except Exception:
        return False, f"날짜 파싱 실패: 추천일 {q!r} 매도일 {rr!r}"

    s_new = close_at(market, code, rec_d)
    t_new = close_at(market, code, sell_d)
    if s_new is None or t_new is None:
        return False, f"시세 조회 실패 (기준가 {s_new}, 매도가 {t_new})"

    def pct(a, b):
        try:
            return f"{(float(b) - float(a)) / float(a) * 100:+.2f}%"
        except Exception:
            return "?"

    msg = (f"P{row} '{name}' ({market}/{code})\n"
           f"    기준가 {s_old} → {s_new:,}  |  매도가 {t_old} → {t_new:,}\n"
           f"    수익률 {pct(s_old, t_old)} → {pct(s_new, t_new)}")
    if dry_run:
        return True, "[드라이런] " + msg + "\n    ※ 시트는 그대로입니다."

    ws.batch_update([
        {"range": f"S{row}", "values": [[s_new]]},
        {"range": f"T{row}", "values": [[t_new]]},
        {"range": f"U{row}", "values": [[f"=(T{row}-S{row})/S{row}"]]},
    ], value_input_option="USER_ENTERED")
    return True, "교정 완료 " + msg


def add_adjustment(ws, all_values, person_name, date_str, label, pct, dry_run=False):
    """실현 섹션에 점수 조정 행을 넣는다(보너스·감점). S=100 기준, T=100+pct.

    미추천 패널티(-10%)와 같은 방식이며 라벨만 다르다. update_gsheets의
    is_adjustment()가 '보너스'·'패널티'가 든 행을 종목이 아닌 조정으로 보므로
    시세 조회나 추천 인정 대상에서 자동으로 빠진다."""
    blocks = find_person_blocks(all_values)
    block = next(
        (b for b in blocks if b["person"] == person_name or person_name in b["person"]),
        None,
    )
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    if dry_run:
        describe_block(all_values, block)

    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        row = all_values[idx]
        pv = (row[15] if len(row) > 15 else "").strip()
        qv = (row[16] if len(row) > 16 else "").strip()[:10]
        if pv == label and qv == date_str:
            return False, f"'{person_name}'에 {date_str}자 '{label}'가 이미 있음(P{r}) — 중복 방지"

    pr = None
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        pv = all_values[idx][15] if len(all_values[idx]) > 15 else ""
        if not pv or not str(pv).strip():
            pr = r
            break
    if pr is None:
        return False, f"'{person_name}' 실현 섹션에 빈 행 없음"

    t_val = round(100 + float(pct), 4)
    desc = f"P{pr} '{label}' {date_str} {float(pct):+g}% (S=100, T={t_val:g})"
    if dry_run:
        return True, f"[드라이런] {desc} 기록 예정 (실제 쓰기 없음)"

    ws.batch_update([
        {"range": f"P{pr}", "values": [[label]]},
        {"range": f"Q{pr}", "values": [[date_str]]},
        {"range": f"R{pr}", "values": [[date_str]]},
        {"range": f"S{pr}", "values": [[100]]},
        {"range": f"T{pr}", "values": [[t_val]]},
        {"range": f"U{pr}", "values": [[f"=(T{pr}-S{pr})/S{pr}"]]},
    ], value_input_option="USER_ENTERED")
    return True, "기록 완료 " + desc


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y")


def run():
    stocks_raw = os.environ.get("STOCKS", "")
    rename_raw = os.environ.get("RENAME", "")
    remove_raw = os.environ.get("REMOVE", "")
    penalty_raw = os.environ.get("RM_PENALTY", "")
    refix_raw = os.environ.get("REFIX", "")
    bonus_raw = os.environ.get("BONUS", "")
    rec_date = get_rec_date(os.environ.get("REC_DATE") or None)
    dry_run = _flag("DRY_RUN")
    allow_dup = _flag("ALLOW_DUP")

    if not (stocks_raw or rename_raw or remove_raw or penalty_raw or refix_raw or bonus_raw):
        print("[오류] STOCKS / RENAME / REMOVE / RM_PENALTY / REFIX / BONUS 중 하나는 필요")
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

    for s4 in bonus_raw.split(","):
        s4 = s4.strip()
        parts = [x.strip() for x in s4.split(":")]
        if len(parts) != 4:
            if s4:
                print(f"  ❌ BONUS 형식 오류: {s4!r} — '이름:YYYY-MM-DD:라벨:퍼센트' 이어야 함\n")
                fails += 1
            continue
        person, d, label, pct = parts
        try:
            float(pct)
        except ValueError:
            print(f"  ❌ BONUS 퍼센트 파싱 실패: {pct!r}\n"); fails += 1; continue
        ok, msg = add_adjustment(ws, all_vals, person, d, label, pct, dry_run=dry_run)
        print(f"  {'✅' if ok else '❌'} {msg}\n")
        if not ok:
            fails += 1
        if ok and not dry_run:
            all_vals = ws.get_all_values()

    for s3 in refix_raw.split(","):
        s3 = s3.strip()
        if ":" not in s3:
            continue
        person, stk = s3.split(":", 1)
        ok, msg = refix_realized(ws, all_vals, person.strip(), stk.strip(), dry_run=dry_run)
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
