"""[일회성 조회] 3분기 7/20·7/21 추천 현황 + 조형오 블록 전체 — 읽기전용."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
print("대상:",ws.title)
vals=ws.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
def blocks():
    out=[]
    for h in hdr:
        s=next((r for r in sog if r>h),None)
        if not s: continue
        nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
        out.append((nm,h+1,s-1))
    return out
print("\n=== 7/20·7/21 추천 현황 ===")
rec={}
for nm,st,en in blocks():
    if not nm: continue
    got=[]
    for r in range(st,en+1):
        j=vals[r-1][9] if len(vals[r-1])>9 else ''
        k=vals[r-1][10] if len(vals[r-1])>10 else ''
        if j and ("2026-07-20" in str(k) or "2026-07-21" in str(k)):
            got.append(f"{j}({k})")
    rec[nm]=got
    print(f"  {nm}: {got if got else '— 없음'}")
print("\n=== 조형오 블록 전체 ===")
for nm,st,en in blocks():
    if nm!="조형오": continue
    for r in range(st,en+1):
        row=vals[r-1]
        j=row[9] if len(row)>9 else ''
        k=row[10] if len(row)>10 else ''
        p=row[15] if len(row)>15 else ''
        q=row[16] if len(row)>16 else ''
        if j or p:
            print(f"  R{r}: 활성[{j} {k}]  실현[{p} {q}]")
