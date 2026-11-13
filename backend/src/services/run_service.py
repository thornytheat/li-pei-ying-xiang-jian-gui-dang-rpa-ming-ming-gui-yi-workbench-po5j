"""归档批次：创建、投递 Celery、序列化、列表、断点续跑、逐件对账。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.constants.enums import (
    RUN_ITEM_OUTCOME_LABELS,
    RUN_MODE_LABELS,
    RUN_STATUS_LABELS,
    RunItemOutcome,
    RunMode,
    RunStatus,
)
from src.constants.log_templates import ACTION_RUN_CREATE, ACTION_RUN_RECONCILE, ACTION_RUN_RESUME
from src.models.archive_run import ArchiveRun
from src.models.attachment import Attachment
from src.models.user import User
from src.services import archive_service, audit_service, reconcile_service, rule_service
from src.utils.errors import AppError
from src.utils.ids import as_uuid


def next_run_no(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"RUN-{today}-"
    count = (
        db.execute(select(ArchiveRun.run_no).where(ArchiveRun.run_no.like(prefix + "%"))).scalars().all()
    )
    return f"{prefix}{len(count) + 1:02d}"


def _checkpoint_stats(run: ArchiveRun) -> dict[str, int]:
    """按 checkpoint 统计各单件结果数量（旧批次/试运行没有 checkpoint 时全为 0）。"""
    stats = {
        RunItemOutcome.ARCHIVED.value: 0,
        RunItemOutcome.DUPLICATE.value: 0,
        RunItemOutcome.PENDING.value: 0,
        RunItemOutcome.UNKNOWN.value: 0,
        RunItemOutcome.UNTOUCHED.value: 0,
        RunItemOutcome.REUSED.value: 0,
        RunItemOutcome.REPAIRED.value: 0,
        RunItemOutcome.UNCHANGED.value: 0,
    }
    for value in (run.checkpoint or {}).values():
        if value in stats:
            stats[value] += 1
    return stats


def serialize(run: ArchiveRun, with_files: bool = False) -> dict:
    stats = _checkpoint_stats(run)
    processed = sum(stats.values())
    payload = {
        "id": str(run.id),
        "run_no": run.run_no,
        "mode": run.mode,
        "mode_label": RUN_MODE_LABELS.get(run.mode, run.mode),
        "status": run.status,
        "status_label": RUN_STATUS_LABELS.get(run.status, run.status),
        "resumable": run.status == RunStatus.INTERRUPTED.value,
        "resumed_from_id": str(run.resumed_from_id) if run.resumed_from_id else None,
        "rule_version": run.rule_version,
        "template_snapshot": run.template_snapshot,
        "triggered_by_email": run.triggered_by_email,
        "note": run.note,
        "scanned": run.scanned,
        "new_files": run.new_files,
        "archived": run.archived,
        "duplicate": run.duplicate,
        "pending": run.pending,
        "unknown": run.unknown,
        "unchanged": run.unchanged,
        "failed": run.failed,
        "manual": run.manual,
        "auto_named": run.archived + run.duplicate,
        "checkpoint_seq": int(run.checkpoint_seq or 0),
        "checkpoint_processed": processed,
        "checkpoint_untouched": max(int(run.scanned or 0) - processed, 0),
        "reused_count": stats[RunItemOutcome.REUSED.value],
        "repaired_count": stats[RunItemOutcome.REPAIRED.value],
        "log": list(run.log or []),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": run.duration_ms,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
    if with_files:
        payload["stored_files"] = archive_service.stored_file_count()
    return payload


def list_runs(db: Session, limit: int = 20) -> dict:
    runs = (
        db.execute(select(ArchiveRun).order_by(ArchiveRun.created_at.desc()).limit(limit)).scalars().all()
    )
    return {
        "items": [serialize(run) for run in runs],
        "stored_files": archive_service.stored_file_count(),
        "active": next(
            (serialize(run) for run in runs if run.status in (RunStatus.QUEUED.value, RunStatus.RUNNING.value)),
            None,
        ),
    }


def get_run(db: Session, run_id: str) -> dict:
    uid = as_uuid(run_id)
    run = db.get(ArchiveRun, uid) if uid else None
    if run is None:
        raise AppError(codes.NOT_FOUND, "批次不存在", f"run_id={run_id}")
    return serialize(run, with_files=True)


def latest_run(db: Session) -> ArchiveRun | None:
    return db.execute(select(ArchiveRun).order_by(ArchiveRun.created_at.desc()).limit(1)).scalars().first()


def _assert_no_busy_run(db: Session) -> None:
    busy = (
        db.execute(
            select(ArchiveRun).where(ArchiveRun.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]))
        )
        .scalars()
        .first()
    )
    if busy is not None:
        raise AppError(codes.RUN_BUSY, "已有批次正在执行，请稍后再发起", f"run_no={busy.run_no}")


def create_run(db: Session, user: User, mode: str, note: str) -> dict:
    if mode not in (RunMode.DRY_RUN.value, RunMode.ARCHIVE.value):
        raise AppError(codes.VALIDATION_ERROR, "批次模式不合法", f"mode={mode}")
    _assert_no_busy_run(db)

    rule = rule_service.get_active(db)
    run = ArchiveRun(
        run_no=next_run_no(db),
        mode=mode,
        status=RunStatus.QUEUED.value,
        rule_version=rule.version,
        template_snapshot=rule.template,
        triggered_by_email=user.email,
        note=(note or "").strip()[:240],
    )
    db.add(run)
    db.flush()
    audit_service.record(
        db,
        action=ACTION_RUN_CREATE,
        actor=user,
        entity_type="archive_run",
        entity_id=run.id,
        detail={"run_no": run.run_no, "mode": mode, "rule_version": rule.version},
    )
    db.commit()
    _dispatch(db, run, mode)
    return get_run(db, str(run.id))


def resume_run(db: Session, user: User, run_id: str, note: str = "") -> dict:
    """对中断批次发起断点续跑：新批次回指原批次，复用其模板快照与已落盘结果。"""
    uid = as_uuid(run_id)
    source = db.get(ArchiveRun, uid) if uid else None
    if source is None:
        raise AppError(codes.NOT_FOUND, "批次不存在", f"run_id={run_id}")
    if source.status != RunStatus.INTERRUPTED.value:
        raise AppError(
            codes.RUN_NOT_RESUMABLE,
            "只有已中断的正式归档批次可以续跑",
            f"run_no={source.run_no} status={source.status}",
        )
    _assert_no_busy_run(db)

    run = ArchiveRun(
        run_no=next_run_no(db),
        mode=RunMode.ARCHIVE.value,
        status=RunStatus.QUEUED.value,
        rule_version=source.rule_version,
        template_snapshot=source.template_snapshot,
        triggered_by_email=user.email,
        note=(note or f"断点续跑：{source.run_no}").strip()[:240],
        resumed_from_id=source.id,
    )
    db.add(run)
    db.flush()
    audit_service.record(
        db,
        action=ACTION_RUN_RESUME,
        actor=user,
        entity_type="archive_run",
        entity_id=run.id,
        detail={
            "run_no": run.run_no,
            "resumed_from": source.run_no,
            "checkpoint_seq": int(source.checkpoint_seq or 0),
        },
    )
    db.commit()
    _dispatch(db, run, RunMode.ARCHIVE.value)
    return get_run(db, str(run.id))


def _dispatch(db: Session, run: ArchiveRun, mode: str) -> None:
    dispatched = False
    try:
        from src.config.celery_app import TASK_ARCHIVE_RUN, celery_app

        celery_app.send_task(TASK_ARCHIVE_RUN, args=[str(run.id)], kwargs={"mode": mode})
        dispatched = True
    except Exception as exc:  # noqa: BLE001 - broker 不可用时退化为进程内执行
        archive_service.append_log(run, f"Celery 投递失败（{exc}），改在 API 进程内同步执行")
        db.commit()

    if not dispatched:
        # 同会话同步执行：execute_run 会把终态直接写回 run，避免请求会话拿到过期的 queued 快照
        archive_service.execute_run(db, run, mode=mode)


def list_run_items(db: Session, run_id: str) -> dict:
    """批次逐件视图：批次停在哪（checkpoint_seq）、每件的处理结果与当前台账状态。"""
    uid = as_uuid(run_id)
    run = db.get(ArchiveRun, uid) if uid else None
    if run is None:
        raise AppError(codes.NOT_FOUND, "批次不存在", f"run_id={run_id}")

    checkpoint = archive_service.load_checkpoint(run)
    attachments = db.execute(select(Attachment).order_by(Attachment.scan_seq)).scalars().all()
    items: list[dict] = []
    outcome_tally: dict[str, int] = {}
    for att in attachments:
        key = str(att.id)
        outcome = checkpoint.get(key)
        if not outcome:
            # checkpoint 没记到的件：中断点之后的未处理件，或试运行下按当前状态展示
            if run.status == RunStatus.INTERRUPTED.value and int(att.scan_seq or 0) > int(
                run.checkpoint_seq or 0
            ):
                outcome = RunItemOutcome.UNTOUCHED.value
            elif att.status == "archived":
                outcome = RunItemOutcome.UNCHANGED.value
            elif att.status in ("duplicate", "pending", "unknown"):
                outcome = att.status
            else:
                outcome = RunItemOutcome.UNTOUCHED.value
        outcome_tally[outcome] = outcome_tally.get(outcome, 0) + 1
        items.append(
            {
                "attachment_id": key,
                "scan_seq": att.scan_seq,
                "source_code": att.source_code,
                "original_name": att.original_name,
                "stored_name": att.stored_name,
                "material_type": att.material_type,
                "status": att.status,
                "outcome": outcome,
                "outcome_label": RUN_ITEM_OUTCOME_LABELS.get(outcome, outcome),
                "sha256_short": att.sha256[:12],
                "sequence_no": att.sequence_no,
                "in_checkpoint": key in checkpoint,
            }
        )
    return {
        "run_id": str(run.id),
        "run_no": run.run_no,
        "status": run.status,
        "checkpoint_seq": int(run.checkpoint_seq or 0),
        "outcome_tally": outcome_tally,
        "items": items,
    }


def reconcile(db: Session, user: User | None = None) -> dict:
    payload = reconcile_service.reconcile(db)
    audit_service.record(
        db,
        action=ACTION_RUN_RECONCILE,
        actor=user,
        entity_type="archive_run",
        entity_id="",
        detail={"issue_total": payload["summary"]["issue_total"], "clean": payload["summary"]["clean"]},
    )
    return payload
