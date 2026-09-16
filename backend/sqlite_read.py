
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any, Iterator

__all__ = ["SqliteError", "SqliteReader"]

_MAGIC = b"SQLite format 3\x00"
_MAX_BYTES = 128 * 1024 * 1024

_INT_SIZES = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}


class SqliteError(Exception):
    pass


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for index in range(8):
        byte = data[pos + index]
        result = (result << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return result, pos + index + 1
    byte = data[pos + 8]
    return (result << 8) | byte, pos + 9


def _split_top(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'`[":
            quote = "]" if char == "[" else char
            current.append(char)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def _parse_columns(sql: str) -> list[str]:
    start = sql.find("(")
    end = sql.rfind(")")
    if start < 0 or end <= start:
        return []
    columns: list[str] = []
    for item in _split_top(sql[start + 1 : end]):
        item = item.strip()
        if not item:
            continue
        upper = item.upper()
        if upper.startswith(
            ("PRIMARY ", "UNIQUE", "CHECK", "FOREIGN ", "CONSTRAINT ", "PRIMARY(", "UNIQUE(")
        ):
            continue
        match = re.match(r'\s*(?:"([^"]+)"|\[([^\]]+)\]|`([^`]+)`|([A-Za-z_][\w$]*))', item)
        if not match:
            continue
        name = next((g for g in match.groups() if g), None)
        if name:
            columns.append(name)
    return columns


class SqliteReader:

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            raise SqliteError(f"Could not read {self.path}: {exc}") from exc

        if len(data) > _MAX_BYTES:
            raise SqliteError(f"{self.path.name} is too large to read safely.")
        if data[:16] != _MAGIC:
            raise SqliteError(f"{self.path.name} is not a SQLite database.")

        self.data = data
        raw_page_size = int.from_bytes(data[16:18], "big")
        self.page_size = 65536 if raw_page_size == 1 else raw_page_size
        if self.page_size < 512 or self.page_size & (self.page_size - 1):
            raise SqliteError(f"Unsupported page size {self.page_size}.")
        self.reserved = data[20]
        encoding = int.from_bytes(data[56:60], "big") or 1
        self.encoding = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}.get(
            encoding, "utf-8"
        )
        self._schema = self._read_schema()

    @property
    def usable(self) -> int:
        return self.page_size - self.reserved

    def _page(self, number: int) -> bytes:
        start = (number - 1) * self.page_size
        page = self.data[start : start + self.page_size]
        if len(page) < self.page_size:
            raise SqliteError(f"Truncated page {number}.")
        return page

    def _payload(self, page: bytes, pos: int, size: int, max_local: int) -> bytes:
        usable = self.usable
        if size <= max_local:
            return page[pos : pos + size]

        local = max_local + (size - max_local) % (usable - 4)
        payload = bytearray(page[pos : pos + local])
        next_page = int.from_bytes(page[pos + local : pos + local + 4], "big")
        guard = 0
        while next_page and len(payload) < size and guard < 100000:
            guard += 1
            overflow = self._page(next_page)
            next_page = int.from_bytes(overflow[0:4], "big")
            remaining = size - len(payload)
            payload.extend(overflow[4 : 4 + min(usable - 4, remaining)])
        return bytes(payload[:size])

    def _walk(self, root: int) -> Iterator[tuple[int | None, bytes]]:
        stack = [root]
        seen: set[int] = set()
        while stack:
            number = stack.pop()
            if number in seen or number <= 0:
                continue
            seen.add(number)
            page = self._page(number)
            header = 100 if number == 1 else 0
            page_type = page[header]
            cells = int.from_bytes(page[header + 3 : header + 5], "big")

            if page_type == 5:
                pointer_start = header + 12
                for index in range(cells):
                    offset = int.from_bytes(
                        page[pointer_start + 2 * index : pointer_start + 2 * index + 2],
                        "big",
                    )
                    stack.append(int.from_bytes(page[offset : offset + 4], "big"))
                right = int.from_bytes(page[header + 8 : header + 12], "big")
                if right:
                    stack.append(right)
                continue

            if page_type == 2:
                pointer_start = header + 12
                for index in range(cells):
                    offset = int.from_bytes(
                        page[pointer_start + 2 * index : pointer_start + 2 * index + 2],
                        "big",
                    )
                    stack.append(int.from_bytes(page[offset : offset + 4], "big"))
                right = int.from_bytes(page[header + 8 : header + 12], "big")
                if right:
                    stack.append(right)
                continue

            if page_type == 13:
                pointer_start = header + 8
                for index in range(cells):
                    offset = int.from_bytes(
                        page[pointer_start + 2 * index : pointer_start + 2 * index + 2],
                        "big",
                    )
                    size, cursor = _varint(page, offset)
                    rowid, cursor = _varint(page, cursor)
                    yield rowid, self._payload(page, cursor, size, self.usable - 35)
                continue

            if page_type == 10:
                pointer_start = header + 8
                for index in range(cells):
                    offset = int.from_bytes(
                        page[pointer_start + 2 * index : pointer_start + 2 * index + 2],
                        "big",
                    )
                    size, cursor = _varint(page, offset)
                    yield None, self._payload(page, cursor, size, self.usable - 12)
                continue

            raise SqliteError(f"Unsupported b-tree page type {page_type}.")

    def _values(self, payload: bytes) -> list[Any]:
        if not payload:
            return []
        header_size, pos = _varint(payload, 0)
        if header_size <= 0 or header_size > len(payload):
            return []
        types: list[int] = []
        cursor = pos
        while cursor < header_size:
            serial, cursor = _varint(payload, cursor)
            types.append(serial)

        values: list[Any] = []
        offset = header_size
        for serial in types:
            if serial == 0:
                values.append(None)
            elif serial in _INT_SIZES:
                size = _INT_SIZES[serial]
                chunk = payload[offset : offset + size]
                values.append(int.from_bytes(chunk, "big", signed=True))
                offset += size
            elif serial == 7:
                values.append(struct.unpack(">d", payload[offset : offset + 8])[0])
                offset += 8
            elif serial == 8:
                values.append(0)
            elif serial == 9:
                values.append(1)
            elif serial >= 12 and serial % 2 == 0:
                size = (serial - 12) // 2
                values.append(payload[offset : offset + size])
                offset += size
            elif serial >= 13 and serial % 2 == 1:
                size = (serial - 13) // 2
                values.append(
                    payload[offset : offset + size].decode(self.encoding, "replace")
                )
                offset += size
            else:
                values.append(None)
        return values

    def _read_schema(self) -> dict[str, dict[str, Any]]:
        schema: dict[str, dict[str, Any]] = {}
        for _rowid, payload in self._walk(1):
            values = self._values(payload)
            if len(values) < 5:
                continue
            kind, name, _table, root, sql = values[:5]
            if kind not in ("table", "view") or not name:
                continue
            schema[str(name)] = {
                "type": str(kind),
                "rootpage": int(root or 0),
                "sql": str(sql or ""),
                "columns": _parse_columns(str(sql or "")),
            }
        return schema

    def tables(self) -> list[str]:
        return sorted(self._schema)

    def has_table(self, name: str) -> bool:
        entry = self._schema.get(name)
        return entry is not None and entry["type"] == "table"

    def columns(self, name: str) -> list[str]:
        entry = self._schema.get(name)
        return list(entry["columns"]) if entry else []

    def rows(self, name: str) -> list[dict[str, Any]]:
        entry = self._schema.get(name)
        if entry is None or entry["type"] != "table":
            return []
        columns = entry["columns"]
        root = entry["rootpage"]
        if root <= 0:
            return []

        page_type = self._page(root)[100 if root == 1 else 0]
        if page_type in (2, 10):
            raise SqliteError(f"{name} is a WITHOUT ROWID table, which is not supported.")

        results: list[dict[str, Any]] = []
        for _rowid, payload in self._walk(root):
            values = self._values(payload)
            if columns:
                if len(values) < len(columns):
                    values = values + [None] * (len(columns) - len(values))
                results.append(dict(zip(columns, values)))
            else:
                results.append({str(index): value for index, value in enumerate(values)})
        return results

    def close(self) -> None:
        self.data = b""
