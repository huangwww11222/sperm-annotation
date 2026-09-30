"""Read-only local operations CLI. Never starts FastAPI or initializes a database."""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from . import quality_audit as audit


def connect_readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("BEGIN")
    return c


def utc_bound(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "时间必须带时区，例如 2026-09-30T00:00:00+08:00"
        )
    from datetime import timezone

    return result.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def jobs(c, ids=(), media_id=None, since=None, until=None):
    exists = c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='training_export_audits'"
    ).fetchone()
    condition, args = [], []
    if ids:
        condition.append("t.id IN (" + ",".join("?" for _ in ids) + ")")
        args.extend(ids)
    if since:
        condition.append("julianday(t.created_at)>=julianday(?)")
        args.append(since)
    if until:
        condition.append("julianday(t.created_at)<julianday(?)")
        args.append(until)
    sql = "SELECT t.*," + (
        "a.audit_id FROM training_exports t LEFT JOIN training_export_audits a ON a.export_id=t.id"
        if exists
        else "NULL AS audit_id FROM training_exports t"
    )
    if condition:
        sql += " WHERE " + " AND ".join(condition)
    result = []
    for r in c.execute(sql + " ORDER BY t.created_at,t.id", args):
        r = dict(r)
        final_ids = json.loads(r["request_json"])["finalVersionIds"]
        refs = [
            dict(x)
            for x in c.execute(
                """SELECT f.id,m.media_id,m.id AS media_revision_id FROM final_versions f
           JOIN annotation_baselines a ON a.id=f.baseline_id JOIN media_revisions m ON m.id=a.media_revision_id
           WHERE f.id IN ("""
                + ",".join("?" for _ in final_ids)
                + ")",
                final_ids,
            )
        ]
        if media_id and not any(
            media_id in (s["media_id"], s["media_revision_id"]) for s in refs
        ):
            continue
        r["sources"] = refs
        current = c.execute(
            """SELECT COUNT(*) FROM final_versions f JOIN confirmation_sessions cs ON cs.id=f.confirmation_id
          JOIN review_versions rv ON rv.id=f.review_version_id JOIN review_sessions rs ON rs.id=rv.session_id
          WHERE f.id IN ("""
            + ",".join("?" for _ in final_ids)
            + """) AND cs.state='confirmed' AND rs.state='reviewed'
          AND f.id=(SELECT id FROM final_versions WHERE confirmation_id=cs.id ORDER BY rowid DESC LIMIT 1)""",
            final_ids,
        ).fetchone()[0]
        r["currentFinalAtRead"] = current == len(final_ids)
        result.append(r)
    if ids and set(ids) != {r["id"] for r in result}:
        raise ValueError("部分数据集不存在或不符合筛选条件")
    return result


def summary(r):
    return dict(
        exportId=r["id"],
        state=r["state"],
        actorId=r["actor_id"],
        createdAt=r["created_at"],
        audited=bool(r["audit_id"]),
        sources=r["sources"],
        currentFinalAtRead=r["currentFinalAtRead"],
        errorCode=r["error_code"],
    )


def bundle(c, selected, destination):
    files, records = {}, []
    for r in selected:
        snapshot, checksum = audit.load(c, r["id"])
        sp = f"audits/{snapshot['auditId']}.json"
        files[sp] = audit.packed(snapshot).encode()
        manifest = json.loads(r["manifest_json"]) if r["manifest_json"] else None
        mp = f"datasets/{r['id']}/manifest.json" if manifest else None
        if manifest:
            if (
                manifest.get("auditReference") != audit.reference(snapshot, checksum)
                or manifest.get("sourceSystemId") != snapshot["sourceSystemId"]
            ):
                raise ValueError("训练清单和审计身份不匹配")
            files[mp] = audit.packed(manifest).encode()
        records.append(
            {
                **summary(r),
                "sourceSystemId": snapshot["sourceSystemId"],
                "auditReference": audit.reference(snapshot, checksum),
                "snapshotFile": sp,
                "manifestFile": mp,
            }
        )
    files["datasets.jsonl"] = "".join(audit.packed(r) + "\n" for r in records).encode()
    manifest = dict(
        format=audit.BUNDLE_FORMAT,
        schemaVersion=audit.BUNDLE_SCHEMA,
        extractedAtUtc=audit.now(),
        datasetCount=len(records),
        files=[
            dict(name=k, sha256=audit.sha(v), size=len(v))
            for k, v in sorted(files.items())
        ],
    )
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files.items()):
            z.writestr(name, data)
        z.writestr("manifest.json", audit.packed(manifest))


