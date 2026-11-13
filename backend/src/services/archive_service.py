"""归档批次执行：扫描投件源 -> 分类复核 -> 命名归一 -> 落盘/重复标记 -> 案件状态回写。

幂等口径：
  - 已 archived 的附件再跑只计入 unchanged，不重复落盘、不改名；
  - 已 duplicate 的附件再跑仍计入 duplicate（不落盘）；
  - 试运行（dry_run）只统计，不写库字段、不写磁盘。

断点续跑口径：
  - 正式归档逐件提交：先安全落盘（不覆盖同名文件），再回写附件与批次 checkpoint；
  - 中途异常 -> 批次置 interrupted，checkpoint 逐件记录已处理结果，落盘点即断点；
  - 续跑生成新批次（resumed_from_id 回指中断批次）：已确认落盘的材料按 REUSED 复用、
    台账已归档但文件缺失的按 REPAIRED 补写，未处理件继续归档，全程不重复落盘、不覆盖。
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.constants.enums import (
    AttachmentStatus,
    CaseStatus,
    RunItemOutcome,
    RunMode,
    RunStatus,
)
from src.constants.log_templates import ACTION_RUN_EXECUTE, ACTION_RUN_FAILED
from src.models.archive_run import ArchiveRun
from src.models.attachment import Attachment
from src.models.claim_case import ClaimCase
from src.services import audit_service
from src.services.naming_engine import plan_target
from src.utils.logger import get_logger
from src.utils.textutils import now_stamp

logger = get_logger("docflow.archive")
LOG_LIMIT = 80
CHECKPOINT_FLUSH_EVERY = 10


def archive_dir() -> str:
    path = os.path.abspath(settings.archive_store)
    os.makedirs(path, exist_ok=True)
    return path


def stored_file_count() -> int:
    return len(list_stored_files())


def list_stored_files() -> list[str]:
    """归档目录里的文件名清单（只列普通文件，子目录不参与对账）。"""
    try:
        base = archive_dir()
        return sorted(
            name
            for name in os.listdir(base)
            if os.path.isfile(os.path.join(base, name))
        )
    except OSError:
        return []


def stored_path(stored_name: str) -> str:
    return os.path.join(archive_dir(), os.path.basename(stored_name))


def safe_write_payload(stored_name: str, content_sample: str, expected_sha256: str = "") -> str:
    """安全落盘：原子替换、绝不覆盖已有文件。

    - 目标已存在且内容一致：直接复用，不重写；
    - 目标已存在但内容不同：抛错，由调用方把批次置为中断（避免覆盖同名材料）；
    - 目标不存在：先写同目录临时文件再 rename，保证目录里不会出现写了一半的文件。
    """
    target = stored_path(stored_name)
    payload = (content_sample or "").encode("utf-8")
    if os.path.exists(target):
        with open(target, "rb") as handle:
            existing = handle.read()
        if existing == payload:
            return target
        raise FileExistsError(f"落盘目标已存在且内容不同，拒绝覆盖：{stored_name}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_path, target)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return target


def write_payload(stored_name: str, content_sample: str) -> str:
    """兼容旧调用方的落盘入口（demo 用内容抽样代替二进制原件）。"""
    return safe_write_payload(stored_name, content_sample)


def append_log(run: ArchiveRun, message: str) -> None:
    line = f"[{now_stamp()}] {message}"
    lines = list(run.log or [])
    lines.append(line)
    run.log = lines[-LOG_LIMIT:]
    print(f"[run {run.run_no}] {message}", flush=True)


def build_name_maps(db: Session) -> tuple[dict[str, str], dict[str, str]]:
    """已占用名字表：name -> sha256，以及 name -> 附件ID（重复件回指用）。"""
    rows = db.execute(select(Attachment).where(Attachment.stored_name.is_not(None))).scalars().all()
    used = {row.stored_name: row.sha256 for row in rows if row.stored_name}
    owners = {row.stored_name: str(row.id) for row in rows if row.stored_name}
    return used, owners


def load_checkpoint(run: ArchiveRun) -> dict[str, str]:
    """checkpoint: {attachment_id(str): outcome}，旧批次缺字段时退化为空。"""
    raw = run.checkpoint or {}
    return {str(key): str(value) for key, value in raw.items()}


def _mark_checkpoint(run: ArchiveRun, att: Attachment, outcome: str) -> None:
    checkpoint = dict(run.checkpoint or {})
    checkpoint[str(att.id)] = outcome
    run.checkpoint = checkpoint
    run.checkpoint_seq = max(int(run.checkpoint_seq or 0), int(att.scan_seq or 0))


def recompute_case_statuses(db: Session) -> None:
    grouped: dict[str, list[Attachment]] = {}
    for att in db.execute(select(Attachment)).scalars().all():
        if att.case_id is None:
            continue
        grouped.setdefault(str(att.case_id), []).append(att)
    for case in db.execute(select(ClaimCase)).scalars().all():
        attachments = grouped.get(str(case.id), [])
        if not attachments:
            new_status = CaseStatus.PENDING_DOCS.value
        elif any(
            att.status in (AttachmentStatus.PENDING.value, AttachmentStatus.UNKNOWN.value) for att in attachments
        ):
            new_status = CaseStatus.EXCEPTION.value
        elif all(
            att.status in (AttachmentStatus.ARCHIVED.value, AttachmentStatus.DUPLICATE.value)
            for att in attachments
        ):
            new_status = CaseStatus.ARCHIVED.value
        else:
            new_status = CaseStatus.PENDING_DOCS.value
        if case.status != new_status:
            case.status = new_status


def _source_tally(attachments: list[Attachment]) -> str:
    tally: dict[str, int] = {}
    for att in attachments:
        tally[att.source_code] = tally.get(att.source_code, 0) + 1
    order = ("shared_drive", "mailbox", "portable")
    labels = {"shared_drive": "共享盘", "mailbox": "邮箱", "portable": "移动盘"}
    return " / ".join(f"{labels.get(code, code)} {tally.get(code, 0)}" for code in order)


def _resume_map(db: Session, source_run: ArchiveRun) -> dict[uuid.UUID, str]:
    """被续跑批次 checkpoint：attachment_id(UUID) -> outcome。"""
    out: dict[uuid.UUID, str] = {}
    for key, value in (source_run.checkpoint or {}).items():
        try:
            out[uuid.UUID(str(key))] = str(value)
        except (ValueError, TypeError):
            continue
    return out


def _finalize_counters(run: ArchiveRun, counters: dict[str, int]) -> None:
    run.scanned = counters["scanned"]
    run.new_files = counters["new_files"]
    run.archived = counters["archived"]
    run.duplicate = counters["duplicate"]
    run.pending = counters["pending"]
    run.unknown = counters["unknown"]
    run.unchanged = counters["unchanged"]
    run.failed = counters["failed"]


def execute_run(db: Session, run: ArchiveRun, mode: str | None = None) -> ArchiveRun:
    source_mode = (mode or run.mode or RunMode.ARCHIVE.value).strip()
    dry_run = source_mode == RunMode.DRY_RUN.value
    started = time.perf_counter()

    # 续跑上下文：resumed_from_id 在创建续跑批次时写入；试运行不产生断点、不可续跑。
    source_run: ArchiveRun | None = None
    resume_map: dict[uuid.UUID, str] = {}
    if not dry_run and run.resumed_from_id is not None:
        source_run = db.get(ArchiveRun, run.resumed_from_id)
        if source_run is not None:
            resume_map = _resume_map(db, source_run)
            run.rule_version = source_run.rule_version
            run.template_snapshot = source_run.template_snapshot

    run.mode = source_mode
    run.status = RunStatus.RUNNING.value
    run.started_at = datetime.now(timezone.utc)
    run.finished_at = None
    run.log = []
    run.scanned = run.new_files = run.archived = 0
    run.duplicate = run.pending = run.unknown = 0
    run.unchanged = run.failed = 0
    if not dry_run:
        run.checkpoint = {}
        run.checkpoint_seq = 0
    db.commit()

    counters = {
        "scanned": 0,
        "new_files": 0,
        "archived": 0,
        "duplicate": 0,
        "pending": 0,
        "unknown": 0,
        "unchanged": 0,
        "failed": 0,
        "reused": 0,
        "repaired": 0,
        "untouched": 0,
    }
    processed = 0
    try:
        attachments = db.execute(select(Attachment).order_by(Attachment.scan_seq)).scalars().all()
        cases = {str(case.id): case for case in db.execute(select(ClaimCase)).scalars().all()}
        used, owners = build_name_maps(db)
        counters["scanned"] = len(attachments)
        append_log(run, f"扫描投件源 {_source_tally(attachments)}，共 {len(attachments)} 件")
        if dry_run:
            append_log(run, "试运行模式：只统计分类与命名结果，不改名、不落盘")
        elif source_run is not None:
            prior = len(resume_map)
            append_log(run, f"断点续跑：复用中断批次 {source_run.run_no} 的 {prior} 件已处理记录，未处理件继续归档")

        threshold = settings.pending_confidence_threshold
        classified = pending = unknown = 0

        def commit_item(att: Attachment) -> None:
            """逐件提交：每件材料（含待确认/未识别）都走同一个提交点并推进断点计数。"""
            nonlocal processed
            if dry_run:
                return
            db.commit()
            processed += 1
            if processed % CHECKPOINT_FLUSH_EVERY == 0:
                append_log(
                    run,
                    f"进度 {processed}/{len(attachments)}，断点已落在扫描序号 {run.checkpoint_seq:03d}",
                )

        for att in attachments:
            case = cases.get(str(att.case_id)) if att.case_id else None
            if att.first_run_id is None:
                counters["new_files"] += 1
                if not dry_run:
                    att.first_run_id = run.id

            # ------------------------------------------------ 断点续跑：先恢复已处理件 ----
            if not dry_run and att.id in resume_map:
                prior_outcome = resume_map[att.id]
                if att.status == AttachmentStatus.ARCHIVED.value and att.stored_name:
                    if os.path.exists(stored_path(att.stored_name)):
                        # 已确认落盘：复用原结果，绝不重复写盘、不改名
                        counters["unchanged"] += 1
                        counters["reused"] += 1
                        _mark_checkpoint(run, att, RunItemOutcome.REUSED.value)
                        att.last_run_id = run.id
                        commit_item(att)
                        continue
                    # 台账说已归档、目录里却没有文件：补写恢复（仍走安全落盘，不覆盖同名）
                    safe_write_payload(att.stored_name, att.content_sample, att.sha256)
                    used[att.stored_name] = att.sha256
                    owners.setdefault(att.stored_name, str(att.id))
                    counters["archived"] += 1
                    counters["repaired"] += 1
                    _mark_checkpoint(run, att, RunItemOutcome.REPAIRED.value)
                    att.last_run_id = run.id
                    append_log(run, f"件 {att.scan_seq:03d} 台账已归档但文件缺失，已补写 {att.stored_name}")
                    commit_item(att)
                    continue
                if att.status == AttachmentStatus.DUPLICATE.value:
                    counters["duplicate"] += 1
                    _mark_checkpoint(run, att, RunItemOutcome.DUPLICATE.value)
                    att.last_run_id = run.id
                    commit_item(att)
                    continue
                if att.status in (AttachmentStatus.PENDING.value, AttachmentStatus.UNKNOWN.value):
                    if att.status == AttachmentStatus.PENDING.value:
                        counters["pending"] += 1
                        pending += 1
                    else:
                        counters["unknown"] += 1
                        unknown += 1
                    _mark_checkpoint(run, att, prior_outcome or att.status)
                    att.last_run_id = run.id
                    commit_item(att)
                    continue
                # 其余情况落到正常流程重新判定（不会重复落盘，archived 分支有 unchanged 兜底）

            # ------------------------------------------------------------ 分类复核 ----
            if att.material_type == "unknown" or att.status == AttachmentStatus.UNKNOWN.value:
                counters["unknown"] += 1
                unknown += 1
                if not dry_run:
                    att.last_run_id = run.id
                    _mark_checkpoint(run, att, RunItemOutcome.UNKNOWN.value)
                commit_item(att)
                continue
            if att.status == AttachmentStatus.PENDING.value or float(att.confidence) < threshold:
                counters["pending"] += 1
                pending += 1
                if not dry_run:
                    att.last_run_id = run.id
                    _mark_checkpoint(run, att, RunItemOutcome.PENDING.value)
                commit_item(att)
                continue

            classified += 1
            if att.status == AttachmentStatus.ARCHIVED.value:
                counters["unchanged"] += 1
                if not dry_run:
                    att.last_run_id = run.id
                    _mark_checkpoint(run, att, RunItemOutcome.UNCHANGED.value)
                commit_item(att)
                continue
            if att.status == AttachmentStatus.DUPLICATE.value:
                counters["duplicate"] += 1
                if not dry_run:
                    att.last_run_id = run.id
                    _mark_checkpoint(run, att, RunItemOutcome.DUPLICATE.value)
                commit_item(att)
                continue

            policy = att.policy_no or (case.policy_no if case else "")
            doc_date = att.doc_date or (case.incident_date if case else None) or date.today()
            if not policy:
                counters["pending"] += 1
                pending += 1
                if not dry_run:
                    att.status = AttachmentStatus.PENDING.value
                    att.last_run_id = run.id
                    _mark_checkpoint(run, att, RunItemOutcome.PENDING.value)
                commit_item(att)
                continue

            result = plan_target(
                template=run.template_snapshot,
                policy_no=policy,
                material_type=att.material_type,
                doc_date=doc_date,
                ext=att.ext,
                content_sha256=att.sha256,
                used=used,
            )
            if result.action == "archive":
                counters["archived"] += 1
                used[result.stored_name] = att.sha256
                owners[result.stored_name] = str(att.id)
                if not dry_run:
                    # 先安全落盘（同名不同内容会直接抛错，绝不覆盖），再回写台账与断点
                    safe_write_payload(result.stored_name, att.content_sample, att.sha256)
                    att.stored_name = result.stored_name
                    att.sequence_no = result.sequence_no
                    att.policy_no = policy
                    att.status = AttachmentStatus.ARCHIVED.value
                    att.last_run_id = run.id
                    _mark_checkpoint(run, att, RunItemOutcome.ARCHIVED.value)
            elif result.action == "duplicate":
                counters["duplicate"] += 1
                if not dry_run:
                    att.status = AttachmentStatus.DUPLICATE.value
                    att.duplicate_of = owners.get(result.duplicate_name)
                    att.stored_name = None
                    att.last_run_id = run.id
                    _mark_checkpoint(run, att, RunItemOutcome.DUPLICATE.value)
            else:
                counters["pending"] += 1
                pending += 1
                if not dry_run:
                    att.status = AttachmentStatus.PENDING.value
                    att.last_run_id = run.id
                    _mark_checkpoint(run, att, RunItemOutcome.PENDING.value)

            # 逐件提交：已落盘件先于批次终态持久化，崩溃后仍能凭 checkpoint 续跑
            commit_item(att)

        _finalize_counters(run, counters)
        counters["untouched"] = max(counters["scanned"] - processed, 0)

        append_log(
            run,
            f"分类复核：可自动归档 {classified} 件 / 待确认 {pending} 件 / 未识别 {unknown} 件",
        )
        if source_run is not None:
            append_log(
                run,
                f"断点恢复：复用已落盘 {counters['reused']} 件 / 补写恢复 {counters['repaired']} 件，"
                "复用件未重复写盘",
            )
        append_log(
            run,
            "命名归一：归档 {archived} 件 / 重复 {duplicate} 件（内容 SHA-256 相同，不落盘）/ "
            "待确认 {pending} / 未识别 {unknown} / 已归档未变 {unchanged}".format(**counters),
        )
        if dry_run:
            append_log(run, "试运行结束：附件状态与落盘目录均未改动")
        else:
            recompute_case_statuses(db)
            append_log(run, f"落盘目录 archive_store 现有 {stored_file_count()} 个文件，案件状态已回写")
        run.status = RunStatus.SUCCEEDED.value
        audit_service.record(
            db,
            action=ACTION_RUN_EXECUTE,
            entity_type="archive_run",
            entity_id=run.id,
            detail={
                "run_no": run.run_no,
                "mode": source_mode,
                "resumed_from": str(run.resumed_from_id) if run.resumed_from_id else None,
                "scanned": run.scanned,
                "archived": run.archived,
                "duplicate": run.duplicate,
                "pending": run.pending,
                "unknown": run.unknown,
                "reused": counters["reused"],
                "repaired": counters["repaired"],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 失败要落日志并让批次可见、可续跑
        logger.error("run %s failed: %s", run.run_no, exc, exc_info=True)
        db.rollback()  # 只回滚当前未提交件；逐件提交过的落盘结果与 checkpoint 保留
        _finalize_counters(run, counters)
        # 已经逐件处理过 -> 可断点续跑；扫描前就挂掉 -> 普通失败（无件可恢复）
        if dry_run or processed == 0:
            run.status = RunStatus.FAILED.value
            run.failed = counters["failed"] + 1
        else:
            run.status = RunStatus.INTERRUPTED.value
            run.failed = counters["failed"] + 1
            append_log(
                run,
                f"批次在扫描序号 {run.checkpoint_seq:03d} 后中断：已处理 {processed} 件均已落账，"
                "可发起续跑，已落盘材料不会重复写盘",
            )
        append_log(run, f"批次执行失败：{exc}")
        audit_service.record(
            db,
            action=ACTION_RUN_FAILED,
            entity_type="archive_run",
            entity_id=run.id,
            detail={"run_no": run.run_no, "error": str(exc)[:300], "interrupted": run.status == "interrupted"},
        )
    finally:
        run.finished_at = datetime.now(timezone.utc)
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        db.commit()
    return run
