#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_prices.py
경로 B: Yahoo Finance 종가 → Notion Portfolio_KR / Portfolio_US
        '현재가'·'갱신일' 속성 write-back

규칙(설계서 옵션 A / 범위 B-2 단일 참조):
  - 대상: 전체 보유 행 (세금우대 계좌 포함). 보유수량 > 0 인 행만
  - 갱신 속성: '현재가'(Number)·'갱신일'(Date) 2개만.
              평가금액·손익률·목표 도달률·손절 임박도 formula는 Notion이 자동 재계산
  - 심볼: 국내 {ticker}.KS (실패 시 .KQ 폴백) / 미국 {ticker} 원형
  - 미국 현재가는 USD 그대로 저장 (환율 불요, 손익률 formula는 USD 기준)
  - 조회·매핑 실패 종목은 스킵 — 현재가 미변경(낡은 값 유지가 오기입보다 안전)
  - write 권한 부재(401/403) 확인 시 명시 후 비정상 종료(추정 입력 금지)

Notion API 2025-09-03:
  - 행 조회는 /v1/data_sources/{id}/query (build_portfolio.py와 동일)
  - 속성 갱신은 /v1/pages/{page_id} PATCH (버전 무관, 안정)

의존성 없음(stdlib urllib만 사용).
"""

import os
import sys
import json
import time
import datetime
import urllib.request
import urllib.error

# ── 설정 ────────────────────────────────────────────────
NOTION_VERSION = "2025-09-03"
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()

# 레지스트리 = 데이터 소스 ID (G_project_base_guide.md §2 / build_portfolio.py와 동일)
DATA_SOURCES = {
    "kr": ("Portfolio_KR", "36236335-6ea8-8077-87aa-000b1dc9811f"),
    "us": ("Portfolio_US", "36336335-6ea8-81d6-aded-000be1c52fec"),
}

YAHOO_HEADERS = {
    # User-Agent 부재 시 Yahoo가 종종 429/403을 반환하므로 명시
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
}

REQUEST_TIMEOUT = 15      # 초
NOTION_THROTTLE = 0.35    # 초 (Notion rate limit ≈ 3req/s 준수)


# ── 유틸 ────────────────────────────────────────────────
def normalize(s):
    """속성명 공백 차이 흡수."""
    return "".join(str(s).split())


def kst_today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")


def extract(prop):
    """Notion 속성 객체에서 값 추출. formula·rollup 등 동적/계산 필드는 None."""
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
    return None


# ── Notion 조회 ─────────────────────────────────────────
def notion_query(ds_id):
    """data_sources 쿼리 — 전 행 페이지네이션. page_id 포함 원본 반환."""
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
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            raise RuntimeError("[Notion 조회 오류] {} HTTP {}: {}".format(ds_id, e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("[네트워크 오류] {} : {}".format(ds_id, e))
        results.extend(payload.get("results", []))
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
    return results


# ── Notion 쓰기 ─────────────────────────────────────────
class WritePermissionError(Exception):
    """통합에 Update content 권한이 없을 때 발생(401/403)."""


def notion_update_price(page_id, price, today):
    """페이지의 '현재가'·'갱신일'만 PATCH. formula 속성은 건드리지 않음."""
    url = "https://api.notion.com/v1/pages/{}".format(page_id)
    headers = {
        "Authorization": "Bearer {}".format(NOTION_TOKEN),
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    body = {
        "properties": {
            "현재가": {"number": price},
            "갱신일": {"date": {"start": today}},
        }
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            resp.read()
            return True
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")
        if e.code in (401, 403):
            raise WritePermissionError(
                "[쓰기 권한 없음] HTTP {}: {}".format(e.code, err_body)
            )
        # 그 외(예: 속성명 불일치 400)는 해당 종목만 실패 처리
        print("   ⚠ 쓰기 실패(page {}): HTTP {} {}".format(page_id[:8], e.code, err_body[:160]))
        return False
    except urllib.error.URLError as e:
        print("   ⚠ 네트워크 오류(page {}): {}".format(page_id[:8], e))
        return False


# ── Yahoo 시세 ──────────────────────────────────────────
def yahoo_close(symbol):
    """단일 심볼 최근 종가. 실패 시 None."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           "{}?interval=1d&range=5d".format(symbol))
    req = urllib.request.Request(url, headers=YAHOO_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
        print("   · {} 조회 실패: {}".format(symbol, e))
        return None
    try:
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]
            price = closes[-1] if closes else None
        return price
    except (KeyError, IndexError, TypeError):
        print("   · {} 응답 파싱 실패".format(symbol))
        return None


def resolve_price(ticker, market):
    """시장별 심볼 변환 후 종가 조회. (가격, 사용심볼) 반환, 실패 시 (None, None)."""
    if market == "미국":
        return yahoo_close(ticker), ticker
    if market == "국내":
        for suffix in (".KS", ".KQ"):       # 코스피 우선, 코스닥 폴백
            p = yahoo_close(ticker + suffix)
            if p is not None:
                return p, ticker + suffix
        return None, None
    # 시장값 미상
    return None, None


# ── 메인 ────────────────────────────────────────────────
def main():
    if not NOTION_TOKEN:
        print("❌ NOTION_TOKEN 환경변수가 비어 있습니다.")
        sys.exit(2)

    today = kst_today()
    n_ok = n_skip = n_fail = 0
    write_checked = False     # 첫 PATCH로 권한 검증

    for mk, (label, ds_id) in DATA_SOURCES.items():
        print("▶ {} 조회 중 … (data_source {})".format(label, ds_id))
        try:
            pages = notion_query(ds_id)
        except RuntimeError as e:
            print("❌ {} 조회 실패: {}".format(label, e))
            sys.exit(2)

        for page in pages:
            props = page.get("properties", {})
            norm = {normalize(k): v for k, v in props.items()}
            name = extract(norm.get(normalize("종목명")))
            ticker = extract(norm.get(normalize("티커")))
            market = extract(norm.get(normalize("시장")))
            qty = extract(norm.get(normalize("보유수량")))
            page_id = page.get("id")

            # 보유수량 0/None 제외 (전량매도 보존 행)
            if not qty or qty <= 0:
                continue
            if not ticker or not page_id:
                print("   · [{}] 티커/page_id 결손 — 스킵".format(name))
                n_skip += 1
                continue

            price, used = resolve_price(ticker, market)
            if price is None:
                print("   · [{}] {} 시세 미확보 — 스킵(현재가 미변경)".format(name, ticker))
                n_skip += 1
                continue

            try:
                ok = notion_update_price(page_id, price, today)
            except WritePermissionError as e:
                # 권한 자체가 없으면 전체 중단(추정·부분진행 의미 없음)
                print("❌ {}".format(e))
                print("   → 통합에 'Update content' 권한을 부여한 뒤 재실행하십시오.")
                sys.exit(3)

            write_checked = True
            if ok:
                cur = "{:.2f}".format(price) if market == "미국" else "{:,.0f}".format(price)
                print("   ✓ [{}] {} → {}".format(name, used, cur))
                n_ok += 1
            else:
                n_fail += 1
            time.sleep(NOTION_THROTTLE)

    print("────────────────────────────")
    print("✅ 시세 갱신 완료 ({})".format(today))
    print("   성공 {} / 스킵 {} / 실패 {}".format(n_ok, n_skip, n_fail))
    if not write_checked:
        print("   ⚠ 갱신 대상이 없었습니다(보유 행 0 또는 전 종목 스킵).")
    # 일부 실패가 있어도 성공분 반영을 위해 정상 종료(0)


if __name__ == "__main__":
    main()
