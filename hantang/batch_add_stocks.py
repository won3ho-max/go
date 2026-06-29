"""[일회성 진단] 시트 목록 + 마지막 시트 블록 구조 조회 — 읽기전용."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
print("=== 전체 워크시트(순서대로) ===")
for i,s in enumerate(ss.worksheets()):
    print(f"  [{i}] {s.title!r}  rows={s.row_count} cols={s.col_count}")
sheets=[s for s in ss.worksheets() if not s.title.startswith("_")]
last=sheets[-1]
print(f"\n=== batch_add 대상(sheets[-1]) = {last.title!r} ===")
vals=last.get_all_values()
print(f"총 {len(vals)}행")
# 블록 파싱
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
print(f"헤더행: {hdr}\n소계행: {sog}")
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if not s: continue
    name=(vals[h][8] if len(vals[h])>8 else '').strip().replace('\n','')
    # 활성 종목 빈행 여부
    filled=[ (r, vals[r-1][9]) for r in range(h+1,s) if r-1<len(vals) and (vals[r-1][9] if len(vals[r-1])>9 else '')]
    empties=[ r for r in range(h+1,s) if r-1<len(vals) and not (vals[r-1][9] if len(vals[r-1])>9 else '')]
    print(f"  블록 '{name}': 행{h+1}~{s-1} | 기존종목={[f[1] for f in filled]} | 빈행수={len(empties)}")
