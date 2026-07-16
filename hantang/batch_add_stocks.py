"""[일회성 조회] 김동환 활성 종목 — 읽기전용."""
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
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if not s: continue
    nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
    if nm!="김동환": continue
    print(f"김동환 블록 행{h+1}~{s-1}")
    for r in range(h+1,s):
        row=vals[r-1]
        j=row[9] if len(row)>9 else ''
        k=row[10] if len(row)>10 else ''
        l=row[11] if len(row)>11 else ''
        if j: print(f"  R{r}: 종목={j!r} 추천일={k!r} 기준가={l!r}")
