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
    print(f"[중단] '{DST}' 이미 존재 — 재생성 안 함"); raise SystemExit(0)

src=ss.worksheet(SRC)
# 끝에 복제
new=ss.duplicate_sheet(src.id, insert_sheet_index=len(ss.worksheets()), new_sheet_name=DST)
print(f"복제 완료: '{DST}' (gid={new.id})")

vals=new.get_all_values()
# 블록 탐색
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
blocks=[]
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if s: blocks.append((h+1,s-1))
print(f"블록 {len(blocks)}개: {blocks}")

clear=["B4:G60"]  # 좌측 순위표 (B~G만, I열 이름 보존)
for st,en in blocks:
    clear.append(f"J{st}:N{en}")   # 활성: 종목/추천일/기준가/현재가/수익률
    clear.append(f"P{st}:U{en}")   # 실현: 종목/추천일/매도일/매수가/매도가/수익률
new.batch_clear(clear)
print(f"초기화 완료 ({len(clear)}개 범위)")

# 검증
v2=new.get_all_values()
f2=new.get_all_values(value_render_option='FORMULA')
print("\n=== 검증 ===")
print("시트 순서:", [w.title for w in ss.worksheets()])
# 멤버 이름 유지 확인
names=[]
for h in hdr:
    nm=(v2[h][8] if len(v2[h])>8 else '').replace('\n','') if h<len(v2) else ''
    if nm: names.append(nm)
print("멤버 이름:", names)
# 데이터 비었는지
nonempty=[]
for st,en in blocks:
    for r in range(st,en+1):
        j=v2[r-1][9] if len(v2[r-1])>9 else ''
        p=v2[r-1][15] if len(v2[r-1])>15 else ''
        if j or p: nonempty.append((r,j,p))
print("데이터 잔존(있으면 문제):", nonempty[:10])
# 소계/합계 수식 유지
print("소계행 U18 수식:", f2[17][20] if len(f2)>17 and len(f2[17])>20 else None)
print("합계행 N235:", f2[234][13] if len(f2)>234 and len(f2[234])>13 else None,
      "U235:", f2[234][20] if len(f2)>234 and len(f2[234])>20 else None)
print("좌측 B4(비어야함):", repr(v2[3][1] if len(v2[3])>1 else ''))
