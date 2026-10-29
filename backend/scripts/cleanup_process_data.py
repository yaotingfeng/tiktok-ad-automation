# ruff: noqa: T201
"""人工运行一次有界清理；默认只读统计，--apply 才执行维护。"""

import argparse

from app.jobs.maintenance_tasks import maintain_process_data
from scripts.verify_cleanup import verify_cleanup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行一次有界维护")
    args = parser.parse_args()
    if args.apply:
        print(maintain_process_data())
    verify_cleanup()


if __name__ == "__main__":
    main()
