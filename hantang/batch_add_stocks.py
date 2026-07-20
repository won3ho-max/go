"""[일회성 드라이런] 잘못 들어간 미추천 패널티 탐지 — 읽기전용."""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_gsheets as U
APPLY = os.environ.get("STOCKS","") == "APPLY"
def is_pen(p): return "미추천" in str(p) or "패널티" in str(p)
ss=U.get_spreadsheet()
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
print("대상:",ws.title,"/ 모드:", "적용" if APPLY else "드라이런")
vals=ws.get_all_values()
blocks=U.find_person_blocks(vals)
def recommended(b, D):
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        j=row[9] if len(row)>9 else ''; k=row[10] if len(row)>10 else ''
        p=row[15] if len(row)>15 else ''; q=row[16] if len(row)>16 else ''
        if j and str(D) in str(k): return True
        if p and str(D) in str(q) and not is_pen(p): return True
    return False
wrong=[]
for b in blocks:
    person=b["person"]
    if not person: continue
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        p=row[15] if len(row)>15 else ''; q=row[16] if len(row)>16 else ''
        if p and is_pen(p) and q:
            try: D=datetime.date.fromisoformat(str(q).strip()[:10])
            except: continue
            if recommended(b, D):
                wrong.append((person,str(D),r))
print(f"\n잘못된 패널티(그날 추천했는데 패널티 있음): {len(wrong)}건")
for person,D,r in wrong:
    print(f"  ❌ {person} {D} 패널티(R{r}) — 실제 추천함 → 제거 대상")
if APPLY and wrong:
    ups=[]
    for _,_,r in wrong:
        ups.append({"range":f"P{r}:U{r}","values":[["","","","","",""]]})
    ws.batch_update(ups, value_input_option="USER_ENTERED")
    print(f"\n→ {len(wrong)}건 제거 완료")
