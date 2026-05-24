#!/usr/bin/env python3
"""
collect_snapshot.py
장 마감 후 GitHub Actions에서 실행.
portfolio.json 보유 종목 + 당일 종가 + 환율로 evalAmount를 산출해
history.json에 일자별 1행씩 누적 (같은 날짜면 overwrite).

요구사항: pip install yfinance requests
"""

import json
import sys
import os
from datetime import datetime, timezone, timedelta

try:
    import yfinance as yf
except ImportError:
    print("[FATAL] yfinance 미설치. pip install yfinance 실행 후 재시도.")
    sys.exit(1)

# ─── 경로 설정 ────────────────────────────────────────────
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTFOLIO_PATH = os.path.join(REPO_ROOT, "portfolio.json")
HISTORY_PATH   = os.path.join(REPO_ROOT, "history.json")

# ─── 오늘 날짜 (KST 기준) ────────────────────────────────
KST = timezone(timedelta(hours=9))
now_kst = datetime.now(KST)

# workflow_dispatch date_override 지원 (누락일 보완용)
_env_date = os.environ.get("SNAPSHOT_DATE", "").strip()
today_kst = _env_date if _env_date else now_kst.strftime("%Y-%m-%d")
date_source = "[강제지정]" if _env_date else "[자동]"

print(f"[INFO] 실행 일시 (KST): {now_kst.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"[INFO] 기록 기준일: {today_kst} {date_source}")


# ─── portfolio.json 로드 ──────────────────────────────────
def load_portfolio():
    if not os.path.exists(PORTFOLIO_PATH):
        print(f"[FATAL] portfolio.json 없음: {PORTFOLIO_PATH}")
        sys.exit(1)
    with open(PORTFOLIO_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── 시세 조회 (yfinance) ────────────────────────────────
def fetch_price(symbol: str) -> float | None:
    """
    yfinance로 종가 조회.
    장 마감 후 실행이므로 regularMarketPrice 또는 직전 종가 사용.
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "regular_market_price", None)
        if price and price > 0:
            return float(price)
        # fallback: 최근 2일 history
        hist = ticker.history(period="2d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
        return None
    except Exception as e:
        print(f"[WARN] {symbol} 시세 조회 실패: {e}")
        return None


def fetch_fx_rate() -> float | None:
    """원/달러 환율 — yfinance KRW=X"""
    return fetch_price("KRW=X")


# ─── API 가용성 확인 (첫 실행 로그용) ─────────────────────
def api_check():
    print("\n[API CHECK] ── 가용성 확인 ──────────────────")
    # 삼성전자 KS (국내 대표)
    kr_test = fetch_price("005930.KS")
    print(f"[API CHECK] KR (005930.KS): {'OK ' + str(kr_test) if kr_test else 'FAIL'}")
    # 애플 (미국 대표)
    us_test = fetch_price("AAPL")
    print(f"[API CHECK] US (AAPL):      {'OK ' + str(us_test) if us_test else 'FAIL'}")
    # 환율
    fx_test = fetch_fx_rate()
    print(f"[API CHECK] FX (KRW=X):     {'OK ' + str(fx_test) if fx_test else 'FAIL'}")
    print("[API CHECK] ─────────────────────────────────\n")
    return kr_test, us_test, fx_test


# ─── 평가금액 산출 ────────────────────────────────────────
def calc_eval_amount(portfolio: dict, fx_rate: float) -> dict:
    """
    국내·미국 보유 종목 전체의 원화 평가금액 합산.
    seed_money 기준 손익률 산출.
    """
    total_eval_krw = 0.0
    fail_tickers   = []
    ok_tickers     = []

    # 국내
    for h in portfolio.get("holdings_kr", []):
        ticker  = h["ticker"] + ".KS"
        qty     = float(h["quantity"])
        avg     = float(h["avgPrice"])
        price   = fetch_price(ticker)
        if price is None:
            print(f"[WARN] {h['name']} ({ticker}) 시세 실패 → 평단({avg}) 대체")
            price = avg
            fail_tickers.append(ticker)
        else:
            ok_tickers.append(ticker)
        total_eval_krw += price * qty

    # 미국
    for h in portfolio.get("holdings_us", []):
        ticker  = h["ticker"]
        qty     = float(h["quantity"])
        avg     = float(h["avgPrice"])
        price   = fetch_price(ticker)
        if price is None:
            print(f"[WARN] {h['name']} ({ticker}) 시세 실패 → 평단({avg}) 대체")
            price = avg
            fail_tickers.append(ticker)
        else:
            ok_tickers.append(ticker)
        total_eval_krw += price * qty * fx_rate

    seed_money = float(portfolio.get("seedMoney", 10_000_000))
    pnl_rate   = round((total_eval_krw - seed_money) / seed_money * 100, 2)

    print(f"[INFO] 평가금액: {total_eval_krw:,.0f} KRW")
    print(f"[INFO] 손익률:   {pnl_rate:+.2f}%")
    print(f"[INFO] 시세 수신: {len(ok_tickers)}/{len(ok_tickers) + len(fail_tickers)}")
    if fail_tickers:
        print(f"[WARN] 평단 대체 종목: {fail_tickers}")

    return {
        "evalAmount": round(total_eval_krw),
        "pnlRate":    pnl_rate,
        "fxRate":     round(fx_rate, 2),
        "partial":    len(fail_tickers) > 0,   # 부분 실패 여부 (참고용)
    }


# ─── history.json 로드 ────────────────────────────────────
def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        print(f"[INFO] history.json 없음 → 신규 생성")
        return []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        print("[WARN] history.json 형식 오류 → 빈 배열로 초기화")
        return []
    return data


# ─── history.json 저장 ────────────────────────────────────
def save_history(history: list, new_row: dict):
    """
    같은 date가 있으면 overwrite (Actions 재실행 대비).
    없으면 append 후 date 오름차순 정렬.
    """
    existing_idx = next(
        (i for i, row in enumerate(history) if row.get("date") == new_row["date"]),
        None
    )
    if existing_idx is not None:
        history[existing_idx] = new_row
        print(f"[INFO] 기존 행 overwrite: {new_row['date']}")
    else:
        history.append(new_row)
        history.sort(key=lambda r: r["date"])
        print(f"[INFO] 신규 행 append: {new_row['date']} (총 {len(history)}행)")

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"[INFO] history.json 저장 완료")


# ─── 메인 ────────────────────────────────────────────────
def main():
    # 1. API 가용성 확인 (로그용 — history에 기록 안 함)
    api_check()

    # 2. portfolio.json 로드
    portfolio = load_portfolio()
    print(f"[INFO] 보유 KR {len(portfolio.get('holdings_kr', []))}종 / "
          f"US {len(portfolio.get('holdings_us', []))}종")

    # 3. 환율 조회
    fx_rate = fetch_fx_rate()
    if fx_rate is None:
        print("[WARN] 환율 조회 실패 → 기본값 1400 사용")
        fx_rate = 1400.0
    print(f"[INFO] 환율: {fx_rate} KRW/USD")

    # 4. 평가금액 산출
    result = calc_eval_amount(portfolio, fx_rate)

    # 5. 기록 행 구성
    new_row = {
        "date":       today_kst,
        "evalAmount": result["evalAmount"],
        "pnlRate":    result["pnlRate"],
        "fxRate":     result["fxRate"],
    }
    print(f"[INFO] 기록 행: {new_row}")

    # 6. history.json 업데이트
    history = load_history()
    save_history(history, new_row)

    print(f"\n[DONE] 스냅샷 완료 ✓")


if __name__ == "__main__":
    main()
