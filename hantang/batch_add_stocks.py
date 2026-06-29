"""[일회성 진단3] 좌측 요약 B~H + 합계행 수식 — 읽기전용."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=ss.worksheet("한탕(26년 2분기)")
def col(n):
    s=""
    while n>0: n,r=divmod(n-1,26); s=chr(65+r)+s
    return s
form=ws.get_all_values(value_render_option='FORMULA')
vals=ws.get_all_values()
print("=== 좌측 요약 B~H (행2~20) formula ===")
for r in range(2,21):
    cells=[(col(c+1), form[r-1][c]) for c in range(1,8) if (len(form[r-1])>c and form[r-1][c]!="")]
    if cells: print(f"R{r}:", cells)
print("\n=== 합계행 234~235 전체 formula ===")
for r in [234,235]:
    cells=[(col(c+1), form[r-1][c]) for c in range(0,22) if (len(form[r-1])>c and form[r-1][c]!="")]
    print(f"R{r}:", cells)
print("\n=== W열 이후(룰 텍스트) 존재 행 수 / 멤버 이름 위치(I열) ===")
for r in [4,22,40,58,76,94,112,130,148,166,184,202,220]:
    iv = vals[r-1][8] if len(vals[r-1])>8 else ""
    print(f"  R{r} I열={iv!r}")
