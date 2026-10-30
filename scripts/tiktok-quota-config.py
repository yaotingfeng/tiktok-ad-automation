#!/usr/bin/env python3
"""输出可审阅的SDK额度配置，不访问任何线上服务或凭据。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.integrations.tiktok.platform_quotas import LEVELS, quota_configuration

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--level",
    choices=tuple(LEVELS),
    required=True,
    help="生产须先核实My Apps实际档位；默认Basic仅可作明确标注的本地假设",
)
parser.add_argument(
    "--max-inflight",
    type=int,
    default=50,
    help="本地资源在途窗口，独立于平台QPS；部署前须验证容量（1–50）",
)
args = parser.parse_args()
try:
    print(
        json.dumps(
            quota_configuration(args.level, max_inflight=args.max_inflight), indent=2
        )
    )
except ValueError as error:
    parser.error(str(error))
