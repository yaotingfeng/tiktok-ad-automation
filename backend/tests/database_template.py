"""每个测试进程只迁移一次模板库，独立测试库用 PostgreSQL TEMPLATE 复制。

从空库执行全部迁移约需数秒，大量用例各自新建库时累计可达二十分钟；复制模板
只需毫秒级，内容与“新库执行到 head”完全一致（含迁移种入的不可变数据）。
"""

import atexit
import os
import time
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from app.core.config import settings
from tests.database import require_test_database

_templates: dict[str, str] = {}


def _admin(url: URL) -> str:
    return url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )


def _head_template(source: URL) -> str:
    """按当前进程的主测试库懒创建模板；进程退出时删除，避免残留旧 schema。"""
    key = source.render_as_string(hide_password=False)
    if key in _templates:
        return _templates[key]
    # 名称含进程号，并行分组共用一个 PostgreSQL 实例时互不覆盖。
    name = f"tpl_{os.getpid()}_{source.database}"[:63]
    require_test_database(source.set(database=name).render_as_string())
    with psycopg.connect(_admin(source), autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    previous = settings.DATABASE_URL
    try:
        # alembic env.py 读取 settings，且使用 NullPool，迁移结束后不留连接。
        settings.DATABASE_URL = source.set(database=name).render_as_string(
            hide_password=False
        )
        backend = Path(__file__).resolve().parents[1]
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "app/alembic"))
        command.upgrade(config, "head")
    finally:
        settings.DATABASE_URL = previous
    _templates[key] = name

    def drop() -> None:
        try:
            with psycopg.connect(_admin(source), autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
        except psycopg.Error:
            pass

    atexit.register(drop)
    return name


def create_head_database(source_url: str, name: str) -> None:
    """创建一个已迁移到 head 的独立测试库。"""
    source = make_url(source_url)
    require_test_database(source.set(database=name).render_as_string())
    template = _head_template(source)
    with psycopg.connect(_admin(source), autocommit=True) as admin:
        for attempt in range(50):
            try:
                admin.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                        sql.Identifier(name), sql.Identifier(template)
                    )
                )
                return
            except psycopg.errors.ObjectInUse:
                # 同进程内复制时模板短暂被占用，稍后重试。
                if attempt == 49:
                    raise
                time.sleep(0.1)
