"""在应用镜像内执行；输出只进入发布私有目录，不打印凭据。"""

import base64
import json
import sys
import time


def credentials():
    """只统计可解密的加密凭据数；候选迁移后用，避免再次对全部表计数。"""
    return {
        "credentials_decrypted": fingerprint(count_rows=False)["credentials_decrypted"]
    }


def fingerprint(count_rows=True):
    from app.core.config import settings
    from app.core.credentials import decrypt_credentials
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import NullPool

    engine = create_engine(str(settings.DATABASE_URL), poolclass=NullPool)
    with engine.connect() as connection:
        inspector = inspect(connection)
        names = inspector.get_table_names(schema="public")
        quote = engine.dialect.identifier_preparer.quote
        rows = (
            {
                n: connection.execute(
                    text("SELECT count(*) FROM public." + quote(n))
                ).scalar_one()
                for n in names
            }
            if count_rows
            else {}
        )
        decrypted = 0
        for name in names:
            columns = {c["name"] for c in inspector.get_columns(name, schema="public")}
            if {"tenant_id", "credential_ciphertext"} <= columns:
                for row in connection.execute(
                    text(
                        "SELECT tenant_id, credential_ciphertext FROM public."
                        + quote(name)
                        + " WHERE credential_ciphertext IS NOT NULL"
                    )
                ):
                    if row.credential_ciphertext:
                        decrypt_credentials(
                            tenant_id=row.tenant_id,
                            ciphertext=row.credential_ciphertext,
                        )
                        decrypted += 1
        return {
            "migration_heads": sorted(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            ),
            "table_rows": rows,
            "credentials_decrypted": decrypted,
        }


def redis_logical(client, key, kind):
    """按内容规范化，避免不同 Redis 版本的 DUMP 编码造成假失败。"""

    def enc(value):
        return base64.b64encode(value).decode()

    if kind == "string":
        value = client.get(key)
        return enc(value) if value is not None else None
    if kind == "list":
        return [enc(v) for v in client.lrange(key, 0, -1)]
    if kind == "set":
        return sorted(enc(v) for v in client.smembers(key))
    if kind == "hash":
        return sorted([enc(k), enc(v)] for k, v in client.hgetall(key).items())
    if kind == "zset":
        return [
            [enc(v), score.hex()]
            for v, score in client.zrange(key, 0, -1, withscores=True)
        ]
    raise RuntimeError("Unsupported Redis key type: " + kind)


def redis_export():
    import redis
    from app.core.config import settings

    client = redis.Redis.from_url(settings.REDIS_URL)
    if client.connection_pool.connection_kwargs.get("db") != 15:
        raise RuntimeError("Source Redis must be DB 15")
    records = []

    def enc(value):
        return base64.b64encode(value).decode()

    # 应用已停写；只读取本逻辑库，不调用共享实例 SAVE/FLUSH。
    for key in sorted(set(client.scan_iter())):
        kind, value, ttl = (
            client.pipeline(transaction=True).type(key).dump(key).pttl(key).execute()
        )
        if value is None or ttl == -2:
            continue
        expires = int(time.time() * 1000) + ttl if ttl >= 0 else None
        kind = kind.decode()
        logical = redis_logical(client, key, kind)
        # 冻结窗口无写入，但短期键仍可自然过期。
        if not client.exists(key):
            continue
        records.append(
            {
                "key": enc(key),
                "dump": enc(value),
                "expires_at_ms": expires,
                "type": kind,
                "logical": logical,
            }
        )
    return records


