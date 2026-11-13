"""断点续跑与逐件对账：用 SQLite 内存库 + 真实 ORM/服务跑完整归档链路。

覆盖：
  1. 落盘中途失败 -> 批次 interrupted，逐件 checkpoint 与落盘目录一致；
  2. 续跑 -> 已落盘件 REUSED 不重复写盘、未处理件继续归档、文件缺失件 REPAIRED 补写；
  3. 安全落盘：同名不同内容拒绝覆盖（FileExistsError），同内容幂等复用；
  4. 对账：matched / ledger_only / orphan_file / content_mismatch 全部可识别且只读；
  5. 试运行：不写库字段、不写磁盘、不产生 checkpoint；
  6. 完整成功批次：checkpoint 逐件可查、计数守恒。
"""
from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.config.settings import settings
from src.constants.enums import RunItemOutcome, RunMode, RunStatus
from src.models.archive_run import ArchiveRun
from src.models.attachment import Attachment
from src.models.base import Base
from src.models.claim_case import ClaimCase
from src.services import archive_service, reconcile_service, run_service

TEMPLATE = "{保单号}_{材料类型}_{YYYYMMDD}_{序号:02d}"


@pytest.fixture()
def archive_store(tmp_path, monkeypatch) -> Path:
    store = tmp_path / "archive_store"
    store.mkdir()
    monkeypatch.setattr(settings, "archive_store", str(store))
    return store


@pytest.fixture()
def db(archive_store):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS docflow"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_case(db: Session, index: int, policy: str | None = None) -> ClaimCase:
    case = ClaimCase(
        case_no=f"C-{index:03d}",
        policy_no=policy or f"P2026{index:08d}",
        customer_name=f"客户{index}",
        insurance_type="medical",
        status="pending_docs",
        incident_date=date(2026, 10, 1),
    )
    db.add(case)
    db.flush()
    return case


def _make_attachment(
    db: Session,
    seq: int,
    *,
    case: ClaimCase | None = None,
    material_type: str = "invoice",
    status: str = "classified",
    confidence: float = 0.92,
    content: str | None = None,
    policy: str = "",
) -> Attachment:
    content = content if content is not None else f"content-{seq}"
    att = Attachment(
        case_id=case.id if case else None,
        scan_seq=seq,
        source_code="shared_drive",
        original_name=f"IMG_{seq}.jpg",
        material_type=material_type,
        confidence=confidence,
        status=status,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        size_bytes=len(content),
        ext=".jpg",
        content_sample=content,
        match_detail={},
        policy_no=policy,
        doc_date=date(2026, 10, 2),
    )
    db.add(att)
    db.flush()
    return att


def _make_run(db: Session, *, resumed_from: ArchiveRun | None = None) -> ArchiveRun:
    seq = len(db.execute(select(ArchiveRun)).scalars().all()) + 1
    run = ArchiveRun(
        run_no=f"RUN-20261112-{seq:02d}",
        mode=RunMode.ARCHIVE.value,
        status=RunStatus.QUEUED.value,
        rule_version=1,
        template_snapshot=TEMPLATE,
        triggered_by_email="analyst@docflow.io",
        resumed_from_id=resumed_from.id if resumed_from else None,
    )
    db.add(run)
    db.flush()
    return run


# ------------------------------------------------------------- 安全落盘 ----


def test_safe_write_refuses_overwrite_and_is_idempotent(db, archive_store):
    target = "P1_发票_20261002_01.jpg"
    path = archive_service.safe_write_payload(target, "AAA")
    assert os.path.exists(path)

    # 同内容再写：幂等复用，不报错、不改动
    again = archive_service.safe_write_payload(target, "AAA")
    assert again == path

    # 不同内容：拒绝覆盖，原文件字节不变
    with pytest.raises(FileExistsError):
        archive_service.safe_write_payload(target, "BBB")
    assert Path(path).read_bytes() == b"AAA"
    # 临时文件已清理
    assert not [name for name in os.listdir(archive_store) if name.startswith(".tmp-")]


# ------------------------------------------------------------- 中断/续跑 ----


