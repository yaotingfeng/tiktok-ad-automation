"""pytest 插件：逐条写出 nodeid、阶段、结果和耗时，供并行分组统计与重跑。"""

import json
import os


def pytest_runtest_logreport(report):
    path = os.environ.get("CI_RESULTS_FILE")
    if not path:
        return
    record = {
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "duration": report.duration,
    }
    with open(path, "a") as stream:
        stream.write(json.dumps(record) + "\n")
