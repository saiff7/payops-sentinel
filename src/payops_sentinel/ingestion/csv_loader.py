"""Parse untrusted CSV bytes into header + raw string rows. No business interpretation."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from payops_sentinel.domain.enums import FileRejectionCode
from payops_sentinel.domain.models import FileRejectedError

FIRST_DATA_ROW = 2  # header is spreadsheet row 1


@dataclass(frozen=True, slots=True)
class RawRow:
    source_row_number: int
    values: dict[str, str] | None  # None when the field count does not match the header


def load_csv(content: bytes, *, max_bytes: int, max_rows: int) -> tuple[list[str], list[RawRow]]:
    if len(content) > max_bytes:
        raise FileRejectedError(FileRejectionCode.FILE_TOO_LARGE, f"file exceeds {max_bytes} bytes")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileRejectedError(FileRejectionCode.NOT_UTF8, "file is not valid UTF-8") from exc
    if not text.strip():
        raise FileRejectedError(FileRejectionCode.EMPTY_FILE, "file is empty")

    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise FileRejectedError(FileRejectionCode.MALFORMED_CSV, "file is not valid CSV") from exc

    headers = [h.strip().lower() for h in records[0]]
    rows: list[RawRow] = []
    for offset, record in enumerate(records[1:]):
        if not any(cell.strip() for cell in record):
            continue  # blank line
        number = FIRST_DATA_ROW + offset
        values = (
            {h: cell.strip() for h, cell in zip(headers, record, strict=True)}
            if len(record) == len(headers)
            else None
        )
        rows.append(RawRow(source_row_number=number, values=values))
        if len(rows) > max_rows:
            raise FileRejectedError(FileRejectionCode.TOO_MANY_ROWS, f"more than {max_rows} rows")
    if not rows:
        raise FileRejectedError(FileRejectionCode.EMPTY_FILE, "file has no data rows")
    return headers, rows