def test_interrupted_run_persists_checkpoint_and_disk_together(db, archive_store, monkeypatch):
    case = _make_case(db, 1)
    attachments = [
        _make_attachment(db, seq, case=case, content=f"材料-{seq}") for seq in range(1, 7)
    ]
    run = _make_run(db)

    real_write = archive_service.safe_write_payload
    failed = {"done": 0}

    def flaky_write(stored_name, content_sample, expected_sha256=""):
        result = real_write(stored_name, content_sample, expected_sha256)
        failed["done"] += 1
        if failed["done"] == 3:
            raise OSError("模拟落盘卷 IO 中断")
        return result

    monkeypatch.setattr(archive_service, "safe_write_payload", flaky_write)
    archive_service.execute_run(db, run)

    assert run.status == RunStatus.INTERRUPTED.value
    assert run.checkpoint_seq == attachments[1].scan_seq  # 断点：第 2 件已落账，第 3 件写盘即失败
    checkpoint = archive_service.load_checkpoint(run)
    # 前两件已逐件提交为 archived，第三件写盘即抛错、未进 checkpoint
    archived_ids = {str(att.id) for att in attachments[:2]}
    assert archived_ids.issubset(checkpoint.keys())
    assert str(attachments[2].id) not in checkpoint
    assert all(value == RunItemOutcome.ARCHIVED.value for value in checkpoint.values())

    db.expire_all()
    first_two = db.get(Attachment, attachments[0].id), db.get(Attachment, attachments[1].id)
    assert all(att.status == "archived" and att.stored_name for att in first_two)
    rest = [db.get(Attachment, att.id) for att in attachments[2:]]
    assert all(att.status == "classified" and att.stored_name is None for att in rest)
    # 台账两件都在目录里；失败件物理上已写盘但未入账 -> 一个等对账识别的孤儿文件
    disk_names = set(archive_service.list_stored_files())
    ledger_names = {att.stored_name for att in first_two}
    assert ledger_names.issubset(disk_names)
    assert len(disk_names) == len(ledger_names) + 1
    report = reconcile_service.reconcile(db)
    assert report["summary"]["orphan_file"] == 1
    assert run.failed == 1


def test_resume_reuses_landed_files_and_continues_remaining(db, archive_store, monkeypatch):
    case = _make_case(db, 1)
    attachments = [
        _make_attachment(db, seq, case=case, content=f"材料-{seq}") for seq in range(1, 7)
    ]
    interrupted = _make_run(db)

    real_write = archive_service.safe_write_payload
    write_calls = {"n": 0}

    def flaky_write(stored_name, content_sample, expected_sha256=""):
        write_calls["n"] += 1
        if write_calls["n"] == 3:
            raise OSError("模拟落盘卷 IO 中断")
        return real_write(stored_name, content_sample, expected_sha256)

    monkeypatch.setattr(archive_service, "safe_write_payload", flaky_write)
    archive_service.execute_run(db, interrupted)
    assert interrupted.status == RunStatus.INTERRUPTED.value

    disk_before = set(archive_service.list_stored_files())
    # 恢复真实落盘，统计续跑阶段的实际写盘次数
    writes_after_resume = {"n": 0}

    def counting_write(stored_name, content_sample, expected_sha256=""):
        writes_after_resume["n"] += 1
        return real_write(stored_name, content_sample, expected_sha256)

    monkeypatch.setattr(archive_service, "safe_write_payload", counting_write)

    resume = _make_run(db, resumed_from=interrupted)
    archive_service.execute_run(db, resume)

    assert resume.status == RunStatus.SUCCEEDED.value
    assert str(resume.resumed_from_id) == str(interrupted.id)
    checkpoint = archive_service.load_checkpoint(resume)
    # 前两件走 REUSED：不重复落盘
    reused = [att for att in attachments[:2] if checkpoint[str(att.id)] == RunItemOutcome.REUSED.value]
    assert len(reused) == 2
    # 剩余 4 件全部新落盘（含第三件重试），复用件一次写盘都没有
    assert writes_after_resume["n"] == 4
    for att in attachments[2:]:
        assert checkpoint[str(att.id)] == RunItemOutcome.ARCHIVED.value

    db.expire_all()
    assert all(db.get(Attachment, att.id).status == "archived" for att in attachments)
    # 目录文件没有被重复写：6 件 6 个唯一文件名
    all_names = [db.get(Attachment, att.id).stored_name for att in attachments]
    assert len(set(all_names)) == 6
    assert set(archive_service.list_stored_files()) == set(all_names)
    assert disk_before.issubset(set(archive_service.list_stored_files()))
    assert resume.archived == 4
    assert run_service._checkpoint_stats(resume)[RunItemOutcome.REUSED.value] == 2


