"""[일회성 검증] 미추천 패널티 최종 상태 + 자동화 재드라이런 — 읽기전용."""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_gsheets as U
def is_pen(p): return "미추천" in str(p) or "패널티" in str(p)
ss=U.get_spreadsheet()
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
vals=ws.get_all_values(); blocks=U.find_person_blocks(vals)
print("=== 현재 미추천 패널티 목록 ===")
for b in blocks:
    if not b["person"]: continue
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        p=row[15] if len(row)>15 else ''; q=row[16] if len(row)>16 else ''
        if p and is_pen(p):
            print(f"  {b['person']} {q} (R{r})")
# 재드라이런: 함수 로직으로 새로 넣을 게 있나
MEMBERS=[b["person"] for b in blocks if b["person"]]
today=U.today_kst()
round_days=set()
for b in blocks:
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        j=row[9] if len(row)>9 else ''; k=row[10] if len(row)>10 else ''
        p=row[15] if len(row)>15 else ''; q=row[16] if len(row)>16 else ''
        for d,ok in [(k,bool(j)),(q,bool(p) and not is_pen(p))]:
            if not ok: continue
            try:
                dd=datetime.date.fromisoformat(str(d).strip()[:10])
                if dd.weekday()==0 and dd<today: round_days.add(dd)
            except: pass
def block(n): return next((b for b in blocks if b["person"]==n or n in b["person"]),None)
def rec(n,D):
    b=block(n)
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        j=row[9] if len(row)>9 else ''; k=row[10] if len(row)>10 else ''
        p=row[15] if len(row)>15 else ''; q=row[16] if len(row)>16 else ''
        if j and str(D) in str(k): return True
        if p and str(D) in str(q) and not is_pen(p): return True
    return False
def pen(n,D):
    b=block(n)
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        p=row[15] if len(row)>15 else ''; q=row[16] if len(row)>16 else ''
        if p and is_pen(p) and str(D) in str(q): return True
    return False
print("\n=== 자동화 재드라이런(새로 넣을 패널티) ===")
tot=0
for D in sorted(round_days):
    new=[m for m in MEMBERS if not rec(m,D) and not pen(m,D)]
    if new: print(f"  [{D}] {new}"); tot+=len(new)
print(f"새로 넣을 패널티: {tot}건 (0이어야 정상)")
