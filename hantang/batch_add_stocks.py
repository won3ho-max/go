"""[일회성 감사] 2분기 실현 종목 매도가 vs 매도일 종가 대조 — 읽기전용."""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_gsheets as U   # parse_stock / fetch_price / get_spreadsheet 재사용

ss = U.get_spreadsheet()
TARGET = "한탕(26년 2분기)"
ws = ss.worksheet(TARGET)
print("감사 대상:", ws.title)
vals = ws.get_all_values()
blocks = U.find_person_blocks(vals)

bad, ok_n, skip = [], 0, []
for b in blocks:
    person = b["person"]
    if not person: continue
    for r in range(b["row_start"], b["row_end"] + 1):
        idx = r - 1
        if idx >= len(vals): continue
        row = vals[idx]
        p = row[15] if len(row) > 15 else ""
        rr = row[17] if len(row) > 17 else ""
        ss_ = row[18] if len(row) > 18 else ""
        tt = row[19] if len(row) > 19 else ""
        if not p or not rr or not tt: continue
        if "미추천" in p or "패널티" in p:
            continue
        try:
            sell_date = datetime.date.fromisoformat(str(rr).strip()[:10])
        except Exception:
            skip.append(f"{person}/{p}: 매도일 파싱불가 {rr!r}"); continue
        market, code = U.parse_stock(str(p).strip())
        if not market or not code:
            skip.append(f"{person}/{p}: 종목코드 미인식"); continue
        try:
            rec = float(str(tt).replace(",", ""))
        except Exception:
            skip.append(f"{person}/{p}: 매도가 파싱불가 {tt!r}"); continue
        correct = U.fetch_price(market, code, sell_date)
        if correct is None:
            skip.append(f"{person}/{p}: 종가 조회실패 ({sell_date})"); continue
        diff = abs(correct - rec) / max(rec, 1)
        if diff >= 0.005:
            bad.append((person, p, str(sell_date), rec, correct, diff*100, r))
        else:
            ok_n += 1

print(f"\n=== 결과: 정상 {ok_n}건 / 불일치 {len(bad)}건 / 확인불가 {len(skip)}건 ===")
for person, name, sd, rec, cor, d, r in sorted(bad, key=lambda x: -x[5]):
    print(f"  ❗R{r} {person} / {name} (매도일 {sd}): 기록 {rec:,.2f} vs 종가 {cor:,.2f}  차이 {d:.1f}%")
if skip:
    print("\n[확인불가]")
    for s_ in skip: print("  -", s_)
