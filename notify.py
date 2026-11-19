# Slack 웹훅 알림. 주소는 ~/.config/autoresearch/slack_webhook(권한 600) 또는 환경변수 SLACK_WEBHOOK_URL.
# 주소가 없거나 네트워크가 끊겨도 연구 실행은 멈추지 않는다.
import json
import os
import urllib.request
from pathlib import Path

WEBHOOK_FILE = Path.home() / ".config" / "autoresearch" / "slack_webhook"


def send(message):
    url = WEBHOOK_FILE.read_text().strip() if WEBHOOK_FILE.exists() else os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return False
    data = json.dumps({"text": message[:35000]}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False
