"""在应用镜像内执行；输出只进入发布私有目录，不打印凭据。"""

import base64
import json
import sys
import time


def fingerprint():
    from app.core.config import settings
    from app.core.credentials import decrypt_credentials
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import NullPool

    engine = create_engine(str(settings.DATABASE_URL), poolclass=NullPool)
    with engine.connect() as connection:
        inspector = inspect(connection)
        names = inspector.get_table_names(schema="public")
        quote = engine.dialect.identifier_preparer.quote
        rows = {
            n: connection.execute(
                text("SELECT count(*) FROM public." + quote(n))
            ).scalar_one()
            for n in names
        }
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


def redis_export():
    import redis
    from app.core.config import settings

    client = redis.Redis.from_url(settings.REDIS_URL)
    if client.connection_pool.connection_kwargs.get("db") != 15:
        raise RuntimeError("Source Redis must be DB 15")
    records = []
    enc = lambda value: base64.b64encode(value).decode()
    # 应用已停写；只读取本逻辑库，不调用共享实例 SAVE/FLUSH。
    for key in sorted(set(client.scan_iter())):
        value, ttl = client.pipeline(transaction=True).dump(key).pttl(key).execute()
        if value is None or ttl == -2:
            continue
        records.append(
            {
                "key": enc(key),
                "dump": enc(value),
                "expires_at_ms": int(time.time() * 1000) + ttl if ttl >= 0 else None,
            }
        )
    return records


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
    elif action == "redis":
        result = redis_export()
    elif action == "broker":
        import redis
        from app.core.config import settings

        client = redis.Redis.from_url(settings.REDIS_URL)
        result = {"unacked": client.hlen("unacked")}
    else:
        result = worker_control(action, json.loads(sys.argv[2]))
    print(json.dumps(result))
