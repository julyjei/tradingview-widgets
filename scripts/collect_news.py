#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_news.py — 대시보드 TODAY'S NEWS용 news.json 생성기

설계 결정 (작업11 / 2026-05-25 확정):
  - 데이터 원천 : RSS 직수집 (Notion 무관·토큰 불필요)
  - 출처 전략   : Google News RSS (8종 카테고리 키워드 쿼리, 데이터센터 IP 안정)
  - 갱신 방식   : 매 실행 전체 교체, 최신 N건만 유지 (스냅샷)
  - 저작권      : 제목·요약(RSS description 절단)·링크만. 본문 전문 절대 금지
  - 카테고리    : S_news_db_update 8종과 정합

권위 위임:
  - 영향도(🔴🟡⚪) 판정은 본 스크립트가 하지 않는다 (G_investing_notion_guide 소관).
    본 스크립트는 category·market 분류까지만 수행한다.
"""

import feedparser
import json
import re
import sys
import html
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
MAX_ITEMS = 30          # news.json에 유지할 최대 건수 (클라이언트 최근 10건 + 여유분)
MAX_SUMMARY_CHARS = 180 # 요약 절단 길이 (저작권 가드: 본문 대체 금지)
PER_FEED_LIMIT = 8      # 피드당 최대 수집 건수 (특정 출처 쏠림 방지)
REQUEST_TIMEOUT = 15    # feedparser 자체 타임아웃 의존 + agent 지정

# Google News RSS 기본형: 한국어/한국 지역
GNEWS_BASE = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"

# 8종 카테고리 → 검색 쿼리 매핑
# (S_news_db_update 8종: 일간브리핑·거시통화정책·산업섹터·개별기업·
#  실적공시·정세지정학·시장흐름·신규발굴)
# 일간브리핑/신규발굴은 RSS 수집 대상이 아님(브리핑은 생성물, 신규발굴은 리서치).
# 따라서 RSS는 나머지 6종 + 보유종목 트랙으로 구성한다.
CATEGORY_QUERIES = [
    # (category, market, query, when_filter)
    ("거시·통화정책", "국내", "한국은행 기준금리 OR 물가 OR 환율", "when:2d"),
    ("거시·통화정책", "미국", "연준 FOMC OR 미국 CPI OR 금리", "when:2d"),
    ("산업·섹터",     "국내", "반도체 OR 2차전지 OR 플랫폼 규제", "when:2d"),
    ("정세·지정학",   "글로벌", "미중 무역 OR 관세 OR 지정학 리스크", "when:2d"),
    ("시장흐름",      "국내", "코스피 OR 코스닥 OR 외국인 수급", "when:1d"),
    ("시장흐름",      "미국", "나스닥 OR S&P500 OR 뉴욕증시", "when:1d"),
    ("실적·공시",     "글로벌", "분기 실적 OR 어닝 OR 자사주", "when:2d"),
]

# 보유 종목 트랙 (개별기업 카테고리) — portfolio.json에서 동적 로드
# 종목명 기준 검색. 티커가 아닌 한글/영문명으로 쿼리.
HOLDINGS_TRACK_CATEGORY = "개별기업"


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def clean_text(raw: str) -> str:
    """HTML 태그·엔티티 제거 후 공백 정리. 저작권 가드 절단."""
    if not raw:
        return ""
    # HTML 태그 제거
    text = re.sub(r"<[^>]+>", "", raw)
    # HTML 엔티티 디코드
    text = html.unescape(text)
    # 연속 공백 정리
    text = re.sub(r"\s+", " ", text).strip()
    # 저작권 가드: 본문 대체 방지를 위해 길이 절단
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS].rstrip() + "…"
    return text


def parse_date(entry) -> str:
    """엔트리에서 발행일 추출 → YYYY-MM-DD (KST). 실패 시 오늘."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc).astimezone(KST)
                return dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                continue
    return datetime.now(KST).strftime("%Y-%m-%d")


