"""ArchiveRun：归档批次（扫描/归档/待确认/重复/失败计数 + 日志 + 断点）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.config.settings import settings
from src.models.base import Base, new_uuid, utcnow


class ArchiveRun(Base):
    __tablename__ = "archive_runs"
    __table_args__ = {"schema": settings.db_schema}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    run_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="archive")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    template_snapshot: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    triggered_by_email: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unknown: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manual: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 断点续跑：resumed_from_id 指向被续跑的中断批次；
    # checkpoint 逐件记录 {attachment_id: outcome}，中断后可凭它区分已落盘件与未处理件。
    resumed_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{settings.db_schema}.archive_runs.id"),
        nullable=True,
        index=True,
    )
    checkpoint_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    log: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
