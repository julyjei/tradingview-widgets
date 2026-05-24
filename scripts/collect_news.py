#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_news.py — 대시보드 뉴스 수집기 (작업11 / 1단계: 한국 아침 우선)

역할:
  RSS 피드를 수집하여 title·summary·link·pubDate만 추출하고,
  8종 카테고리 분류 + 보유/관심 종목 매칭 후 news.json으로 저장한다.
  (본문 크롤링 없음 — 저작권: 제목·요약·링크만 사용)

설계 원칙:
  - RSS 주소는 FEEDS 설정 영역에 분리. 일부 매체가 차단(403)되어도
    코드 본체를 수정하지 않고 주소만 교체/추가하면 된다.
  - 한 매체가 실패해도 나머지 매체로 계속 진행한다(부분 실패 허용).
  - 영향도(🔴🟡⚪)는 기계 판정하지 않는다. category·relatedStocks만 부여하고
    영향도 판단은 사람(브리핑) 및 후속 작업(News DB 연동)으로 위임한다.

실행: python scripts/collect_news.py
산출: 저장소 루트의 news.json
"""

import json
import sys
import time
import datetime
from pathlib import Path

import feedparser

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ════════════════════════════════════════════════════════════════
# 설정 영역 — 여기만 고치면 됩니다 (코드 본체는 건드릴 필요 없음)
# ════════════════════════════════════════════════════════════════

# 세션 구분 (morning=한국 우선 / evening=미국 우선)
SESSION = "morning"

# 수집 대상 RSS 피드.
#   - 일부 매체는 데이터센터 IP를 차단(403)할 수 있습니다.
#   - 첫 실행 로그에서 entries=0 / HTTP 403 으로 찍히는 피드는
#     아래 목록에서 빼거나 다른 주소로 교체하세요.
#   - market: 국내 / 미국 (카드 필터 분류에 사용)
FEEDS = [
    # ----- 한국경제 (증권·경제·IT) -----
    {"source": "한국경제", "market": "국내", "url": "https://www.hankyung.com/feed/finance"},
    {"source": "한국경제", "market": "국내", "url": "https://www.hankyung.com/feed/economy"},
    {"source": "한국경제", "market": "국내", "url": "https://www.hankyung.com/feed/it"},

    # ----- 연합뉴스 -----
    #   본사(yna.co.kr) RSS 경로는 변경 이력이 있어 미검증 상태입니다.
    #   첫 실행에서 403/0건이면 아래 주소를 최신 경로로 교체하세요.
    {"source": "연합뉴스", "market": "국내", "url": "https://www.yna.co.kr/rss/economy.xml"},
    {"source": "연합뉴스", "market": "국내", "url": "https://www.yna.co.kr/rss/market.xml"},
]

# 최근 N시간 이내 기사만 수집 (아침 브리핑은 전일~당일 새벽 포괄)
RECENT_HOURS = 24

# 카테고리별 최대 건수 컷 (과다 수집 방지)
MAX_PER_CATEGORY = 8

# 전체 최대 건수
MAX_TOTAL = 40

# HTTP 요청 헤더 (일부 매체의 봇 차단 완화용)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


# ════════════════════════════════════════════════════════════════
# 8종 카테고리 분류 규칙 (S_news_briefing 정합)
# ════════════════════════════════════════════════════════════════
# 위에서부터 순서대로 검사하여 첫 매칭 카테고리를 부여한다.
# (개별기업·실적 신호가 거시 신호보다 구체적이므로 먼저 검사)
CATEGORY_RULES = [
    ("실적·공시", [
        "실적", "영업이익", "순이익", "매출", "어닝", "공시", "배당",
        "분기", "잠정", "컨센서스", "가이던스", "자사주",
    ]),
    ("개별기업", [
        "삼성", "SK", "현대", "LG", "네이버", "카카오", "엔씨", "포스코",
        "셀트리온", "신규상장", "IPO", "인수", "합병", "M&A", "지분",
    ]),
    ("거시·통화정책", [
        "금리", "기준금리", "금통위", "한은", "한국은행", "연준", "FOMC", "Fed",
        "물가", "CPI", "PPI", "인플레", "환율", "원/달러", "국채", "통화정책",
        "경기", "GDP", "고용", "실업",
    ]),
    ("정세·지정학", [
        "관세", "무역", "수출규제", "제재", "지정학", "전쟁", "분쟁",
        "중동", "우크라", "대만", "북한", "외교", "정상회담", "선거",
    ]),
    ("산업·섹터", [
        "반도체", "AI", "인공지능", "2차전지", "배터리", "전기차", "바이오",
        "방산", "조선", "철강", "화학", "자동차", "디스플레이", "게임",
        "플랫폼", "클라우드", "데이터센터", "원전",
    ]),
    ("시장흐름", [
        "코스피", "코스닥", "증시", "지수", "외국인", "기관", "수급",
        "나스닥", "다우", "S&P", "뉴욕증시", "상승", "하락", "급등", "급락",
    ]),
    # 위 어디에도 안 걸리면 신규발굴로 분류
    ("신규발굴", []),
]


def classify_category(text: str) -> str:
    """제목+요약 텍스트를 8종 카테고리 중 하나로 분류."""
    for category, keywords in CATEGORY_RULES:
        if not keywords:  # 신규발굴 (fallthrough)
            return category
        for kw in keywords:
            if kw.lower() in text.lower():
                return category
    return "신규발굴"


# ════════════════════════════════════════════════════════════════
# 보유/관심 종목 매칭 (portfolio.json 연동)
# ════════════════════════════════════════════════════════════════
def load_watch_stocks(repo_root: Path):
    """
    portfolio.json에서 보유 종목명을 읽어 매칭 사전을 만든다.
    파일이 없거나 읽기 실패해도 빈 목록으로 안전하게 진행한다.
    반환: [{"name": 표기명, "aliases": [별칭들]}, ...]
    """
    # 종목명 별칭 (RSS 제목에 다른 표기로 등장하는 경우 대비)
    ALIAS_MAP = {
        "삼성전자": ["삼성전자", "삼전"],
        "NAVER": ["NAVER", "네이버"],
        "카카오": ["카카오"],
        "엔씨소프트": ["엔씨소프트", "엔씨", "NC소프트"],
        "애플": ["애플", "Apple", "아이폰"],
        "엔비디아": ["엔비디아", "Nvidia", "NVDA"],
    }

    stocks = []
    pf_path = repo_root / "portfolio.json"
    try:
        with open(pf_path, encoding="utf-8") as f:
            pf = json.load(f)
        names = []
        for key in ("holdings_kr", "holdings_us"):
            for h in pf.get(key, []):
                if h.get("name"):
                    names.append(h["name"])
        for nm in names:
            aliases = ALIAS_MAP.get(nm, [nm])
            stocks.append({"name": nm, "aliases": aliases})
        print(f"  · 보유 종목 {len(stocks)}종 로드: {', '.join(n['name'] for n in stocks)}")
    except Exception as e:
        print(f"  · portfolio.json 로드 실패 ({e}) → 종목 매칭 생략")
    return stocks


def match_stocks(text: str, watch_stocks) -> list:
    """텍스트에서 보유 종목 별칭을 찾아 표기명 리스트로 반환."""
    matched = []
    for stock in watch_stocks:
        for alias in stock["aliases"]:
            if alias.lower() in text.lower():
                matched.append(stock["name"])
                break
    return matched


# ════════════════════════════════════════════════════════════════
# RSS 수집
# ════════════════════════════════════════════════════════════════
def fetch_feed(feed_cfg: dict):
    """단일 RSS 피드를 받아 feedparser 결과를 반환. 실패 시 None."""
    url = feed_cfg["url"]
    # 1차: requests로 헤더 붙여 받기 (가능한 경우)
    if HAS_REQUESTS:
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=15)
            if r.status_code == 200:
                return feedparser.parse(r.content)
            else:
                print(f"  ✗ [{feed_cfg['source']}] HTTP {r.status_code} — {url}")
                return None
        except Exception as e:
            print(f"  ✗ [{feed_cfg['source']}] 요청 실패 ({str(e)[:40]}) — {url}")
            # 2차 폴백으로 넘어감
    # 2차: feedparser 직접 (requests 없거나 1차 실패 시)
    try:
        d = feedparser.parse(url, agent=HTTP_HEADERS["User-Agent"])
        if d.entries:
            return d
        print(f"  ✗ [{feed_cfg['source']}] 0건 — {url}")
        return None
    except Exception as e:
        print(f"  ✗ [{feed_cfg['source']}] 파싱 실패 ({str(e)[:40]}) — {url}")
        return None


def parse_pubdate(entry):
    """entry의 발행시각을 KST ISO 문자열로. 실패 시 현재시각."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        dt_utc = datetime.datetime.fromtimestamp(time.mktime(t), tz=datetime.timezone.utc)
        kst = dt_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
        return kst, kst.isoformat(timespec="seconds")
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return now, now.isoformat(timespec="seconds")