def lookup(c, selected, query):
    found = []
    for r in selected:
        if not r["manifest_json"]:
            continue
        manifest = json.loads(r["manifest_json"])
        for s in manifest["samples"]:
            if query not in (s.get("sampleId"), s["image"], Path(s["image"]).name):
                continue
            source = next(
                x
                for x in manifest["sources"]
                if x["finalVersionId"] == s["finalVersionId"]
            )
            found.append(
                dict(
                    exportId=r["id"],
                    sourceSystemId=manifest.get("sourceSystemId"),
                    sample=s,
                    source=source,
                    auditReference=manifest.get("auditReference"),
                    audited=bool(r["audit_id"]),
                )
            )
    if not found:
        raise ValueError("未找到图片；请使用 sampleId、原文件名或包内路径")
    if len(found) > 1:
        raise ValueError("图片路径在多个包中重复，请同时指定 --dataset-id")
    return found[0]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="只读提取固定质量审计。时间范围按数据集创建时间，until 不含上界。"
    )
    parser.add_argument(
        "--db",
        default=os.getenv("APP_DB_FILE")
        or str(
            Path(
                os.getenv("APP_DATA_DIR")
                or Path(__file__).resolve().parents[1] / "data"
            )
            / "app.db"
        ),
    )
    parser.add_argument("--dataset-id", action="append", default=[])
    parser.add_argument("--media-id")
    parser.add_argument("--since", type=utc_bound)
    parser.add_argument("--until", type=utc_bound)
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="同时导出失败/未完成尝试的证据，不计成功数据集",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true")
    action.add_argument("--lookup", metavar="SAMPLE_OR_FILENAME")
    parser.add_argument(
        "--output", default="-", help="ZIP 输出；- 表示标准输出。拒绝覆盖已有文件。"
    )
    opts = parser.parse_args(argv)
    try:
        with closing(connect_readonly(opts.db)) as c:
            selected = jobs(c, opts.dataset_id, opts.media_id, opts.since, opts.until)
            if opts.list or opts.lookup:
                result = (
                    lookup(c, selected, opts.lookup)
                    if opts.lookup
                    else [summary(r) for r in selected]
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if opts.dataset_id and any(not r["audit_id"] for r in selected):
                raise ValueError(
                    "所选旧数据集缺少导出时快照；--list 可查看覆盖范围，旧图片可用 --lookup 追溯"
                )
            uncovered = sum(not r["audit_id"] for r in selected)
            if uncovered:
                print(
                    f"audit.uncovered legacyDatasets={uncovered}（未伪造旧快照）",
                    file=sys.stderr,
                )
            selected = [
                r
                for r in selected
                if r["audit_id"] and (opts.include_failed or r["state"] == "ready")
            ]
            if not selected:
                raise ValueError(
                    "没有符合条件的固定审计；失败尝试需显式 --include-failed"
                )
            output = Path(opts.output) if opts.output != "-" else None
            if output and output.exists():
                raise ValueError("输出文件已存在，请指定新文件名")
            with tempfile.TemporaryFile() as temp:
                bundle(c, selected, temp)
                temp.seek(0)
                if output:
                    # Exclusive creation protects existing operator output.
                    with output.open("xb") as dest:
                        try:
                            shutil.copyfileobj(temp, dest)
                        except BaseException:
                            output.unlink(missing_ok=True)
                            raise
                else:
                    shutil.copyfileobj(temp, sys.stdout.buffer)
            print(f"audit.extracted datasets={len(selected)}", file=sys.stderr)
            return 0
    except (ValueError, OSError, sqlite3.Error, KeyError) as e:
        print(f"audit.export_failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
