#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_history.py — 시드머니 대비 평가금액 추이용 history.json 누적기

설계 결정 (작업11 / 2026-05-25 확정):
  - 계산 주체 : 서버 (GitHub Actions). 완전 자동
  - 시세 소스 : Stooq CSV (종가 기준, 데이터센터 IP 친화·무료)
                 한국 005930.KS / 미국 AAPL.US 형식
  - 환율      : frankfurter.app (클라이언트 index.html과 동일 소스)
  - 타이밍    : KST 16:00 (한국 장 마감 후) 1회
  - 누적 방식 : 당일 1행 append. 같은 날짜 재실행 시 해당 행 갱신(upsert)
  - 미국 종목 : 종가 미수신 시 직전 history의 마지막 종가 carry-forward

클라이언트 정합:
  index.html drawTrendChart()가 기대하는 키 — date·evalAmount·pnlRate·fxRate
"""

import json
import sys
import csv
import io
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

KST = timezone(timedelta(hours=9))
HISTORY_FILE = "history.json"
PORTFOLIO_FILE = "portfolio.json"
FX_FALLBACK = 1497.76  # 클라이언트 index.html 폴백값과 동일
REQUEST_TIMEOUT = 20


# ─────────────────────────────────────────────
# 외부 조회
# ─────────────────────────────────────────────
def http_get(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; DashboardBot/1.0)"})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_fx_usdkrw() -> float:
    """frankfurter.app에서 USD/KRW 종가. 실패 시 폴백."""
    try:
        raw = http_get("https://api.frankfurter.app/latest?from=USD&to=KRW")
        data = json.loads(raw)
        rate = data.get("rates", {}).get("KRW")
        if rate:
            return float(rate)
    except (URLError, HTTPError, json.JSONDecodeError, ValueError) as e:
        print(f"[warn] 환율 조회 실패, 폴백 사용: {e}", file=sys.stderr)
    return FX_FALLBACK


def stooq_symbol(ticker: str, market: str) -> str:
    """portfolio 티커 → Stooq 심볼. 국내 .KS / 미국 .US"""
    if market == "국내":
        return f"{ticker}.KS"
    return f"{ticker}.US"


def fetch_close_price(symbol: str):
    """Stooq 일별 CSV에서 가장 최근 종가 1개. 실패 시 None."""
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    try:
        raw = http_get(url)
        # Stooq는 데이터 없을 때 'N/D' 또는 빈 응답 반환
        if not raw or raw.strip().upper().startswith("N/D"):
            return None
        reader = list(csv.DictReader(io.StringIO(raw)))
        if not reader:
            return None
        # 마지막 행이 최신 종가
        last = reader[-1]
        close = last.get("Close")
        if close in (None, "", "N/D"):
            return None
        return float(close)
    except (URLError, HTTPError, ValueError, KeyError) as e:
        print(f"[warn] 종가 조회 실패 {symbol}: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────
# 파일 I/O
# ─────────────────────────────────────────────
def load_portfolio() -> dict:
    with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_history() -> list:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def last_known_price(history: list, ticker: str):
    """직전 history 행들에서 해당 티커의 마지막 종가 (carry-forward용)."""
    for row in reversed(history):
        prices = row.get("_prices", {})
        if ticker in prices:
            return prices[ticker]
    return None


# ─────────────────────────────────────────────
# 평가금액 계산
# ─────────────────────────────────────────────
def compute_eval(pf: dict, history: list, fx: float):
    """
    보유 종목 평가금액·매입원가 합계 (원화 환산).
    종가 미수신 종목: carry-forward → 그래도 없으면 평단 사용.
    반환: (평가금액, 매입원가, 종목별종가dict, 성공수, 전체수)
    """
    total_eval = 0.0
    total_cost = 0.0
    prices = {}
    ok = 0
    total = 0

    for market_key, market_label in (("holdings_kr", "국내"), ("holdings_us", "미국")):
        for h in pf.get(market_key, []):
            total += 1
            ticker = h["ticker"]
            qty = h["quantity"]
            avg = h["avgPrice"]
            sym = stooq_symbol(ticker, market_label)

            close = fetch_close_price(sym)
            if close is None:
                # carry-forward 시도
                close = last_known_price(history, ticker)
                if close is None:
                    close = avg  # 최후: 평단 (손익 0 처리)
                    print(f"[warn] {ticker} 종가·이력 모두 없음 → 평단 사용", file=sys.stderr)
                else:
                    print(f"[info] {ticker} carry-forward 적용", file=sys.stderr)
            else:
                ok += 1

            prices[ticker] = close

            # 원화 환산: 국내는 그대로, 미국은 환율 적용
            mult = fx if market_label == "미국" else 1.0
            total_eval += close * qty * mult
            total_cost += avg * qty * mult

    return total_eval, total_cost, prices, ok, total


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    try:
        pf = load_portfolio()
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[error] portfolio.json 로드 실패: {e}", file=sys.stderr)
        sys.exit(1)

    history = load_history()
    fx = fetch_fx_usdkrw()
    today = datetime.now(KST).strftime("%Y-%m-%d")

    total_eval, total_cost, prices, ok, total = compute_eval(pf, history, fx)

    # 전 종목 실패(평단으로만 계산) 시 기록 보류 — 오염 방지
    if ok == 0:
        print("[error] 종가 0종목 수신 — history 갱신 보류, 커밋 안 함", file=sys.stderr)
        sys.exit(1)

    pnl_rate = ((total_eval - total_cost) / total_cost * 100) if total_cost > 0 else 0.0

    new_row = {
        "date": today,
        "evalAmount": round(total_eval),
        "pnlRate": round(pnl_rate, 2),
        "fxRate": round(fx, 2),
        "_prices": prices,  # carry-forward용 내부 보존 (클라이언트는 무시)
    }

    # upsert: 같은 날짜 행이 있으면 교체, 없으면 append
    history = [r for r in history if r.get("date") != today]
    history.append(new_row)
    history.sort(key=lambda r: r.get("date", ""))

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print(f"[done] history.json 갱신 — {today} "
          f"평가 {new_row['evalAmount']:,}원 / 손익 {new_row['pnlRate']}% / "
          f"환율 {new_row['fxRate']} / 종가 {ok}/{total}", file=sys.stderr)


if __name__ == "__main__":
    main()