def clean_summary(raw: str, limit: int = 200) -> str:
    """요약에서 HTML 태그를 제거하고 길이를 제한 (본문 아님, 매체 발췌)."""
    import re
    txt = re.sub(r"<[^>]+>", "", raw or "").strip()
    txt = re.sub(r"\s+", " ", txt)
    return txt[:limit]


# ════════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════════
def main():
    repo_root = Path(__file__).resolve().parent.parent  # scripts/ 의 부모 = 루트
    print(f"뉴스 수집 시작 — session={SESSION} / 대상 {len(FEEDS)}개 피드")

    watch_stocks = load_watch_stocks(repo_root)

    now_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    cutoff = now_kst - datetime.timedelta(hours=RECENT_HOURS)

    seen_links = set()
    items = []
    ok_feeds = 0

    for feed_cfg in FEEDS:
        d = fetch_feed(feed_cfg)
        if not d or not d.entries:
            continue
        ok_feeds += 1
        cnt = 0
        for e in d.entries:
            link = e.get("link", "").strip()
            if not link or link in seen_links:
                continue
            pub_dt, pub_iso = parse_pubdate(e)
            if pub_dt < cutoff:
                continue  # 24h 이전 기사 제외

            title = (e.get("title") or "").strip()
            summary = clean_summary(e.get("summary", ""))
            if not title:
                continue

            blob = f"{title} {summary}"
            category = classify_category(blob)
            related = match_stocks(blob, watch_stocks)

            items.append({
                "title": title,
                "summary": summary,
                "link": link,
                "pubDate": pub_iso,
                "category": category,
                "market": feed_cfg["market"],
                "relatedStocks": related,
                "source": feed_cfg["source"],
            })
            seen_links.add(link)
            cnt += 1
        print(f"  ✓ [{feed_cfg['source']}] {cnt}건 수집 — {feed_cfg['url']}")

    # 최신순 정렬
    items.sort(key=lambda x: x["pubDate"], reverse=True)

    # 카테고리별 컷 적용
    per_cat = {}
    capped = []
    for it in items:
        c = it["category"]
        per_cat[c] = per_cat.get(c, 0) + 1
        if per_cat[c] <= MAX_PER_CATEGORY:
            capped.append(it)
    capped = capped[:MAX_TOTAL]

    output = {
        "lastUpdated": now_kst.isoformat(timespec="seconds"),
        "session": SESSION,
        "feedsOk": ok_feeds,
        "feedsTotal": len(FEEDS),
        "count": len(capped),
        "items": capped,
    }

    out_path = repo_root / "news.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n완료 — {len(capped)}건 저장 (피드 {ok_feeds}/{len(FEEDS)} 성공)")
    print(f"  → {out_path}")

    # 카테고리 분포 요약
    if capped:
        dist = {}
        for it in capped:
            dist[it["category"]] = dist.get(it["category"], 0) + 1
        print("  카테고리 분포:", ", ".join(f"{k} {v}" for k, v in dist.items()))

    # 피드가 하나도 성공하지 못하면 비정상 종료 (Actions에서 빈 커밋 방지)
    if ok_feeds == 0:
        print("\n경고: 성공한 피드가 없습니다. RSS 주소를 점검하세요.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
