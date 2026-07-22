# -*- coding: utf-8 -*-
"""
카카오톡 선물하기 랭킹 자동 수집기
- 지정한 카테고리의 상위 N위(순위/브랜드/상품명/위시수)를 시간별로 기록
- 화면 글자를 긁지 않고, 페이지가 스스로 불러오는 JSON을 가로채서 저장 (안정적)
- 결과: data/ranking.sqlite (누적 히스토리) + data/latest_*.csv (최신 스냅샷)

사용법:
  python collector.py            # 실제 수집 (스케줄러가 1시간마다 이걸 실행)
  python collector.py --debug    # 처음 1회: 무엇을 잡았는지 debug/ 폴더에 저장 (필드 보정용)
"""

import sys
import os
import re
import csv
import json
import time
import sqlite3
import datetime

from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# 1) 추적할 카테고리
#    url 은 "카카오 선물하기 랭킹 페이지에서 해당 카테고리 탭을 눌렀을 때
#    주소창에 뜨는 URL" 을 그대로 넣으면 됩니다. (처음 세팅 때 한 번만)
#    지금은 대표 랭킹 페이지로 두었고, 보정 단계에서 정확한 주소로 교체합니다.
# ----------------------------------------------------------------------------
CATEGORIES = [
    {"name": "건강_전체",        "url": "https://gift.kakao.com/ranking"},
    {"name": "이너뷰티_다이어트", "url": "https://gift.kakao.com/ranking"},
]

TOP_N = 500          # 몇 위까지 기록할지
SCROLL_ROUNDS = 40   # 500위까지 불러오려면 스크롤을 여러 번 해야 함
HEADLESS = True      # True = 창 안 뜨고 백그라운드 실행

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
DB_PATH = os.path.join(DATA_DIR, "ranking.sqlite")

# ----------------------------------------------------------------------------
# 필드 이름 후보 (카카오 JSON의 키 이름이 무엇이든 여기서 자동 매칭)
# 보정 단계에서 실제 키를 확인하면 맨 앞에 추가해 정확도를 올립니다.
# ----------------------------------------------------------------------------
NAME_KEYS  = ["productName", "itemName", "name", "title", "dispName", "goodsName"]
BRAND_KEYS = ["brandName", "brand", "shopName", "sellerName", "makerName"]
WISH_KEYS  = ["wishCount", "wishCnt", "wish", "likeCount", "favoriteCount", "zzimCount"]
RANK_KEYS  = ["rank", "ranking", "rankNo", "order"]
PRICE_KEYS = ["price", "sellPrice", "discountPrice", "amount"]


def now_kst():
    # 깃허브 서버는 UTC로 도므로, 한국시간(UTC+9)으로 명시 변환
    KST = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(datetime.timezone.utc).astimezone(KST)


def first_key(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def looks_like_ranking_list(lst):
    """리스트가 '상품 목록'처럼 보이는지 점수화"""
    if not isinstance(lst, list) or len(lst) < 5:
        return 0
    sample = [x for x in lst[:10] if isinstance(x, dict)]
    if not sample:
        return 0
    score = 0
    for item in sample:
        if first_key(item, NAME_KEYS):
            score += 2
        if first_key(item, WISH_KEYS) is not None:
            score += 2
        if first_key(item, BRAND_KEYS):
            score += 1
        if first_key(item, PRICE_KEYS) is not None:
            score += 1
    return score


def find_ranking_lists(obj, found):
    """JSON 전체를 훑어 상품목록으로 보이는 리스트들을 수집"""
    if isinstance(obj, list):
        s = looks_like_ranking_list(obj)
        if s >= 4:
            found.append((s, obj))
        for x in obj:
            find_ranking_lists(x, found)
    elif isinstance(obj, dict):
        for v in obj.values():
            find_ranking_lists(v, found)


def parse_items(payloads):
    """가로챈 JSON들 중 가장 그럴듯한 상품목록을 골라 표준 형식으로 변환"""
    candidates = []
    for p in payloads:
        find_ranking_lists(p, candidates)
    if not candidates:
        return []
    # 가장 길고 점수 높은 목록 선택
    candidates.sort(key=lambda t: (t[0], len(t[1])), reverse=True)
    best = candidates[0][1]

    rows = []
    for idx, it in enumerate(best, start=1):
        if not isinstance(it, dict):
            continue
        name = first_key(it, NAME_KEYS)
        if not name:
            continue
        rank = first_key(it, RANK_KEYS) or idx
        rows.append({
            "rank": int(rank) if str(rank).isdigit() else idx,
            "brand": first_key(it, BRAND_KEYS) or "",
            "name": str(name),
            "wish": first_key(it, WISH_KEYS),
            "price": first_key(it, PRICE_KEYS),
        })
        if len(rows) >= TOP_N:
            break
    # 순위 정렬 + 상위 N
    rows.sort(key=lambda r: r["rank"])
    return rows[:TOP_N]


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ranking (
            collected_at TEXT,      -- 수집 시각 (YYYY-MM-DD HH:MM)
            category     TEXT,
            rank         INTEGER,
            brand        TEXT,
            name         TEXT,
            wish         INTEGER,
            price        INTEGER
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_time ON ranking(collected_at, category)")
    con.commit()
    return con


def save_rows(con, category, rows, stamp):
    con.executemany(
        "INSERT INTO ranking VALUES (?,?,?,?,?,?,?)",
        [(stamp, category, r["rank"], r["brand"], r["name"],
          _to_int(r["wish"]), _to_int(r["price"])) for r in rows]
    )
    con.commit()
    # 최신 스냅샷 CSV
    csv_path = os.path.join(DATA_DIR, f"latest_{category}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["수집시각", "순위", "브랜드", "상품명", "위시수", "가격"])
        for r in rows:
            w.writerow([stamp, r["rank"], r["brand"], r["name"],
                        _to_int(r["wish"]), _to_int(r["price"])])


def _to_int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.sub(r"[^\d]", "", str(v))
    return int(m) if m else None


def collect_category(page, cat, debug=False):
    payloads = []

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct.lower():
                return
            data = resp.json()
            payloads.append(data)
            if debug:
                fn = re.sub(r"[^a-zA-Z0-9]+", "_", resp.url)[-80:]
                path = os.path.join(DEBUG_DIR, f"{cat['name']}__{fn}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    page.on("response", on_response)
    page.goto(cat["url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    # 500위까지 불러오기 위해 반복 스크롤
    for _ in range(SCROLL_ROUNDS):
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(700)

    page.remove_listener("response", on_response)
    return parse_items(payloads)


def main():
    debug = "--debug" in sys.argv
    os.makedirs(DEBUG_DIR, exist_ok=True)
    con = init_db()
    stamp = now_kst().strftime("%Y-%m-%d %H:%M")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            locale="ko-KR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        for cat in CATEGORIES:
            try:
                rows = collect_category(page, cat, debug=debug)
                if rows:
                    save_rows(con, cat["name"], rows, stamp)
                    print(f"[{stamp}] {cat['name']}: {len(rows)}개 저장")
                else:
                    print(f"[{stamp}] {cat['name']}: 상품목록을 못 찾음 "
                          f"(--debug 로 한 번 돌려 debug 폴더를 확인하세요)")
            except Exception as e:
                print(f"[{stamp}] {cat['name']}: 오류 - {e}")
        browser.close()
    con.close()


if __name__ == "__main__":
    main()