def migrate_targeted_messages(
    client,
    *,
    token,
    manifest_sha256,
    priority_steps=(0, 3, 6, 9),
    sep="\x06\x16",
    key_prefix="",
):
    """冻结且已验证备份后迁移；WATCH 防并发，队列与幂等凭据在同一事务提交。"""
    import hashlib
    import re

    if not re.fullmatch(r"[a-f0-9]{32}", token) or not re.fullmatch(
        r"[a-f0-9]{64}", manifest_sha256
    ):
        raise RuntimeError("Invalid broker migration identity")
    source, target = "ad-management", "ads-live-directory"
    receipt_key = key_prefix + "tkada:release:targeted-queue:" + token
    pairs = [
        (
            key_prefix + source + (sep + str(p) if p else ""),
            key_prefix + target + (sep + str(p) if p else ""),
        )
        for p in priority_steps
    ]
    keys = [key for pair in pairs for key in pair]
    unacked = [key_prefix + "unacked", key_prefix + "unacked_index"]

    def checksum(items):
        # 长度前缀消除不同消息边界产生相同拼接值的歧义。
        digest = hashlib.sha256()
        for item in items:
            digest.update(len(item).to_bytes(8, "big"))
            digest.update(item)
        return digest.hexdigest()

    def verify_receipt(receipt):
        readback = client.pipeline(transaction=True)
        for queue in receipt["queues"]:
            readback.lrange(queue["source"], 0, -1)
            readback.lrange(queue["target"], 0, -1)
        values = readback.execute()
        for index, queue in enumerate(receipt["queues"]):
            for offset, side in enumerate(("source", "target")):
                observed = values[index * 2 + offset]
                if (
                    len(observed) != queue[side + "_after"]
                    or checksum(observed) != queue[side + "_after_sha256"]
                ):
                    raise RuntimeError(
                        "Broker migration readback mismatch; keep consumers frozen"
                    )
        return dict(receipt, verified=True)

    with client.pipeline() as pipe:
        pipe.watch(receipt_key, *keys, *unacked)
        previous = pipe.get(receipt_key)
        if previous:
            receipt = json.loads(previous)
            if receipt["manifest_sha256"] != manifest_sha256:
                raise RuntimeError("Broker migration backup identity changed")
            return verify_receipt(receipt)
        if pipe.hlen(unacked[0]) or pipe.zcard(unacked[1]):
            raise RuntimeError("Broker deliveries must be fully acknowledged")
        # Kombu 优先级键必须来自当前 transport 合同，不能遗漏自定义优先级的积压。
        known = {k.encode() for k in keys}
        for queue in (source, target):
            discovered = set(client.scan_iter(match=key_prefix + queue + sep + "*"))
            if not discovered <= known:
                raise RuntimeError("Unexpected Kombu priority queue keys")
        updates, receipt = (
            [],
            {
                "token": token,
                "manifest_sha256": manifest_sha256,
                "moved": 0,
                "queues": [],
            },
        )
        for old_key, new_key in pairs:
            for key in (old_key, new_key):
                if pipe.type(key) not in (b"none", b"list") or pipe.pttl(key) >= 0:
                    raise RuntimeError("Broker queue type or expiry is unexpected")
            before, existing = pipe.lrange(old_key, 0, -1), pipe.lrange(new_key, 0, -1)
            retained, moved = [], []
            for raw in before:
                message = json.loads(raw)
                if message.get("headers", {}).get("task") != "ads.targeted_step":
                    retained.append(raw)
                    continue
                # body、ID、其余 header 原值保留；重试/拒绝重排必须继续回到新读取队列。
                delivery = message.get("properties", {}).get("delivery_info", {})
                if (
                    delivery.get("routing_key") != source
                    or delivery.get("exchange") != ""
                ):
                    raise RuntimeError("Targeted message routing does not match source")
                delivery["routing_key"] = target
                headers = message["headers"]
                if "routing_key" in headers:
                    if headers["routing_key"] != source:
                        raise RuntimeError(
                            "Targeted header routing does not match source"
                        )
                    headers["routing_key"] = target
                moved.append(
                    json.dumps(
                        message, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                )
            # Redis LPUSH/BRPOP：已有目标消息先消费，迁入消息内部与源队列原顺序相同。
            after = moved + existing
            receipt["moved"] += len(moved)
            receipt["queues"].append(
                {
                    "source": old_key,
                    "target": new_key,
                    "moved": len(moved),
                    "source_before": len(before),
                    "source_after": len(retained),
                    "target_before": len(existing),
                    "target_after": len(after),
                    "source_before_sha256": checksum(before),
                    "source_after_sha256": checksum(retained),
                    "target_before_sha256": checksum(existing),
                    "target_after_sha256": checksum(after),
                }
            )
            if moved:
                updates.append((old_key, retained, new_key, after))
        pipe.multi()
        for old_key, retained, new_key, after in updates:
            pipe.delete(old_key, new_key)
            if retained:
                pipe.rpush(old_key, *retained)
            pipe.rpush(new_key, *after)
        pipe.set(receipt_key, json.dumps(receipt))
        pipe.execute()
    # 原子凭据也写回调用方私有目录；断流后同 token 重试不会重复搬运。
    return verify_receipt(receipt)


def migrate_targeted(expected):
    import redis
    from app.core.config import settings
    from app.jobs.celery_app import celery_app

    client = redis.Redis.from_url(settings.REDIS_URL)
    if client.connection_pool.connection_kwargs.get("db") != 15:
        raise RuntimeError("Source Redis must be DB 15")
    options = celery_app.conf.broker_transport_options or {}
    if (
        options.get("unacked_key", "unacked") != "unacked"
        or options.get("unacked_index_key", "unacked_index") != "unacked_index"
    ):
        raise RuntimeError("Custom unacked keys require release review")
    return migrate_targeted_messages(
        client,
        **expected,
        priority_steps=options.get("priority_steps", (0, 3, 6, 9)),
        sep=options.get("sep", "\x06\x16"),
        key_prefix=options.get("global_keyprefix", ""),
    )


def worker_control(action, expected):
    from app.jobs.celery_app import celery_app

    inspect = celery_app.control.inspect(destination=expected, timeout=5)
    queues = inspect.active_queues() or {}
    if set(queues) != set(expected):
        raise RuntimeError("Not all expected workers replied")
    if action == "cancel":
        for worker, subscriptions in queues.items():
            for queue in subscriptions:
                replies = celery_app.control.cancel_consumer(
                    queue["name"], destination=[worker], reply=True, timeout=5
                )
                if not any(worker in r and "ok" in r[worker] for r in replies):
                    raise RuntimeError("Consumer cancellation was not acknowledged")
    result = {}
    for kind in ("active", "reserved", "scheduled", "active_queues"):
        result[kind] = getattr(inspect, kind)() or {}
    return result


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "fingerprint":
        result = fingerprint()
    elif action == "credentials":
        result = credentials()
    elif action == "redis":
        # 积压时 Redis 可达数十 MB，经 exec 标准输出回传会断流；先写入容器文件，
        # 再由发布端带重试与校验和复制，标准输出只返回条数。
        records = redis_export()
        with open(json.loads(sys.argv[2]), "w") as stream:
            json.dump(records, stream)
        result = {"records": len(records)}
    elif action == "migrate-targeted":
        result = migrate_targeted(json.loads(sys.argv[2]))
    elif action == "broker":
        import redis
        from app.core.config import settings

        client = redis.Redis.from_url(settings.REDIS_URL)
        result = {"unacked": client.hlen("unacked") + client.zcard("unacked_index")}
    else:
        result = worker_control(action, json.loads(sys.argv[2]))
    print(json.dumps(result))
