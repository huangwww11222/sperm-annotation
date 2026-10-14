"""Export A/B/C/F database facts for the standalone statistics system.

This is an operator utility rather than a pytest test.  Run it from the
repository root with::

    python backend/tests/export_statistics_data.py

By default it reads ``APP_DB_FILE`` (or the project's normal ``app.db``) and
writes a versioned ZIP to ``Desktop/标注确认数据``.  The source database is
opened read-only and is never modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import DB_FILE  # noqa: E402  (path bootstrap must run first)


EXPORT_SCHEMA_VERSION = "annotation-confirmation-statistics-v1"
DEFAULT_SOURCE_SYSTEM_ID = os.getenv(
    "STATISTICS_SOURCE_SYSTEM_ID", "annotation-confirmation-system"
)


class ExportError(RuntimeError):
    """Raised when a safe, complete statistics export cannot be produced."""


@dataclass(frozen=True)
class ExportSpec:
    table: str
    filename: str
    file_format: str = "csv"
    required: bool = True
    columns: tuple[str, ...] | None = None
    json_columns: tuple[str, ...] = ()
    order_by: str | None = None
    query: str | None = None


EXPORT_SPECS: tuple[ExportSpec, ...] = (
    # Password hashes are deliberately excluded.
    ExportSpec(
        "users",
        "users.csv",
        columns=("id", "username", "created_at"),
        order_by="id",
    ),
    ExportSpec("media_revisions", "media_revisions.csv", order_by="rowid"),
    ExportSpec(
        "annotation_baselines", "annotation_baselines.csv", order_by="rowid"
    ),
    ExportSpec(
        "baseline_frames",
        "baseline_frames.jsonl",
        file_format="jsonl",
        json_columns=("objects_json",),
        order_by="baseline_id, frame_index",
    ),
    ExportSpec("review_sessions", "review_sessions.csv", order_by="rowid"),
    ExportSpec("review_versions", "review_versions.csv", order_by="rowid"),
    ExportSpec(
        "review_version_frames",
        "review_version_frames.jsonl",
        file_format="jsonl",
        json_columns=("objects_json",),
        order_by="version_id, frame_index",
    ),
    ExportSpec(
        "review_changes",
        "review_changes.csv",
        order_by="review_version_id, frame_index, object_id",
    ),
    ExportSpec(
        "confirmation_sessions", "confirmation_sessions.csv", order_by="rowid"
    ),
    ExportSpec(
        "decision_heads",
        "decision_heads.csv",
        order_by="confirmation_id, change_id",
    ),
    ExportSpec(
        "decision_events",
        "decision_events.csv",
        order_by="confirmation_id, change_id, revision, rowid",
    ),
    ExportSpec(
        "final_versions",
        "final_versions.csv",
        query="""
            SELECT fv.*,
                   CASE
                     WHEN cs.state = 'confirmed'
                      AND fv.rowid = (
                        SELECT newest.rowid
                        FROM final_versions AS newest
                        WHERE newest.confirmation_id = fv.confirmation_id
                        ORDER BY newest.rowid DESC
                        LIMIT 1
                      )
                     THEN 1 ELSE 0
                   END AS is_current
            FROM final_versions AS fv
            JOIN confirmation_sessions AS cs ON cs.id = fv.confirmation_id
            ORDER BY fv.rowid
        """,
    ),
    ExportSpec(
        "final_version_frames",
        "final_version_frames.jsonl",
        file_format="jsonl",
        json_columns=("objects_json",),
        order_by="final_version_id, frame_index",
    ),
    ExportSpec(
        "final_frame_objects",
        "final_frame_objects.csv",
        order_by="final_version_id, frame_index, object_id",
    ),
    ExportSpec(
        "confirmation_final_records",
        "confirmation_final_records.jsonl",
        file_format="jsonl",
        json_columns=("decision_snapshot_json",),
        order_by="rowid",
    ),
    # These tables are useful process history but are not required to compute
    # the core A/B/C/F statistics.
    ExportSpec(
        "confirmation_actions",
        "confirmation_actions.csv",
        required=False,
        order_by="confirmation_id, created_at, rowid",
    ),
    ExportSpec(
        "confirmation_reopen_events",
        "confirmation_reopen_events.csv",
        required=False,
        order_by="confirmation_id, created_at, rowid",
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def desktop_path() -> Path:
    """Return the current user's Desktop, including redirected Windows paths."""

    if os.name == "nt":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                raw, _ = winreg.QueryValueEx(key, "Desktop")
            expanded = os.path.expandvars(str(raw))
            if expanded:
                return Path(expanded)
        except (OSError, ImportError):
            pass

    candidates = [
        Path.home() / "Desktop",
        Path.home() / "桌面",
        Path(os.getenv("OneDrive", "")) / "Desktop" if os.getenv("OneDrive") else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    return Path.home() / "Desktop"


def default_output_directory() -> Path:
    return desktop_path() / "标注确认数据"


def open_read_only(database: Path) -> sqlite3.Connection:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise ExportError(f"数据库文件不存在：{database}")
    try:
        conn = sqlite3.connect(
            database.as_uri() + "?mode=ro",
            uri=True,
            timeout=30,
        )
    except sqlite3.Error as exc:
        raise ExportError(f"无法只读打开数据库：{exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def select_rows(
    conn: sqlite3.Connection, spec: ExportSpec
) -> tuple[list[str], list[sqlite3.Row]]:
    if spec.query:
        cursor = conn.execute(spec.query)
    else:
        selected = (
            ", ".join(f'"{column}"' for column in spec.columns)
            if spec.columns
            else "*"
        )
        sql = f'SELECT {selected} FROM "{spec.table}"'
        if spec.order_by:
            # order_by values are constants declared above, never user input.
            sql += f" ORDER BY {spec.order_by}"
        cursor = conn.execute(sql)
    columns = [item[0] for item in cursor.description or ()]
    return columns, cursor.fetchall()


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[sqlite3.Row]) -> int:
    count = 0
    # utf-8-sig makes Chinese text readable when a user opens the CSV in Excel.
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[column] for column in columns])
            count += 1
    return count


