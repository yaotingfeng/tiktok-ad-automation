#!/usr/bin/env python3
"""只读检查 Celery 全部优先级队列；连接由 REDIS_URL 环境变量提供。"""

import json
import os
import sys

from redis import Redis

QUEUES = ("resources", "resource-results", "builds", "control", "ad-management",
          "ads-directory", "ads-publish", "ads-reporting", "ads-backfill",
          "ads-live-directory")


def inspect_queues(client):
    depths = {}
    for queue in QUEUES:
        depths[queue] = sum(client.llen(queue if p == 0 else f"{queue}\x06\x16{p}")
                            for p in (0, 3, 6, 9))
    return {"queues": depths, "queued": sum(depths.values()),
            "unacked": client.hlen("unacked")}


def main():
    url = os.environ.get("REDIS_URL")
    if not url:
        raise SystemExit("需要 REDIS_URL 环境变量")
    try:
        with Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5) as client:
            print(json.dumps(inspect_queues(client), ensure_ascii=False))
    except Exception as error:
        print(f"队列读取失败：{type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
