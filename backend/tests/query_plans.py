"""用真实服务器预备语句验证长连接执行计划，避免只测新连接的定制计划。"""

import re
from datetime import date, datetime
from uuid import UUID, uuid4

from psycopg import sql


def explain_prepared(connection, statement, parameters):
    names = list(dict.fromkeys(re.findall(r"%\((\w+)\)s", statement)))
    prepared = re.sub(r"%\((\w+)\)s", lambda m: f"${names.index(m[1]) + 1}", statement)

    def parameter_type(value):
        for kind, name in (
            (UUID, "uuid"),
            (datetime, "timestamptz"),
            (date, "date"),
            (bool, "boolean"),
            (int, "integer"),
            ((list, tuple), "text[]"),
        ):
            if isinstance(value, kind):
                return sql.SQL(name)
        return sql.SQL("text")

    name = sql.Identifier("plan_probe_" + uuid4().hex)
    with connection.connection.driver_connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("PREPARE {} ({}) AS ").format(
                name,
                sql.SQL(", ").join(parameter_type(parameters[n]) for n in names),
            )
            + sql.SQL(prepared)
        )
        try:
            cursor.execute(
                sql.SQL("EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE {} ({})").format(
                    name, sql.SQL(", ").join(sql.Literal(parameters[n]) for n in names)
                )
            )
            return cursor.fetchone()[0]
        finally:
            cursor.execute(sql.SQL("DEALLOCATE {}").format(name))