def test_resume_repairs_ledger_archived_file_missing(db, archive_store):
    case = _make_case(db, 1)
    att = _make_attachment(db, 1, case=case, content="唯一材料")
    interrupted = _make_run(db)
    # 模拟“台账已 archived、文件也落过盘”的中断现场
    target = archive_service.safe_write_payload("P1_发票_20261002_01.jpg", att.content_sample)
    att.status = "archived"
    att.stored_name = os.path.basename(target)
    interrupted.status = RunStatus.INTERRUPTED.value
    interrupted.checkpoint_seq = att.scan_seq
    interrupted.checkpoint = {str(att.id): RunItemOutcome.ARCHIVED.value}
    interrupted.scanned = 1
    db.commit()
    # 目录文件被外部清掉
    os.unlink(target)
    assert not os.path.exists(target)

    resume = _make_run(db, resumed_from=interrupted)
    archive_service.execute_run(db, resume)

    assert resume.status == RunStatus.SUCCEEDED.value
    assert archive_service.load_checkpoint(resume)[str(att.id)] == RunItemOutcome.REPAIRED.value
    assert os.path.exists(target)
    assert run_service._checkpoint_stats(resume)[RunItemOutcome.REPAIRED.value] == 1
    assert resume.archived == 1


def test_resume_service_rejects_non_interrupted_run(db):
    from src.models.user import User
    from src.utils.errors import AppError
    from src.constants import error_codes as codes

    user = User(
        email="analyst@docflow.io",
        password_hash="x",
        display_name="受理员",
        role="analyst",
    )
    db.add(user)
    db.flush()
    run = _make_run(db)
    run.status = RunStatus.FAILED.value
    db.commit()

    with pytest.raises(AppError) as exc:
        run_service.resume_run(db, user=user, run_id=str(run.id))
    assert exc.value.code == codes.RUN_NOT_RESUMABLE


# ------------------------------------------------------------- 逐件对账 ----


def test_reconcile_clean_when_ledger_matches_disk(db, archive_store):
    case = _make_case(db, 1)
    att = _make_attachment(db, 1, case=case, content="对账材料")
    run = _make_run(db)
    archive_service.execute_run(db, run)

    report = reconcile_service.reconcile(db)
    assert report["summary"]["clean"] is True
    assert report["summary"]["matched"] == 1
    assert report["summary"]["issue_total"] == 0
    assert report["rows"][0]["status"] == "matched"
    assert report["rows"][0]["disk_sha256"] == att.sha256


def test_reconcile_detects_ledger_only_orphan_and_mismatch(db, archive_store):
    case = _make_case(db, 1)
    a = _make_attachment(db, 1, case=case, content="甲")
    b = _make_attachment(db, 2, case=case, content="乙")
    run = _make_run(db)
    archive_service.execute_run(db, run)
    name_a, name_b = a.stored_name, b.stored_name

    # 1) 台账有、目录缺
    os.unlink(archive_service.stored_path(name_a))
    # 2) 内容被篡改 -> 哈希不符
    (archive_store / name_b).write_bytes("被篡改的内容".encode("utf-8"))
    # 3) 目录有、台账缺的孤儿文件
    (archive_store / "P9_孤儿_20261002_99.jpg").write_bytes(b"nobody")

    report = reconcile_service.reconcile(db)
    summary = report["summary"]
    assert summary["clean"] is False
    assert summary["ledger_only"] == 1
    assert summary["content_mismatch"] == 1
    assert summary["orphan_file"] == 1
    statuses = {row["stored_name"]: row["status"] for row in report["rows"]}
    assert statuses[name_a] == "ledger_only"
    assert statuses[name_b] == "content_mismatch"
    assert statuses["P9_孤儿_20261002_99.jpg"] == "orphan_file"

    # 只读：对账不改变目录与台账
    assert not os.path.exists(archive_service.stored_path(name_a))
    assert (archive_store / name_b).read_bytes() == "被篡改的内容".encode("utf-8")
    db.expire_all()
    assert db.get(Attachment, b.id).status == "archived"


# ------------------------------------------------------------- 试运行 ----


