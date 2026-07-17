"""[일회성 진단] 2분기·3분기 활성 종목의 자동매도 예정일 점검 — 읽기전용."""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_gsheets as U

today = U.today_kst()
print("오늘(KST):", today)
ss = U.get_spreadsheet()
for title in ["한탕(26년 2분기)", "한탕(26년 3분기)"]:
    ws = ss.worksheet(title)
    vals = ws.get_all_values()
    print(f"\n===== {title} 활성 종목 =====")
    for b in U.find_person_blocks(vals):
        person = b["person"]
        if not person: continue
        for r in range(b["row_start"], b["row_end"]+1):
            idx=r-1
            if idx>=len(vals): continue
            row=vals[idx]
            j=row[9] if len(row)>9 else ''
            k=row[10] if len(row)>10 else ''
            if not j: continue
            try:
                rec=datetime.date.fromisoformat(str(k).strip()[:10])
            except Exception:
                print(f"  {person}/{j}: 추천일 파싱불가 {k!r}"); continue
            market, code = U.parse_stock(str(j).strip())
            if not market:
                print(f"  {person}/{j} (추천 {rec}): 코드 미인식 → 자동매도 판정 불가 ❗"); continue
            sd = U.calc_sell_date(rec, market)
            overdue = "❗지났음(매도됐어야 함)" if sd < today else ("오늘 도래(내일 처리)" if sd==today else "")
            print(f"  {person}/{j} (추천 {rec}) → 매도예정 {sd}  {overdue}")
