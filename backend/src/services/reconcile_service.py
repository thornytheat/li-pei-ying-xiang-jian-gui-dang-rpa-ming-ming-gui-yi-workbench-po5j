"""归档目录与批次台账逐件对账（只读，不改目录也不改台账）。

对账口径：
  - 台账侧：attachments.status='archived' 且有 stored_name 的每件都应在归档目录有同名文件，
    且文件内容 SHA-256 与台账哈希一致；
  - 目录侧：归档目录里每个普通文件都应能在台账中找到唯一归属；
  - 差异四类：ledger_only（台账有目录缺）/ content_mismatch（内容哈希不符）/
    name_mismatch（同名归属冲突）/ orphan_file（目录有台账缺）。
"""
from __future__ import annotations

import hashlib
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import MATERIAL_TYPE_LABELS, ReconcileStatus
from src.models.attachment import Attachment
from src.services import archive_service


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ledger_row(att: Attachment, status: str, detail: str, disk_sha256: str = "") -> dict:
    return {
        "kind": "ledger",
        "status": status,
        "attachment_id": str(att.id),
        "scan_seq": att.scan_seq,
        "original_name": att.original_name,
        "stored_name": att.stored_name or "",
        "material_type": att.material_type,
        "material_label": MATERIAL_TYPE_LABELS.get(att.material_type, att.material_type),
        "ledger_sha256": att.sha256,
        "disk_sha256": disk_sha256,
        "last_run_id": str(att.last_run_id) if att.last_run_id else None,
        "detail": detail,
    }


def reconcile(db: Session) -> dict:
    archived = (
        db.execute(
            select(Attachment)
            .where(Attachment.status == "archived")
            .where(Attachment.stored_name.is_not(None))
            .order_by(Attachment.scan_seq)
        )
        .scalars()
        .all()
    )
    owners: dict[str, Attachment] = {}
    rows: list[dict] = []
    summary = {
        ReconcileStatus.MATCHED.value: 0,
        ReconcileStatus.LEDGER_ONLY.value: 0,
        ReconcileStatus.CONTENT_MISMATCH.value: 0,
        ReconcileStatus.NAME_MISMATCH.value: 0,
        ReconcileStatus.ORPHAN_FILE.value: 0,
    }

    for att in archived:
        name = att.stored_name or ""
        # 同一档案名在台账里被两个附件占用：命名冲突，逐件标出
        if name in owners:
            summary[ReconcileStatus.NAME_MISMATCH.value] += 1
            rows.append(_ledger_row(att, ReconcileStatus.NAME_MISMATCH.value, "同一档案名被多个附件占用"))
            continue
        owners[name] = att
        path = archive_service.stored_path(name)
        if not os.path.exists(path):
            summary[ReconcileStatus.LEDGER_ONLY.value] += 1
            rows.append(_ledger_row(att, ReconcileStatus.LEDGER_ONLY.value, "台账已归档，但归档目录缺少该文件"))
            continue
        disk_sha = _file_sha256(path)
        if disk_sha != att.sha256:
            summary[ReconcileStatus.CONTENT_MISMATCH.value] += 1
            rows.append(
                _ledger_row(att, ReconcileStatus.CONTENT_MISMATCH.value, "文件存在但内容 SHA-256 与台账不符", disk_sha)
            )
            continue
        summary[ReconcileStatus.MATCHED.value] += 1
        rows.append(_ledger_row(att, ReconcileStatus.MATCHED.value, "台账与落盘文件、内容哈希一致", disk_sha))

    for name in archive_service.list_stored_files():
        if name in owners:
            continue
        # 重复件本来就不落盘、pending/unknown 也没有 stored_name，因此目录文件必须有归属
        summary[ReconcileStatus.ORPHAN_FILE.value] += 1
        path = archive_service.stored_path(name)
        rows.append(
            {
                "kind": "disk",
                "status": ReconcileStatus.ORPHAN_FILE.value,
                "attachment_id": None,
                "scan_seq": None,
                "original_name": "",
                "stored_name": name,
                "material_type": "",
                "material_label": "",
                "ledger_sha256": "",
                "disk_sha256": _file_sha256(path) if os.path.exists(path) else "",
                "last_run_id": None,
                "detail": "归档目录存在文件，但批次台账没有对应归档记录",
            }
        )

    rows.sort(key=lambda row: (row["status"] == ReconcileStatus.MATCHED.value, row["stored_name"]))
    issue_total = (
        summary[ReconcileStatus.LEDGER_ONLY.value]
        + summary[ReconcileStatus.CONTENT_MISMATCH.value]
        + summary[ReconcileStatus.NAME_MISMATCH.value]
        + summary[ReconcileStatus.ORPHAN_FILE.value]
    )
    return {
        "summary": {
            "matched": summary[ReconcileStatus.MATCHED.value],
            "ledger_only": summary[ReconcileStatus.LEDGER_ONLY.value],
            "content_mismatch": summary[ReconcileStatus.CONTENT_MISMATCH.value],
            "name_mismatch": summary[ReconcileStatus.NAME_MISMATCH.value],
            "orphan_file": summary[ReconcileStatus.ORPHAN_FILE.value],
            "ledger_archived": len(archived),
            "disk_files": archive_service.stored_file_count(),
            "issue_total": issue_total,
            "clean": issue_total == 0,
        },
        "rows": rows,
    }
