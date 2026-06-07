#!/usr/bin/env python3
"""
build_portfolio.py
──────────────────
Notion Portfolio_KR / Portfolio_US DB 전 행을 REST API로 읽어
portfolio.json (정적 스냅샷) 을 생성한다.

스키마 기준 : S_portfolio_sync.md §3.1 (정적 13필드 + sector)
대상        : 일반 계좌 & 보유수량 > 0 행만 포함
진실의 원천 : Notion  →  json 단방향

사용법:
    NOTION_TOKEN=secret_xxx python3 scripts/build_portfolio.py
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 상수 ──────────────────────────────────────────────────────────────────────
PORTFOLIO_KR_ID = "36236335-6ea8-8077-87aa-000b1dc9811f"
PORTFOLIO_US_ID = "36336335-6ea8-81d6-aded-000be1c52fec"

NOTION_VERSION  = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"

# 일반 계좌 행만 포함 (연금저축·IRP·ISA 제외)
TARGET_ACCOUNT = "일반"

# 출력 경로 (스크립트 위치 기준 상위)
OUTPUT_PATH = Path(__file__).parent.parent / "portfolio.json"

# KST = UTC+9
KST = timezone(timedelta(hours=9))


# ── Notion 조회 헬퍼 ──────────────────────────────────────────────────────────

def notion_query_all(database_id: str, token: str) -> list[dict]:
    """
    POST /v1/databases/{id}/query  페이지네이션으로 전 행 수집.
    MCP semantic 검색 대신 REST API 결정적 조회를 사용한다.
    (엔씨소프트·펩시 등 semantic 검색 누락 종목 대응)
    """
    url     = f"{NOTION_API_BASE}/databases/{database_id}/query"
    headers = {
        "Authorization"  : f"Bearer {token}",
        "Notion-Version" : NOTION_VERSION,
        "Content-Type"   : "application/json",
    }
    rows        = []
    start_cursor = None

    while True:
        payload: dict = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        req = urllib.request.Request(
            url,
            data    = json.dumps(payload).encode(),
            headers = headers,
            method  = "POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(
                f"[Notion API 오류] {database_id} HTTP {e.code}: {body}"
            ) from e

        rows.extend(data.get("results", []))
        if data.get("has_more"):
            start_cursor = data.get("next_cursor")
        else:
            break

    return rows


# ── 속성 추출 헬퍼 ────────────────────────────────────────────────────────────

def _text(prop: dict | None) -> str | None:
    """title / rich_text → str"""
    if not prop:
        return None
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in items) or None


def _select(prop: dict | None) -> str | None:
    if not prop:
        return None
    sel = prop.get("select")
    return sel.get("name") if sel else None


def _number(prop: dict | None) -> float | int | None:
    if not prop:
        return None
    val = prop.get("number")
    return val  # None 허용


def _date(prop: dict | None) -> str | None:
    """date → YYYY-MM-DD"""
    if not prop:
        return None
    d = prop.get("date")
    if not d:
        return None
    start = d.get("start", "")
    return start[:10] if start else None


# ── 행 → json 항목 변환 ───────────────────────────────────────────────────────

def row_to_entry(page: dict, market_label: str) -> dict | None:
    """
    Notion 페이지 → portfolio.json 종목 딕셔너리.
    - 계좌구분 != 일반  → None (제외)
    - 보유수량 == 0     → None (제외)
    - sector 없으면 키 생략
    - consensusTarget 없으면 키 생략
    """
    props = page.get("properties", {})

    # ── 필수 식별자 ──
    ticker = _text(props.get("티커/코드") or props.get("티커")) \
             or _text(props.get("Ticker"))
    name   = _text(props.get("종목명") or props.get("Name"))
    if not ticker or not name:
        return None   # 식별자 결손 행 제외

    # ── 계좌구분 필터 ──
    account = _select(props.get("계좌구분") or props.get("Account"))
    if account != TARGET_ACCOUNT:
        return None

    # ── 보유수량 필터 ──
    quantity = _number(props.get("보유수량") or props.get("Quantity"))
    if quantity is None or quantity == 0:
        return None

    # ── 정적 13필드 ──
    asset_type    = _select(props.get("자산군") or props.get("AssetType"))
    sector        = _select(props.get("산업·섹터") or props.get("Sector"))
    avg_price     = _number(props.get("평균매입단가") or props.get("AvgPrice"))
    target_price  = _number(props.get("목표가") or props.get("TargetPrice"))
    consensus     = _number(props.get("컨센서스 목표가") or props.get("ConsensusTarget"))
    stop_loss     = _number(props.get("손절가") or props.get("StopLossPrice"))
    strategy      = _select(props.get("전략태그") or props.get("Strategy"))
    purchase_date = _date(props.get("매수일") or props.get("PurchaseDate"))

    entry: dict = {
        "name"          : name,
        "ticker"        : ticker,
        "market"        : market_label,
        "assetType"     : asset_type,
        "account"       : account,
        "avgPrice"      : avg_price,
        "quantity"      : quantity,
        "targetPrice"   : target_price,
        "stopLossPrice" : stop_loss,
        "strategy"      : strategy,
        "purchaseDate"  : purchase_date,
    }

    # 선택 필드 (없으면 키 생략)
    if sector:
        entry["sector"] = sector
    if consensus is not None:
        entry["consensusTarget"] = consensus

    # None 값 정리 (선택 아닌 필드에도 None 남을 수 있음)
    entry = {k: v for k, v in entry.items() if v is not None}

    return entry


# ── 검증 ─────────────────────────────────────────────────────────────────────

def validate(holdings_kr: list, holdings_us: list) -> list[str]:
    """
    정합 검증 — 실패해도 json 생성은 계속한다.
    반환값: 경고 메시지 목록 (빈 리스트면 정상)
    """
    warnings = []

    kr_tickers = {e["ticker"] for e in holdings_kr}
    us_tickers = {e["ticker"] for e in holdings_us}

    if "036570" not in kr_tickers:
        warnings.append("⚠️  정합 경고: 엔씨소프트(036570)가 KR에 없습니다.")
    if "PEP" not in us_tickers:
        warnings.append("⚠️  정합 경고: 펩시(PEP)가 US에 없습니다.")

    return warnings


# ── 기존 json 메타 보존 ───────────────────────────────────────────────────────

def load_existing_meta(path: Path) -> dict:
    """기존 portfolio.json에서 seedMoney 등 메타를 읽어 반환."""
    if not path.exists():
        return {"seedMoney": 10000000}
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        return {"seedMoney": existing.get("seedMoney", 10000000)}
    except Exception:
        return {"seedMoney": 10000000}


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        print("❌ 환경변수 NOTION_TOKEN 이 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)

    print("▶ Portfolio_KR 조회 중 …")
    try:
        kr_rows = notion_query_all(PORTFOLIO_KR_ID, token)
    except RuntimeError as e:
        print(f"❌ Portfolio_KR 조회 실패: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"  → {len(kr_rows)}행 수신")

    print("▶ Portfolio_US 조회 중 …")
    try:
        us_rows = notion_query_all(PORTFOLIO_US_ID, token)
    except RuntimeError as e:
        print(f"❌ Portfolio_US 조회 실패: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"  → {len(us_rows)}행 수신")

    # 변환
    holdings_kr = [
        e for row in kr_rows
        if (e := row_to_entry(row, "국내")) is not None
    ]
    holdings_us = [
        e for row in us_rows
        if (e := row_to_entry(row, "미국")) is not None
    ]

    print(f"  → 일반계좌 KR {len(holdings_kr)}종목 / US {len(holdings_us)}종목 추출")

    # 정합 검증
    warns = validate(holdings_kr, holdings_us)
    for w in warns:
        print(w, file=sys.stderr)

    # 메타 보존
    meta = load_existing_meta(OUTPUT_PATH)

    output = {
        "seedMoney"   : meta["seedMoney"],
        "lastUpdated" : datetime.now(KST).strftime("%Y-%m-%d"),
        "holdings_kr" : holdings_kr,
        "holdings_us" : holdings_us,
    }

    # 출력
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    if warns:
        print(
            f"\n⚠️  portfolio.json 생성 완료 (정합 경고 {len(warns)}건 — 위 경고 확인 바람)",
            file=sys.stderr,
        )
    else:
        print(f"\n✅ portfolio.json 생성 완료 → {OUTPUT_PATH}")
        print(f"   KR {len(holdings_kr)}종목 / US {len(holdings_us)}종목 / lastUpdated {output['lastUpdated']}")


if __name__ == "__main__":
    main()
