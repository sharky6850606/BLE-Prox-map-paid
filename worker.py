import time
from cron_evaluator import run_once


def main():
    print("[WORKER] Background evaluator started (1-minute interval)")
    while True:
        try:
            run_once()   # single evaluation tick
        except Exception as e:
            print(f"[WORKER] ERROR: {e}")
        time.sleep(60)   # run every 1 minute


if __name__ == "__main__":
    main()
