"""真实 Redis 队列迁移回归；仅回环独立测试库，不清库、不连接生产。"""

import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from probe import migrate_targeted_messages


class BrokerMigrationTests(unittest.TestCase):
    def setUp(self):
        url = os.environ["TEST_RELEASE_REDIS_URL"]
        parsed = urlsplit(url)
        if parsed.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ) or parsed.path not in ("/12", "/42"):
            raise RuntimeError("Use isolated loopback Redis DB12 (local) or DB42 (CI)")
        self.client = redis.Redis.from_url(url)
        self.client.ping()
        self.prefix = "release-test:" + uuid.uuid4().hex + ":"
        self.options = {
            "token": uuid.uuid4().hex,
            "manifest_sha256": "a" * 64,
            "key_prefix": self.prefix,
        }
        self.addCleanup(self.cleanup)

    def cleanup(self):
        keys = list(self.client.scan_iter(match=self.prefix + "*"))
        if keys:
            self.client.delete(*keys)
        self.client.close()

    def message(self, number, task="ads.targeted_step"):
        return json.dumps(
            {
                "body": "unchanged-base64-" + str(number),
                "headers": {
                    "id": str(number),
                    "task": task,
                    "routing_key": "ad-management",
                    "tenant": "fixture",
                },
                "properties": {
                    "delivery_info": {"exchange": "", "routing_key": "ad-management"},
                    "priority": 0,
                    "correlation_id": str(number),
                },
                "content-type": "application/json",
            }
        ).encode()

    def migrate(self):
        return migrate_targeted_messages(self.client, **self.options)

    def snapshot(self):
        return {
            k: self.client.dump(k)
            for k in self.client.scan_iter(match=self.prefix + "*")
        }

    def test_1750_messages_keep_ids_bodies_and_fifo_across_priority_keys(self):
        total = 0
        for priority in (0, 3, 6, 9):
            suffix = "\x06\x16" + str(priority) if priority else ""
            old, new = (
                self.prefix + "ad-management" + suffix,
                self.prefix + "ads-live-directory" + suffix,
            )
            source = [
                self.message(n, "ads.targeted_step" if n % 4 != 0 else "ads.execute")
                for n in range(584)
            ]
            # 438 targeted per priority, minus two gives the production-sized 1750 fixture.
            if priority == 0:
                source[1], source[2] = (
                    self.message(1, "ads.execute"),
                    self.message(2, "ads.execute"),
                )
            retained = [
                raw
                for raw in source
                if json.loads(raw)["headers"]["task"] != "ads.targeted_step"
            ]
            selected = [raw for raw in source if raw not in retained]
            existing = [self.message("already-queued")]
            self.client.rpush(old, *source)
            self.client.rpush(new, *existing)
            total += len(selected)
            setattr(
                self,
                "fixture" + str(priority),
                (old, new, retained, selected, existing),
            )
        receipt = self.migrate()
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["moved"], 1750)
        self.assertEqual(receipt["moved"], total)
        for priority in (0, 3, 6, 9):
            old, new, retained, selected, existing = getattr(
                self, "fixture" + str(priority)
            )
            self.assertEqual(self.client.lrange(old, 0, -1), retained)
            received = self.client.lrange(new, 0, -1)
            self.assertEqual(received[-len(existing) :], existing)
            for before, after in zip(selected, received, strict=False):
                expected = json.loads(before)
                expected["properties"]["delivery_info"]["routing_key"] = (
                    "ads-live-directory"
                )
                expected["headers"]["routing_key"] = "ads-live-directory"
                self.assertEqual(json.loads(after), expected)
            self.assertEqual(len(received), len(selected) + len(existing))
        snapshot = self.snapshot()
        self.assertEqual(
            self.migrate(), receipt
        )  # 模拟事务成功、exec 回包丢失后的重试。
        self.assertEqual(self.snapshot(), snapshot)

    def test_real_celery_envelope_reject_requeues_into_new_queue(self):
        from celery import Celery
        from kombu import Queue

        app = Celery("release-fixture", broker=os.environ["TEST_RELEASE_REDIS_URL"])
        app.conf.update(
            task_default_queue="control",
            task_create_missing_queues=False,
            task_queues=tuple(
                Queue(name)
                for name in ("control", "ad-management", "ads-live-directory")
            ),
            broker_transport_options={"global_keyprefix": self.prefix},
        )
        self.addCleanup(app.close)
        sent = app.send_task(
            "ads.targeted_step",
            args=["synthetic-tenant", "synthetic-run"],
            queue="ad-management",
        )
        before = json.loads(self.client.lindex(self.prefix + "ad-management", 0))
        self.assertEqual(self.migrate()["moved"], 1)
        after = json.loads(self.client.lindex(self.prefix + "ads-live-directory", 0))
        self.assertEqual(after["body"], before["body"])
        self.assertEqual(after["headers"]["id"], sent.id)
        with app.connection_for_read() as connection:
            channel = connection.channel()
            message = channel.basic_get("ads-live-directory", no_ack=False)
            self.assertIsNotNone(message)
            message.reject(requeue=True)
            self.assertEqual(self.client.llen(self.prefix + "ads-live-directory"), 1)
            self.assertEqual(self.client.llen(self.prefix + "ad-management"), 0)
            message = channel.basic_get("ads-live-directory", no_ack=False)
            self.assertEqual(message.headers["id"], sent.id)
            message.ack()
            channel.close()

    def test_unmatched_raw_bytes_and_empty_target_remain_untouched(self):
        raw = b'{ "headers": {"task": "ads.execute"}, "body": "opaque" }'
        self.client.rpush(self.prefix + "ad-management", raw)
        self.assertEqual(self.migrate()["moved"], 0)
        self.assertEqual(
            self.client.lrange(self.prefix + "ad-management", 0, -1), [raw]
        )
        self.assertFalse(self.client.exists(self.prefix + "ads-live-directory"))

    def test_wrong_route_malformed_message_expiry_and_unacked_abort_without_changes(
        self,
    ):
        for failure in ("route", "json", "ttl", "unacked", "priority", "type"):
            with self.subTest(failure=failure):
                old = self.prefix + "ad-management"
                self.client.rpush(old, self.message(1))
                if failure == "route":
                    message = json.loads(self.message(2))
                    message["properties"]["delivery_info"]["routing_key"] = "foreign"
                    self.client.rpush(old, json.dumps(message))
                elif failure == "json":
                    self.client.rpush(old, b"not-json")
                elif failure == "ttl":
                    self.client.expire(old, 60)
                elif failure == "unacked":
                    self.client.zadd(self.prefix + "unacked_index", {"delivery": 1})
                elif failure == "priority":
                    self.client.rpush(old + "\x06\x165", self.message(2))
                elif failure == "type":
                    self.client.set(self.prefix + "ads-live-directory", "bad")
                before = self.snapshot()
                with self.assertRaises((RuntimeError, json.JSONDecodeError)):
                    self.migrate()
                self.assertEqual(self.snapshot(), before)
                self.cleanup()

    def test_watch_conflict_preserves_all_messages(self):
        old = self.prefix + "ad-management"
        first, concurrent = self.message(1), self.message(2)
        self.client.rpush(old, first)
        original = redis.client.Pipeline.execute

        def changed(pipe, *args, **kwargs):
            self.client.lpush(old, concurrent)
            return original(pipe, *args, **kwargs)

        with (
            patch.object(redis.client.Pipeline, "execute", changed),
            self.assertRaises(redis.WatchError),
        ):
            self.migrate()
        self.assertEqual(self.client.lrange(old, 0, -1), [concurrent, first])
        self.assertFalse(self.client.exists(self.prefix + "ads-live-directory"))

    def test_readback_detects_changes_after_atomic_migration(self):
        self.client.rpush(self.prefix + "ad-management", self.message(1))
        self.migrate()
        self.client.lpop(self.prefix + "ads-live-directory")
        with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
            self.migrate()

    def test_receipt_cannot_be_reused_for_another_backup(self):
        self.migrate()
        self.options["manifest_sha256"] = "b" * 64
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.migrate()


if __name__ == "__main__":
    unittest.main()
