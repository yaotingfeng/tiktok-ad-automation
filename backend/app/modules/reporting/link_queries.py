"""任务内复用旧帖定位查询；只缓存完整集合，部分页不能证明帖子不存在。"""

import hashlib
import json
from typing import Any

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage

CACHE_SECONDS = 300
MAX_ENTRIES = 128
MAX_CACHE_BYTES = 512 * 1024
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_TARGETS = 10_000


def material_identity_key(mid: str, kind: str) -> str:
    """JSON 字符串可直接保存在现有任务续点中，不混淆同号不同命名空间。"""
    return json.dumps([mid, kind])


def row_identity_key(row: dict[str, Any]) -> str:
    dimensions = row.get("dimensions") or {}
    mid, kind = dimensions.get("main_material_id"), dimensions.get("main_material_type")
    if not isinstance(mid, str) or not mid or not isinstance(kind, str) or not kind:
        raise DomainError("report_response_invalid", "定位回执缺少主素材 ID 或类型")
    return material_identity_key(mid, kind)


def identity_ids(keys) -> set[str]:
    return {json.loads(key)[0] for key in keys}


class QueryMembershipReader:
    def __init__(self, read, cache, *, targets, now):
        self.read, self.cache = read, cache
        self.now = now.timestamp()
        # 旧成员只有 ID，既不能证明命中，也不能证明某种类型不存在。
        if cache.get("typed") is not True:
            cache.clear()
            cache["typed"] = True
        targets = set(targets)
        # progress 只属于一个不可变租户/BC/账户/冻结路由任务；新增目标不能借用
        # 此前只记录旧目标的集合，否则会把未收集的帖子误判为不存在。
        if (
            len(targets) > MAX_TARGETS
            or len(json.dumps(sorted(targets)).encode()) > MAX_CACHE_BYTES // 2
        ):
            # 超大集合停用复用，仍保留当前分页证据继续读取，不能每页清游标。
            cache["targets"], cache["entries"] = [], {}
        elif not targets <= set(cache.get("targets", [])):
            cache.clear()
            cache["typed"] = True
            cache["targets"] = sorted(targets)
        self.targets = set(cache.setdefault("targets", []))
        self.entries = cache.setdefault("entries", {})
        for key, value in list(self.entries.items()):
            if self.now - value["at"] >= CACHE_SECONDS:
                del self.entries[key]

    def __call__(self, ads, first, last, page):
        key = hashlib.sha256(
            json.dumps([sorted(ads), str(first), str(last)]).encode()
        ).hexdigest()
        # 长分页可能刚在本 reader 中完成；完整集合仍从第一页观察起点计时，
        # 不能仅靠构造时清缓存，把刚完成的旧负证据当成新的五分钟命中。
        if key in self.entries and self.now - self.entries[key]["at"] >= CACHE_SECONDS:
            del self.entries[key]
        if page == 1 and key in self.entries:
            members = self.entries[key]["members"]
            return RawReportPage(
                tuple(
                    {
                        "dimensions": {
                            "main_material_id": mid,
                            "main_material_type": kind,
                        }
                    }
                    for mid, kind in map(json.loads, members)
                ),
                1,
                1 if members else 0,
                len(members),
            )
        result = self.read(ads, first, last, page)
        if result.total_number >= 20_000:
            self.cache.pop("pending", None)
            return result  # 由定位器拆广告子集；截断结果绝不缓存成完整集合。
        signature = [result.total_page, result.total_number]
        pending: dict[str, Any] | None = self.cache.get("pending")
        if page == 1:
            pending = {
                "key": key,
                "at": self.now,
                "next_page": 1,
                "signature": signature,
                "seen": [],
                "members": [],
            }
        elif pending is None or pending["key"] != key:
            # 升级时已经在第2页的旧游标没有完整集合证据，先重启当前查询。
            raise DomainError("report_snapshot_changed", "定位查询缺少完整分页证据")
        assert pending is not None
        if pending["signature"] != signature or pending["next_page"] != page:
            raise DomainError("report_snapshot_changed", "定位查询分页发生变化")
        # 部分页是续点而非可复用负缓存。排队时长本身不证明平台集合变化；
        # 保留范围、总数、页次和下方重复维度校验，完整缓存仍沿原 at 过期。
        keys = [
            hashlib.sha256(
                json.dumps(row.get("dimensions") or {}, sort_keys=True).encode()
            ).hexdigest()
            for row in result.rows
        ]
        if len(keys) != len(set(keys)):
            raise DomainError("report_row_invalid", "定位查询单页有重复维度")
        if set(keys) & set(pending["seen"]):
            raise DomainError("report_snapshot_changed", "定位查询跨页重复")
        pending["seen"].extend(keys)
        pending["members"] = sorted(
            set(pending["members"])
            | {row_identity_key(row) for row in result.rows} & self.targets
        )
        if result.next_page is not None:
            pending["next_page"] = result.next_page
            self.cache["pending"] = pending
            self._trim(MAX_STATE_BYTES)
            if len(json.dumps(self.cache).encode()) > MAX_STATE_BYTES:
                raise DomainError(
                    "report_truncation_risk", "定位查询分页证据超过容量边界"
                )
        else:
            self.cache.pop("pending", None)
            if self.targets:
                self.entries[key] = {
                    "at": pending["at"],
                    "members": pending["members"],
                }
                # 淘汰整条查询，不能截断成员集合后错误地签发否定证据。
                self._trim(MAX_CACHE_BYTES)
        return result

    def _trim(self, maximum):
        while self.entries and (
            len(self.entries) > MAX_ENTRIES
            or len(json.dumps(self.cache).encode()) > maximum
        ):
            oldest = min(self.entries, key=lambda k: self.entries[k]["at"])
            del self.entries[oldest]
