from __future__ import annotations

import re
from typing import Any, Iterator


_CREATE_TABLE_RE = re.compile(r"^CREATE TABLE `([^`]+)`")
_CREATE_COLUMN_RE = re.compile(r"^\s*`([^`]+)`\s+")
_INSERT_VALUES_RE = re.compile(r"^INSERT INTO `([^`]+)` VALUES\s*", re.DOTALL)


def _sql_statement_complete(sql: str) -> bool:
    in_quote = False
    escaped = False
    complete = False
    for ch in sql:
        if in_quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_quote = False
            continue
        if ch == "'":
            in_quote = True
            continue
        if ch == ";":
            complete = True
        elif not ch.isspace():
            complete = False
    return complete and not in_quote


def _split_sql_rows(values_sql: str) -> list[str]:
    rows: list[str] = []
    i = 0
    n = len(values_sql)
    while i < n:
        while i < n and (values_sql[i].isspace() or values_sql[i] in ",;"):
            i += 1
        if i >= n:
            break
        if values_sql[i] != "(":
            return []
        start = i + 1
        i += 1
        depth = 1
        in_quote = False
        escaped = False
        while i < n:
            ch = values_sql[i]
            if in_quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == "'":
                    in_quote = False
            else:
                if ch == "'":
                    in_quote = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        rows.append(values_sql[start:i])
                        i += 1
                        break
            i += 1
        else:
            return []
    return rows


def _split_sql_values(row_sql: str) -> list[str]:
    values: list[str] = []
    start = 0
    depth = 0
    in_quote = False
    escaped = False
    for i, ch in enumerate(row_sql):
        if in_quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_quote = False
            continue
        if ch == "'":
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            values.append(row_sql[start:i])
            start = i + 1
    values.append(row_sql[start:])
    return values


def _quote_ident(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def _rewrite_generated_insert(stmt: str, generated_columns: dict[str, tuple[list[str], set[int]]]) -> str:
    match = _INSERT_VALUES_RE.match(stmt)
    if not match:
        return stmt
    table = match.group(1)
    meta = generated_columns.get(table)
    if not meta:
        return stmt
    columns, generated_indexes = meta
    non_generated_columns = [name for idx, name in enumerate(columns) if idx not in generated_indexes]
    rows = _split_sql_rows(stmt[match.end() :])
    if not rows:
        return stmt
    rewritten_rows: list[str] = []
    for row in rows:
        values = _split_sql_values(row)
        if len(values) != len(columns):
            return stmt
        kept = [value for idx, value in enumerate(values) if idx not in generated_indexes]
        rewritten_rows.append("(" + ",".join(kept) + ")")
    column_sql = ",".join(_quote_ident(col) for col in non_generated_columns)
    return f"INSERT INTO {_quote_ident(table)} ({column_sql}) VALUES\n" + ",\n".join(rewritten_rows) + ";\n"


def iter_mysql_import_sql(lines: Any) -> Iterator[str]:
    """Rewrite dump INSERTs so MySQL computes generated columns on import.

    ``mysqldump`` emits positional INSERTs after each table definition.  A
    generated column is present in that positional row even though MySQL does
    not allow a value to be supplied for it.  We learn the generated indexes
    from each CREATE TABLE statement and emit an explicit-column INSERT that
    omits every generated column.  The rule is table/column agnostic and is
    shared by image installation and live instance migration imports.
    """
    generated_columns: dict[str, tuple[list[str], set[int]]] = {}
    create_table = ""
    create_columns: list[tuple[str, bool]] = []
    insert_buffer: list[str] = []

    for line in lines:
        if insert_buffer:
            insert_buffer.append(line)
            stmt = "".join(insert_buffer)
            if _sql_statement_complete(stmt):
                yield _rewrite_generated_insert(stmt, generated_columns)
                insert_buffer = []
            continue

        create_match = _CREATE_TABLE_RE.match(line)
        if create_match:
            create_table = create_match.group(1)
            create_columns = []
            yield line
            continue

        if create_table:
            column_match = _CREATE_COLUMN_RE.match(line)
            if column_match:
                create_columns.append((column_match.group(1), "GENERATED" in line.upper()))
            if line.startswith(")"):
                indexes = {idx for idx, (_, generated) in enumerate(create_columns) if generated}
                if indexes:
                    generated_columns[create_table] = ([name for name, _ in create_columns], indexes)
                create_table = ""
                create_columns = []
            yield line
            continue

        insert_match = _INSERT_VALUES_RE.match(line)
        if insert_match and insert_match.group(1) in generated_columns:
            insert_buffer = [line]
            stmt = "".join(insert_buffer)
            if _sql_statement_complete(stmt):
                yield _rewrite_generated_insert(stmt, generated_columns)
                insert_buffer = []
            continue

        yield line

    if insert_buffer:
        yield _rewrite_generated_insert("".join(insert_buffer), generated_columns)
