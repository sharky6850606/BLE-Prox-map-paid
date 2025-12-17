import time
import requests
import os

WEB_INTERNAL_URL = os.getenv("WEB_INTERNAL_URL")
INTERNAL_SECRET = os.getenv("INTERNAL_SECRET")

INTERVAL_SECONDS = 60

print("[WORKER] Background evaluator started (1-minute interval)")

while True:
    try:
        r = requests.post(
            f"{WEB_INTERNAL_URL}/internal/evaluate",
            headers={"X-Internal-Secret": INTERNAL_SECRET},
            timeout=15
        )
        print("[WORKER] Evaluate status:", r.status_code)
    except Exception as e:
        print("[WORKER] ERROR:", e)

    time.sleep(INTERVAL_SECONDS)
