# Slack 웹훅 알림. 주소는 ~/.config/autoresearch/slack_webhook(권한 600) 또는 환경변수 SLACK_WEBHOOK_URL.
# 주소가 없거나 네트워크가 끊겨도 연구 실행은 멈추지 않는다.
import json
import os
import re
import unicodedata
import urllib.request
from pathlib import Path

WEBHOOK_FILE = Path.home() / ".config" / "autoresearch" / "slack_webhook"


def width(s):
    # 고정폭 글꼴에서의 표시 폭. 한글 같은 전각 문자는 2칸
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def to_mrkdwn(md):
    # 마크다운을 Slack mrkdwn으로: **굵게** → *굵게*, 제목 → 굵은 줄, [글](주소) → <주소|글>,
    # 표 → 칸을 맞춘 코드 블록 (Slack에는 표가 없다), ```python → ``` (언어 표시는 Slack에서 글자로 보인다)
    out, table, in_code = [], [], False

    def flush():
        rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in table
                if not re.fullmatch(r"\|?[\s:|-]+\|?", ln.strip())]
        table.clear()
        if not rows:
            return
        n = max(len(r) for r in rows)
        rows = [r + [""] * (n - len(r)) for r in rows]
        w = [max(width(r[i]) for r in rows) for i in range(n)]
        out.append("```")
        out.extend("  ".join(c + " " * (w[i] - width(c)) for i, c in enumerate(r)).rstrip() for r in rows)
        out.append("```")

    for line in md.splitlines():
        if line.strip().startswith("```"):
            flush()
            in_code = not in_code
            out.append("```")
            continue
        if in_code:
            out.append(line)
            continue
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        flush()
        line = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        line = re.sub(r"\[([^\]]+)\]\((\S+?)\)", r"<\2|\1>", line)
        out.append(line)
    flush()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def send(message):
    url = WEBHOOK_FILE.read_text().strip() if WEBHOOK_FILE.exists() else os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return False
    data = json.dumps({"text": to_mrkdwn(message)[:35000]}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


if __name__ == "__main__":
    md = "# 제목\n\n**굵게** 와 `코드`\n\n| 항목 | 값 |\n|---|---|\n| 시간 | 0.5초 |\n| guard | 통과 |\n\n```python\nx = 1\n```"
    got = to_mrkdwn(md)
    assert got.startswith("*제목*") and "*굵게*" in got and "**" not in got and "|" not in got, got
    assert "```\n항목   값\n시간   0.5초\nguard  통과\n```" in got and "```python" not in got, got
    print(got)