def extract_source(entry, feed) -> str:
    """출처명 추출. Google News는 entry.source.title에 원 출처 보존."""
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return src["title"]
    # 폴백: 제목 끝의 ' - 출처명' 패턴
    title = entry.get("title", "")
    m = re.search(r" - ([^-]+)$", title)
    if m:
        return m.group(1).strip()
    return feed.feed.get("title", "출처 미상")


def strip_source_suffix(title: str) -> str:
    """Google News 제목 끝 ' - 출처명' 제거."""
    return re.sub(r" - [^-]+$", "", title).strip()


def fetch_feed(query: str, when_filter: str = "") -> feedparser.FeedParserDict:
    """Google News RSS 1건 파싱. when 필터로 최근 기사만."""
    q = query
    if when_filter:
        q = f"{query} {when_filter}"
    url = GNEWS_BASE.format(q=quote(q))
    # feedparser는 agent 지정 가능 — 데이터센터 차단 완화
    return feedparser.parse(url, agent="Mozilla/5.0 (compatible; DashboardBot/1.0)")


def load_holdings_queries() -> list:
    """portfolio.json에서 보유 종목명을 읽어 개별기업 쿼리 생성."""
    queries = []
    try:
        with open("portfolio.json", "r", encoding="utf-8") as f:
            pf = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[warn] portfolio.json 로드 실패, 보유종목 트랙 생략: {e}", file=sys.stderr)
        return queries

    for h in pf.get("holdings_kr", []):
        name = h.get("name")
        if name:
            queries.append((HOLDINGS_TRACK_CATEGORY, "국내", name, "when:2d"))
    for h in pf.get("holdings_us", []):
        name = h.get("name")
        if name:
            queries.append((HOLDINGS_TRACK_CATEGORY, "미국", name, "when:2d"))
    return queries


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def collect() -> dict:
    all_queries = list(CATEGORY_QUERIES) + load_holdings_queries()
    items = []
    seen_links = set()
    feed_ok = 0
    feed_fail = 0

    for category, market, query, when_filter in all_queries:
        try:
            feed = fetch_feed(query, when_filter)
            if feed.bozo and not feed.entries:
                # 파싱 오류 + 결과 0건 → 실패로 간주
                print(f"[warn] 피드 파싱 실패: {category}/{query}", file=sys.stderr)
                feed_fail += 1
                continue
            feed_ok += 1

            count = 0
            for entry in feed.entries:
                if count >= PER_FEED_LIMIT:
                    break
                link = entry.get("link", "")
                if not link or link in seen_links:
                    continue
                seen_links.add(link)

                title = strip_source_suffix(entry.get("title", ""))
                if not title:
                    continue

                items.append({
                    "title": title,
                    "summary": clean_text(entry.get("summary", "")),
                    "link": link,
                    "source": extract_source(entry, feed),
                    "category": category,
                    "market": market,
                    "pubDate": parse_date(entry),
                })
                count += 1

        except Exception as e:
            print(f"[warn] 피드 처리 예외: {category}/{query} — {e}", file=sys.stderr)
            feed_fail += 1
            continue

    # 최신순 정렬 후 상위 N건
    items.sort(key=lambda x: x["pubDate"], reverse=True)
    items = items[:MAX_ITEMS]

    print(f"[info] 피드 성공 {feed_ok} / 실패 {feed_fail} / 수집 {len(items)}건",
          file=sys.stderr)

    return {
        "lastUpdated": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "itemCount": len(items),
        "feedOk": feed_ok,
        "feedFail": feed_fail,
        "items": items,
    }


def main():
    result = collect()

    # 부분실패 가드: 단 한 건도 수집 못 하면 기존 json 보존 (커밋 안 함)
    if result["itemCount"] == 0:
        print("[error] 수집 0건 — 기존 news.json 보존, 커밋하지 않음", file=sys.stderr)
        sys.exit(1)

    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[done] news.json 생성 완료 ({result['itemCount']}건)", file=sys.stderr)


if __name__ == "__main__":
    main()
