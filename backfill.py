# -*- coding: utf-8 -*-
"""
backfill.py — 과거 채우기 (한 번만 실행)
ranking.sqlite(전체 순위 원본)에서 타이거모닝 등 추적 브랜드의 과거 순위를 꺼내,
대시보드 그래프용 history/daily JSON을 처음부터 다시 만들어 준다.
(추적 브랜드가 100위 밖이라 과거 히스토리에서 빠졌던 걸 원본에서 복구)
"""
import sqlite3, json, os

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB   = os.path.join(DATA, "ranking.sqlite")

TRACK_BRANDS = ["타이거모닝"]                       # collector.py와 동일하게 유지
CATS = {"건강_전체": "health", "이너뷰티_다이어트": "innerbeauty"}


def keep_items(rows, keep_top=100):
    items, seen = [], set()
    for r in rows[:keep_top]:
        items.append({"r": r[0], "b": r[1], "n": r[2], "w": r[3]}); seen.add(r[2])
    for r in rows[keep_top:]:
        if r[2] in seen:
            continue
        if any(b in (r[1] or "") for b in TRACK_BRANDS):
            items.append({"r": r[0], "b": r[1], "n": r[2], "w": r[3]}); seen.add(r[2])
    return items


def main():
    con = sqlite3.connect(DB)
    for cname, slug in CATS.items():
        stamps = [r[0] for r in con.execute(
            "SELECT DISTINCT collected_at FROM ranking WHERE category=? ORDER BY collected_at", (cname,))]
        hourly, daily = [], []
        for st in stamps:
            rows = con.execute(
                "SELECT rank,brand,name,wish FROM ranking WHERE category=? AND collected_at=? ORDER BY rank",
                (cname, st)).fetchall()
            if not rows:
                continue
            items = keep_items(rows)
            hk = str(st)[:13]
            if hourly and str(hourly[-1]["t"])[:13] == hk:
                hourly[-1] = {"t": st, "items": items}
            else:
                hourly.append({"t": st, "items": items})
            day = str(st)[:10]
            if daily and daily[-1]["t"] == day:
                daily[-1] = {"t": day, "items": items}
            else:
                daily.append({"t": day, "items": items})
        hourly, daily = hourly[-720:], daily[-366:]
        json.dump(hourly, open(os.path.join(DATA, f"history_{slug}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        json.dump(daily, open(os.path.join(DATA, f"daily_{slug}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        print(f"{cname}: hourly {len(hourly)}개 / daily {len(daily)}개 재구성 완료")
    con.close()


if __name__ == "__main__":
    main()
