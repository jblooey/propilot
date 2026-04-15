#!/usr/bin/env python3
"""
pp_pusher.py — runs on your Mac, fetches PrizePicks every 10 minutes
and pushes the data to the server so the runner can use it.

Run with: python3 pp_pusher.py
"""

import time
import requests
from prizepicks import get_prizepicks_nba, get_prizepicks_mlb

SERVER_NBA = "https://flypropilot.app/api/pp-props"
SERVER_MLB = "https://flypropilot.app/api/pp-mlb-props"
SECRET     = "propilot-pp-secret"
INTERVAL   = 300  # 5 minutes


def push_nba():
    try:
        players = get_prizepicks_nba()
        if not players:
            print(f"  [PP NBA] No players fetched, skipping push")
            return
        r = requests.post(
            SERVER_NBA,
            json=players,
            headers={"X-PP-Secret": SECRET},
            timeout=10,
        )
        print(f"  [PP NBA] Pushed {len(players)} players → {r.status_code} {r.json()}")
    except Exception as e:
        print(f"  [PP NBA] Error: {e}")


def push_mlb():
    try:
        players = get_prizepicks_mlb()
        if not players:
            print(f"  [PP MLB] No players fetched, skipping push")
            return
        r = requests.post(
            SERVER_MLB,
            json=players,
            headers={"X-PP-Secret": SECRET},
            timeout=10,
        )
        print(f"  [PP MLB] Pushed {len(players)} players → {r.status_code} {r.json()}")
    except Exception as e:
        print(f"  [PP MLB] Error: {e}")


if __name__ == "__main__":
    print("PP Pusher started — pushing NBA + MLB every 10 minutes")
    while True:
        push_nba()
        push_mlb()
        time.sleep(INTERVAL)
