name: 📸 Snapshot — 평가금액 누적

on:
  # KST 16:30 (UTC 07:30), 평일만
  # KOSPI 마감(15:30) + 데이터 안정화 1시간 후
  schedule:
    - cron: '30 7 * * 1-5'

  # 수동 실행 (첫 테스트·누락 보완용)
  workflow_dispatch:
    inputs:
      date_override:
        description: '날짜 강제 지정 (YYYY-MM-DD, 비우면 오늘 KST)'
        required: false
        default: ''

jobs:
  snapshot:
    name: Collect & Commit Snapshot
    runs-on: ubuntu-latest
    timeout-minutes: 10

    permissions:
      contents: write   # history.json 커밋 권한

    steps:
      # ── 1. 저장소 체크아웃 ──────────────────────────────
      - name: Checkout
        uses: actions/checkout@v4
        with:
          token: ${{ secrets.GITHUB_TOKEN }}

      # ── 2. Python 설정 ──────────────────────────────────
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      # ── 3. 의존성 설치 ──────────────────────────────────
      - name: Install dependencies
        run: pip install yfinance requests

      # ── 4. 날짜 강제 지정 처리 ─────────────────────────
      #   workflow_dispatch 에서 date_override 입력 시
      #   환경 변수로 전달 → collect_snapshot.py에서 참조
      - name: Set date override
        if: ${{ github.event.inputs.date_override != '' }}
        run: echo "SNAPSHOT_DATE=${{ github.event.inputs.date_override }}" >> $GITHUB_ENV

      # ── 5. 스냅샷 수집 ──────────────────────────────────
      - name: Run collect_snapshot.py
        run: python scripts/collect_snapshot.py

      # ── 6. 변경 감지 후 커밋 ────────────────────────────
      - name: Commit history.json
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add history.json
          # 변경 없으면 커밋 스킵 (Actions 재실행 시 동일 날짜 overwrite 후 diff 없을 수 있음)
          if git diff --cached --quiet; then
            echo "history.json 변경 없음 — 커밋 스킵"
          else
            git commit -m "snapshot $(date -u +'%Y-%m-%d') [bot]"
            git push
            echo "커밋 완료 ✓"
          fi
