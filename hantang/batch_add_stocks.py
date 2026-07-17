"""[일회성] 3분기 어정윤 ANET 분석글 쓰레기 행 삭제 (아리스타 네트웍스(ANET)와 중복)."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=ss.worksheet("한탕(26년 3분기)")
vals=ws.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
targets=[]
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if not s: continue
    nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
    for r in range(h+1,s):
        j=vals[r-1][9] if len(vals[r-1])>9 else ''
        if j and len(j) > 40:      # 정상 종목명은 40자 넘지 않음 → 분석글 오염
            targets.append((nm,r,j[:45]))
print("오염(분석글) 행:", len(targets))
for nm,r,prev in targets:
    print(f"  {nm} R{r}: {prev}...")
if targets:
    ws.batch_update([{"range": f"J{r}:N{r}", "values": [["","","","",""]]} for _,r,_ in targets],
                    value_input_option="USER_ENTERED")
    print("→ 해당 행 J:N 삭제 완료")
else:
    print("오염 행 없음")
# 남은 어정윤 활성 확인
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if not s: continue
    nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
    if nm!="어정윤": continue
    print("\n어정윤 활성(삭제 후 재조회):")
    for r in range(h+1,s):
        row=ws.row_values(r)
        j=row[9] if len(row)>9 else ''
        k=row[10] if len(row)>10 else ''
        if j: print(f"  R{r}: {j} ({k})")
