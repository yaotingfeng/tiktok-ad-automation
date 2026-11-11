"""把已审查的额度档案转换成 Deployment；调用方负责停消费者、CAS及验收。"""

import copy
import re


def deployment_spec(deployment: dict, profile: dict) -> dict:
    spec = copy.deepcopy(deployment["spec"])
    role = deployment["metadata"]["labels"]["role"]
    selected = profile["workers"].get(role)
    if not selected:
        return spec
    pod = spec["template"]["spec"]
    container = pod["containers"][0]
    # 仅改显式 prefork 数，不重建原命令，保留队列、内存回收及任务退出参数。
    changed = 0
    for field in ("command", "args"):
        if field not in container:
            continue
        values = container[field]
        for i, value in enumerate(values):
            updated, count = re.subn(
                r"--concurrency=\d+",
                "--concurrency=" + str(selected["concurrency"]),
                value,
            )
            values[i] = updated
            changed += count
            if value in {"--concurrency", "-c"}:
                values[i + 1] = str(selected["concurrency"])
                changed += 1
    if changed != 1:
        raise ValueError("Expected exactly one explicit worker concurrency: " + role)
    container["resources"] = copy.deepcopy(selected["resources"])
    if selected.get("avoid_colocation_with"):
        terms = (
            pod.setdefault("affinity", {})
            .setdefault("podAntiAffinity", {})
            .setdefault("requiredDuringSchedulingIgnoredDuringExecution", [])
        )
        term = {
            "labelSelector": {
                "matchLabels": {"app": deployment["metadata"]["labels"]["app"]},
                "matchExpressions": [
                    {
                        "key": "role",
                        "operator": "In",
                        "values": selected["avoid_colocation_with"],
                    }
                ],
            },
            "topologyKey": "kubernetes.io/hostname",
        }
        if term not in terms:
            terms.append(term)
    return spec