def json_value(value: Any, table: str, column: str, row_number: int) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExportError(
            f"{table}.{column} 第 {row_number} 行不是合法 JSON"
        ) from exc


def write_jsonl(
    path: Path,
    columns: Sequence[str],
    rows: Iterable[sqlite3.Row],
    spec: ExportSpec,
) -> int:
    count = 0
    json_columns = set(spec.json_columns)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row_number, row in enumerate(rows, start=1):
            item = {
                column: json_value(row[column], spec.table, column, row_number)
                if column in json_columns
                else row[column]
                for column in columns
            }
            stream.write(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            count += 1
    return count


def safe_source_system_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ExportError("source-system-id 不能为空")
    if len(value) > 128:
        raise ExportError("source-system-id 不能超过 128 个字符")
    return value


def export_statistics_zip(
    database: Path,
    output_directory: Path | None = None,
    source_system_id: str = DEFAULT_SOURCE_SYSTEM_ID,
    output_name: str | None = None,
) -> Path:
    """Create and return a statistics ZIP without modifying ``database``."""

    database = database.expanduser().resolve()
    output_directory = (output_directory or default_output_directory()).expanduser()
    source_system_id = safe_source_system_id(source_system_id)
    export_id = "stats_" + uuid.uuid4().hex
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = output_name or f"标注确认数据_{timestamp}_{export_id[-8:]}.zip"
    if Path(filename).name != filename or not filename.lower().endswith(".zip"):
        raise ExportError("输出文件名必须是不含目录的 .zip 文件名")

    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / filename
    partial = target.with_name(target.name + ".partial")
    if target.exists() or partial.exists():
        raise ExportError(f"输出文件已经存在：{target}")

    conn = open_read_only(database)
    try:
        conn.execute("BEGIN")
        tables = existing_tables(conn)
        missing = [
            spec.table
            for spec in EXPORT_SPECS
            if spec.required and spec.table not in tables
        ]
        if missing:
            raise ExportError(
                "数据库缺少统计导出所需表：" + "、".join(sorted(missing))
            )

        omitted_optional = [
            spec.table
            for spec in EXPORT_SPECS
            if not spec.required and spec.table not in tables
        ]
        file_entries: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory(prefix="annotation-statistics-export-") as temp:
            staging = Path(temp)
            for spec in EXPORT_SPECS:
                if spec.table not in tables:
                    continue
                columns, rows = select_rows(conn, spec)
                destination = staging / spec.filename
                if spec.file_format == "jsonl":
                    count = write_jsonl(destination, columns, rows, spec)
                    encoding = "utf-8"
                else:
                    count = write_csv(destination, columns, rows)
                    encoding = "utf-8-sig"
                file_entries.append(
                    {
                        "name": spec.filename,
                        "sourceTable": spec.table,
                        "format": spec.file_format,
                        "encoding": encoding,
                        "columns": columns,
                        "rowCount": count,
                        "size": destination.stat().st_size,
                        "sha256": sha256_file(destination),
                        "required": spec.required,
                    }
                )

            manifest = {
                "exportId": export_id,
                "sourceSystemId": source_system_id,
                "schemaVersion": EXPORT_SCHEMA_VERSION,
                "exportMode": "full",
                "generatedAt": utc_now(),
                "timezone": "UTC",
                "sourceDatabaseName": database.name,
                "files": file_entries,
                "omittedOptionalTables": omitted_optional,
                "currentVersionRule": (
                    "final_versions.is_current=1 only for the newest final version "
                    "of a confirmation session whose state is confirmed"
                ),
                "semantics": {
                    "annotation_baselines.object_count": "所有帧中的框实例总数，不是去重 objectId 数",
                    "review_sessions.changed_count": "最终存在净修改的帧数",
                    "review_changes": "固定 B 相对 A 的最终净几何变化",
                    "decision_heads": "当前选择；decision_events 是历史事件",
                    "final_version_frames": "完整 F，包含空帧",
                    "frameIndex": "数据库从 0 开始",
                    "bbox": "原图像素连续坐标 [x1,y1,x2,y2]",
                },
                "excludedSensitiveFields": [
                    "users.password_hash",
                    "JWT",
                    "密钥",
                    "原始视频",
                ],
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            try:
                with zipfile.ZipFile(
                    partial, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.write(manifest_path, "manifest.json")
                    for entry in sorted(file_entries, key=lambda item: item["name"]):
                        archive.write(staging / entry["name"], entry["name"])
                os.replace(partial, target)
            except Exception:
                partial.unlink(missing_ok=True)
                raise
        return target
    except sqlite3.Error as exc:
        partial.unlink(missing_ok=True)
        raise ExportError(f"读取 SQLite 数据失败：{exc}") from exc
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="导出标注、审查、确认和最终版本数据，供独立统计系统导入。"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DB_FILE,
        help=f"SQLite 数据库路径，默认读取当前项目配置：{DB_FILE}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录；默认是桌面上的“标注确认数据”文件夹",
    )
    parser.add_argument(
        "--source-system-id",
        default=DEFAULT_SOURCE_SYSTEM_ID,
        help="此标注系统的稳定来源 ID",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help="可选 ZIP 文件名，必须以 .zip 结尾且不能包含目录",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = export_statistics_zip(
            database=args.database,
            output_directory=args.output_dir,
            source_system_id=args.source_system_id,
            output_name=args.output_name,
        )
    except ExportError as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1
    print("统计数据导出完成：")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
