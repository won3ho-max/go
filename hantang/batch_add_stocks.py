"""[일회성 드라이런] 미추천 패널티 자동화 시뮬레이션 — 읽기전용(쓰기 없음)."""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_gsheets as U

MEMBERS=["안병열","김동환","이광훈","송지호","조형오","어정윤","이원호","김태완"]
today=U.today_kst()
print("today_kst:",today,"(요일:",today.strftime("%a"),")")
ss=U.get_spreadsheet()
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
print("대상:",ws.title)
vals=ws.get_all_values()
blocks=U.find_person_blocks(vals)
def block(name):
    return next((b for b in blocks if b["person"]==name or name in b["person"]),None)

# 라운드 날짜 = 시트에 존재하는 '월요일' 추천일(활성 K + 실현 Q, 패널티 제외)
round_days=set()
def is_pen(p): return "미추천" in str(p) or "패널티" in str(p)
for b in blocks:
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        k=row[10] if len(row)>10 else ''
        p=row[15] if len(row)>15 else ''
        q=row[16] if len(row)>16 else ''
        j=row[9] if len(row)>9 else ''
        for d in [k if j else '', (q if (p and not is_pen(p)) else '')]:
            try:
                dd=datetime.date.fromisoformat(str(d).strip()[:10])
                if dd.weekday()==0 and dd<=today:   # 월요일, 오늘 이하
                    round_days.add(dd)
            except Exception: pass
print("감지된 라운드(월):", sorted(str(d) for d in round_days))

def recommended(name, D):
    b=block(name)
    if not b: return False
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        j=row[9] if len(row)>9 else ''; k=row[10] if len(row)>10 else ''
        p=row[15] if len(row)>15 else ''; q=row[16] if len(row)>16 else ''
        if j and str(D) in str(k): return True
        if p and (not is_pen(p)) and str(D) in str(q): return True
    return False
def has_pen(name, D):
    b=block(name)
    for r in range(b["row_start"],b["row_end"]+1):
        row=vals[r-1]
        p=row[15] if len(row)>15 else ''; q=row[16] if len(row)>16 else ''
        if p and is_pen(p) and str(D) in str(q): return True
    return False

for D in sorted(round_days):
    recs=[m for m in MEMBERS if recommended(m,D)]
    miss=[m for m in MEMBERS if not recommended(m,D)]
    new_pen=[m for m in miss if not has_pen(m,D)]
    already=[m for m in miss if has_pen(m,D)]
    print(f"\n[{D}] 추천 {len(recs)}명")
    print(f"   미추천: {miss}")
    print(f"   이미 패널티: {already}")
    print(f"   → 새로 넣을 패널티: {new_pen}")