def test_dry_run_changes_nothing(db, archive_store):
    case = _make_case(db, 1)
    attachments = [_make_attachment(db, seq, case=case, content=f"试-{seq}") for seq in range(1, 4)]
    run = ArchiveRun(
        run_no="RUN-DRY-01",
        mode=RunMode.DRY_RUN.value,
        status=RunStatus.QUEUED.value,
        template_snapshot=TEMPLATE,
    )
    db.add(run)
    db.flush()
    archive_service.execute_run(db, run, mode=RunMode.DRY_RUN.value)

    assert run.status == RunStatus.SUCCEEDED.value
    assert run.archived == 3
    assert dict(run.checkpoint or {}) == {}
    assert run.checkpoint_seq == 0
    assert archive_service.list_stored_files() == []
    db.expire_all()
    for att in attachments:
        assert db.get(Attachment, att.id).status == "classified"
        assert db.get(Attachment, att.id).stored_name is None


# ------------------------------------------------------------- 成功批次 ----


def test_successful_run_checkpoint_covers_every_item_and_counts_conserve(db, archive_store):
    case = _make_case(db, 1)
    good = [_make_attachment(db, seq, case=case, content=f"好-{seq}") for seq in range(1, 4)]
    pending = _make_attachment(db, 4, case=case, confidence=0.3, status="pending")
    unknown = _make_attachment(db, 5, material_type="unknown", status="unknown", confidence=0.0)
    run = _make_run(db)
    archive_service.execute_run(db, run)

    assert run.status == RunStatus.SUCCEEDED.value
    checkpoint = archive_service.load_checkpoint(run)
    all_atts = good + [pending, unknown]
    assert set(checkpoint.keys()) == {str(att.id) for att in all_atts}
    assert run.checkpoint_seq == max(att.scan_seq for att in all_atts)
    tally = run_service._checkpoint_stats(run)
    assert tally[RunItemOutcome.ARCHIVED.value] == 3
    assert tally[RunItemOutcome.PENDING.value] == 1
    assert tally[RunItemOutcome.UNKNOWN.value] == 1
    assert run.scanned == 5
    assert run.archived + run.pending + run.unknown == 5

    # run_items 视图能逐件还原结果
    view = run_service.list_run_items(db, str(run.id))
    assert view["checkpoint_seq"] == run.checkpoint_seq
    assert len(view["items"]) == 5
    assert {item["outcome"] for item in view["items"]} >= {"archived", "pending", "unknown"}


def test_non_archived_items_are_committed_to_checkpoint_per_item(db, archive_store, monkeypatch):
    """回归：待确认/未识别件也必须逐件提交进 checkpoint，不能只搭车后续归档件的提交。"""
    case = _make_case(db, 1)
    # 第一件就是未识别件，之后的归档件必然失败 -> 中断时第一件必须已落 checkpoint
    first = _make_attachment(db, 1, material_type="unknown", status="unknown", confidence=0.0)
    second = _make_attachment(db, 2, case=case, content="第二件")
    run = _make_run(db)

    real_write = archive_service.safe_write_payload

    def fail_on_archive(stored_name, content_sample, expected_sha256=""):
        raise OSError("模拟第二件落盘失败")

    monkeypatch.setattr(archive_service, "safe_write_payload", fail_on_archive)
    archive_service.execute_run(db, run)

    assert run.status == RunStatus.INTERRUPTED.value
    checkpoint = archive_service.load_checkpoint(run)
    # 未识别件在归档件失败前已逐件提交，断点保留得下来
    assert checkpoint[str(first.id)] == RunItemOutcome.UNKNOWN.value
    assert str(second.id) not in checkpoint
    assert run.checkpoint_seq == first.scan_seq

    # 完整成功批次：processed 必须覆盖全部件，untouched 为 0（旧实现这里会少算非归档件）
    monkeypatch.setattr(archive_service, "safe_write_payload", real_write)
    full = _make_run(db)
    for seq in range(10, 13):
        _make_attachment(db, seq, case=case, content=f"全量-{seq}")
    _make_attachment(db, 13, case=case, confidence=0.3, status="pending")
    archive_service.execute_run(db, full)
    assert full.status == RunStatus.SUCCEEDED.value
    # 库中 6 件（首批 2 + 新增 4）全部逐件进入 checkpoint，含待确认/未识别
    stats = run_service._checkpoint_stats(full)
    assert sum(stats.values()) == 6
    assert stats[RunItemOutcome.UNTOUCHED.value] == 0
    assert full.scanned == 6
    # 每件都进了 checkpoint（含待确认/未识别），没有未处理件
    stats = run_service._checkpoint_stats(full)
    assert stats[RunItemOutcome.UNTOUCHED.value] == 0
    assert full.scanned == sum(stats.values())
