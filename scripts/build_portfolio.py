#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_portfolio.py
Notion Portfolio_KR / Portfolio_US → portfolio.json 생성

핵심 변경(2026-06): Notion API 2025-09-03 파괴적 변경 대응
  - 기존: /v1/databases/{id}/query  (database_id 요구)  → 404
  - 변경: /v1/data_sources/{id}/query (data_source_id 요구)
  - 레지스트리에 저장된 ID는 '데이터 소스 ID'이므로 그대로 사용한다.

규칙(S_portfolio_sync §3 단일 참조):
  - 정적 13필드만 동기화 (현재가·환율·평가금액 등 동적/계산 필드 제외)
  - 일반 계좌만 포함 (연금저축·IRP·ISA 제외), 보유수량 0/None 제외
  - seedMoney 보존, lastUpdated 오늘(KST) 갱신
  - sector·consensusTarget는 값이 없으면 키 생략
의존성 없음(stdlib urllib만 사용).
"""

import os
import sys
import json
import datetime
import urllib.request
import urllib.error

# ── 설정 ────────────────────────────────────────────────
NOTION_VERSION = "2025-09-03"
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()

# 레지스트리 = 데이터 소스 ID (database ID 아님)
DATA_SOURCES = {
    "kr": ("Portfolio_KR", "36236335-6ea8-8077-87aa-000b1dc9811f"),
    "us": ("Portfolio_US", "36336335-6ea8-81d6-aded-000be1c52fec"),
}

# 출력 경로 = 저장소 루트의 portfolio.json (스크립트는 scripts/ 하위)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(BASE_DIR, "..", "portfolio.json")

# Notion 속성명 → json 키 (출력 순서 = 현행 portfolio.json 순서)
FIELD_MAP = [
    ("종목명", "name"),
    ("티커", "ticker"),
    ("시장", "market"),
    ("자산군", "assetType"),
    ("산업·섹터", "sector"),
    ("계좌구분", "account"),
    ("평균매입단가", "avgPrice"),
    ("보유수량", "quantity"),
    ("목표가", "targetPrice"),
    ("컨센서스목표가", "consensusTarget"),
    ("손절가", "stopLossPrice"),
    ("전략태그", "strategy"),
    ("매수일", "purchaseDate"),
]
# 값이 없으면 키 자체를 생략하는 선택 필드
OPTIONAL_KEYS = {"sector", "consensusTarget"}


# ── 유틸 ────────────────────────────────────────────────
def normalize(s):
    """속성명 공백 차이 흡수 (예: '컨센서스 목표가' ↔ '컨센서스목표가')."""
    return "".join(str(s).split())


def kst_today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")


def extract(prop):
    """Notion 속성 객체에서 값 추출. 동적/계산(formula 등)은 None."""
    if not prop:
        return None
    t = prop.get("type")
    if t == "title":
        return ("".join(x.get("plain_text", "") for x in prop.get("title", [])).strip()) or None
    if t == "rich_text":
        return ("".join(x.get("plain_text", "") for x in prop.get("rich_text", [])).strip()) or None
    if t == "number":
        return prop.get("number")
    if t == "select":
        sel = prop.get("select")
        return sel.get("name") if sel else None
    if t == "status":
        st = prop.get("status")
        return st.get("name") if st else None
    if t == "date":
        d = prop.get("date")
        return d.get("start") if d else None
    return None  # formula·rollup 등 동적/계산 필드 무시


def notion_query(ds_id):
    """data_sources 쿼리 — 전 행 페이지네이션."""
    url = "https://api.notion.com/v1/data_sources/{}/query".format(ds_id)
    headers = {
        "Authorization": "Bearer {}".format(NOTION_TOKEN),
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    results = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            raise RuntimeError("[Notion API 오류] {} HTTP {}: {}".format(ds_id, e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("[네트워크 오류] {} : {}".format(ds_id, e))
        results.extend(payload.get("results", []))
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
    return results


def build_holding(page):
    props = page.get("properties", {})
    norm = {normalize(k): v for k, v in props.items()}
    h = {}
    for ko, key in FIELD_MAP:
        val = extract(norm.get(normalize(ko)))
        if val is None and key in OPTIONAL_KEYS:
            continue
        h[key] = val
    return h


# ── 메인 ────────────────────────────────────────────────
def main():
    if not NOTION_TOKEN:
        print("❌ NOTION_TOKEN 환경변수가 비어 있습니다.")
        sys.exit(2)

    holdings = {"kr": [], "us": []}

    for mk, (label, ds_id) in DATA_SOURCES.items():
        print("▶ {} 조회 중 … (data_source {})".format(label, ds_id))
        try:
            pages = notion_query(ds_id)
        except RuntimeError as e:
            print("❌ {} 조회 실패: {}".format(label, e))
            sys.exit(2)

        for page in pages:
            h = build_holding(page)

            # 일반 계좌만
            if h.get("account") != "일반":
                continue
            # 보유수량 0/None 제외
            q = h.get("quantity")
            if not q or q <= 0:
                continue
            # 시장 라우팅
            market = h.get("market")
            if market == "국내":
                holdings["kr"].append(h)
            elif market == "미국":
                holdings["us"].append(h)
            else:
                print("⚠ 시장값 미상('{}') — 스킵: {}".format(market, h.get("name")))

    # seedMoney 보존 (기존 portfolio.json에서 읽음)
    seed = None
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, encoding="utf-8") as f:
                seed = json.load(f).get("seedMoney")
        except Exception as e:
            print("⚠ 기존 portfolio.json 파싱 실패(seedMoney 보존 불가): {}".format(e))
    if seed is None:
        print("⚠ seedMoney를 기존 파일에서 찾지 못했습니다. 0으로 설정 — 수동 확인 요망.")
        seed = 0

    out = {
        "seedMoney": seed,
        "lastUpdated": kst_today(),
        "holdings_kr": holdings["kr"],
        "holdings_us": holdings["us"],
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # 정합 검증 (semantic 누락 점검 — 엔씨소프트·펩시)
    kr_tickers = {h.get("ticker") for h in holdings["kr"]}
    us_tickers = {h.get("ticker") for h in holdings["us"]}
    if "036570" not in kr_tickers:
        print("⚠ 정합 경고: KR 일반 계좌에 엔씨소프트(036570)가 없습니다.")
    if "PEP" not in us_tickers:
        print("⚠ 정합 경고: US 일반 계좌에 펩시(PEP)가 없습니다.")

    print("✅ portfolio.json 생성 완료")
    print("   KR {}종목 / US {}종목".format(len(holdings["kr"]), len(holdings["us"])))
    print("   경로: {}".format(os.path.abspath(OUTPUT_PATH)))


if __name__ == "__main__":
    main()
