"""[일회성] 3분기 시트 생성 = 2분기 복제 후 데이터/좌측순위표 초기화."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
SRC="한탕(26년 2분기)"; DST="한탕(26년 3분기)"
titles=[w.title for w in ss.worksheets()]
print("기존 시트:", titles)
if DST in titles:
    print(f"[중단] '{DST}' 이미 존재"); raise SystemExit(0)
src=ss.worksheet(SRC)
new=ss.duplicate_sheet(src.id, insert_sheet_index=len(ss.worksheets()), new_sheet_name=DST)
print(f"복제 완료: '{DST}' (gid={new.id})")
vals=new.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
blocks=[]
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if s: blocks.append((h+1,s-1))
clear=["B4:G60"]
for st,en in blocks:
    clear.append(f"J{st}:N{en}"); clear.append(f"P{st}:U{en}")
new.batch_clear(clear)
print(f"초기화 완료 ({len(clear)}범위), 블록 {len(blocks)}개")
v2=new.get_all_values()
names=[(v2[h][8] if len(v2[h])>8 else '').replace('\n','') for h in hdr if h<len(v2) and (v2[h][8] if len(v2[h])>8 else '')]
print("멤버:", names)
print("시트 순서:", [w.title for w in ss.worksheets()])
