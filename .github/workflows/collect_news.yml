name: Collect News

# 뉴스 수집 자동화 (작업11 1단계)
# cron 타이밍 (UTC 기준 / KST = UTC+9):
#   - UTC 22:00 = KST 익일 07:00  (아침 브리핑 시점)
#   - UTC 13:00 = KST 당일 22:00  (밤 브리핑 시점)
# public 저장소이므로 GitHub-hosted 러너 무료 (분 제한 없음)

on:
  schedule:
    - cron: '0 22 * * *'   # KST 07:00
    - cron: '0 13 * * *'   # KST 22:00
  workflow_dispatch:        # 수동 트리거 (첫 실행·점검용)

permissions:
  contents: write           # news.json 커밋·푸시 권한

# 동시 실행 방지 (앞 실행 진행 중이면 취소)
concurrency:
  group: collect-news
  cancel-in-progress: false

jobs:
  collect:
    runs-on: ubuntu-latest
    steps:
      - name: 저장소 체크아웃
        uses: actions/checkout@v4

      - name: Python 설정
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: 의존성 설치
        run: pip install -r scripts/requirements.txt

      - name: 뉴스 수집 실행
        run: python scripts/collect_news.py

      - name: 변경 시에만 커밋·푸시
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          if [ -n "$(git status --porcelain news.json)" ]; then
            git add news.json
            git commit -m "chore: update news.json [skip ci]"
            git push
            echo "news.json 갱신·푸시 완료"
          else
            echo "변경 없음 — 커밋 건너뜀"
          fi
