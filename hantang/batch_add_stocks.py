"""[일회성] 티엘비 권리락 훼손 매도가 복원 (7/16·7/17 실행이 덮어쓴 값 원복)."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=ss.worksheet("한탕(26년 2분기)")
print("대상:",ws.title)
vals=ws.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
def blk(name):
    for h in hdr:
        s=next((r for r in sog if r>h),None)
        if not s: continue
        nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
        if nm and (nm==name or name in nm): return h+1,s-1
    return None
RESTORE=[("이광훈","2026-04-29",84900),("이광훈","2026-05-04",91900),
         ("이광훈","2026-06-10",96500),("이광훈","2026-06-25",79100),
         ("이광훈","2026-06-30",82300),("김태완","2026-05-12",96400)]
ups=[]
for person,sd,orig in RESTORE:
    b=blk(person)
    if not b: print(f"  ❌ {person} 블록없음"); continue
    found=None
    for r in range(b[0],b[1]+1):
        row=vals[r-1]
        p=row[15] if len(row)>15 else ''
        rr=row[17] if len(row)>17 else ''
        if p and "티엘비" in p and sd in str(rr):
            found=r; break
    if not found: print(f"  ❌ {person} 티엘비 {sd} 행 없음"); continue
    cur=vals[found-1][19] if len(vals[found-1])>19 else ''
    s_=vals[found-1][18] if len(vals[found-1])>18 else ''
    ups.append({"range":f"T{found}","values":[[orig]]})
    ups.append({"range":f"U{found}","values":[[f"=(T{found}-S{found})/S{found}"]]})
    try:
        ret=(orig-float(str(s_).replace(",","")))/float(str(s_).replace(",",""))*100
    except Exception: ret=0
    print(f"  ✅ {person} R{found} 티엘비 {sd}: 매도가 {cur} → {orig:,} (매수가 {s_} → 수익률 {ret:+.1f}%)")
if ups:
    ws.batch_update(ups, value_input_option="USER_ENTERED")
print("복원 완료")
