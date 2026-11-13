"""共享枚举（后端定义）。

前端镜像文件：frontend/src/constants/enums.ts（两份必须一致，README 列出全部出现位置）。
"""
from __future__ import annotations

from enum import Enum


class MaterialType(str, Enum):
    """材料类型（算法 1 的输出）。"""

    INVOICE = "invoice"          # 发票
    DIAGNOSIS = "diagnosis"      # 诊断书
    ID_CARD = "id_card"          # 身份证
    BANK_CARD = "bank_card"      # 银行卡
    DISCHARGE = "discharge"      # 出院小结
    UNKNOWN = "unknown"          # 未识别


class CaseStatus(str, Enum):
    """案件状态（工作台三个抽屉）。"""

    PENDING_DOCS = "pending_docs"  # 待收件
    ARCHIVED = "archived"          # 已归档
    EXCEPTION = "exception"        # 异常


class AttachmentStatus(str, Enum):
    """附件状态。"""

    CLASSIFIED = "classified"  # 已识别，待归档
    PENDING = "pending"        # 待确认（置信不足或缺保单号）
    UNKNOWN = "unknown"        # 未识别类型
    ARCHIVED = "archived"      # 已重命名落盘
    DUPLICATE = "duplicate"    # 内容重复，不落盘


class RunStatus(str, Enum):
    """归档批次状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # 落盘中途失败：已处理件可复用，支持断点续跑


class RunItemOutcome(str, Enum):
    """批次内单件处理结果（写入批次 checkpoint，供断点续跑与逐件对账）。"""

    ARCHIVED = "archived"        # 本次落盘成功
    DUPLICATE = "duplicate"      # 重复件，不落盘
    PENDING = "pending"          # 待人工确认
    UNKNOWN = "unknown"          # 未识别
    UNTOUCHED = "untouched"      # 中断点之后，本批次尚未处理
    REUSED = "reused"            # 续跑时复用已确认落盘的结果，不重复写盘
    REPAIRED = "repaired"        # 续跑时发现台账已归档但文件缺失，补写落盘
    UNCHANGED = "unchanged"      # 此前已归档，本批次只计数未改动


class ReconcileStatus(str, Enum):
    """归档目录与批次台账逐件对账结果。"""

    MATCHED = "matched"          # 台账与落盘文件一致（含内容哈希）
    LEDGER_ONLY = "ledger_only"  # 台账有记录、目录无文件（落盘丢失）
    ORPHAN_FILE = "orphan_file"  # 目录有文件、台账无记录（无人认领的落盘）
    CONTENT_MISMATCH = "content_mismatch"  # 文件存在但内容哈希与台账不符
    NAME_MISMATCH = "name_mismatch"        # 同一档案名被另一个附件占用


class RunMode(str, Enum):
    """批次模式。"""

    DRY_RUN = "dry_run"  # 试运行：只统计不落盘
    ARCHIVE = "archive"  # 正式归档


class InsuranceType(str, Enum):
    """险种。"""

    MEDICAL = "medical"        # 医疗险
    CRITICAL = "critical"      # 重疾险
    ACCIDENT = "accident"      # 意外险
    AUTO = "auto"              # 车险
    LIFE = "life"              # 寿险


class IngestSource(str, Enum):
    """投件源（3 个 fixture 目录）。"""

    SHARED_DRIVE = "shared_drive"  # 共享盘投件
    MAILBOX = "mailbox"            # 理赔邮箱附件
    PORTABLE = "portable"          # 移动盘补件


MATERIAL_TYPE_LABELS: dict[str, str] = {
    MaterialType.INVOICE.value: "发票",
    MaterialType.DIAGNOSIS.value: "诊断书",
    MaterialType.ID_CARD.value: "身份证",
    MaterialType.BANK_CARD.value: "银行卡",
    MaterialType.DISCHARGE.value: "出院小结",
    MaterialType.UNKNOWN.value: "未识别",
}

# 命名模板里出现的中文材料标签（与文件名一一对应）
MATERIAL_TYPE_FILE_TOKENS: dict[str, str] = {
    MaterialType.INVOICE.value: "发票",
    MaterialType.DIAGNOSIS.value: "诊断书",
    MaterialType.ID_CARD.value: "身份证",
    MaterialType.BANK_CARD.value: "银行卡",
    MaterialType.DISCHARGE.value: "出院小结",
    MaterialType.UNKNOWN.value: "未识别",
}

CASE_STATUS_LABELS: dict[str, str] = {
    CaseStatus.PENDING_DOCS.value: "待收件",
    CaseStatus.ARCHIVED.value: "已归档",
    CaseStatus.EXCEPTION.value: "异常",
}

ATTACHMENT_STATUS_LABELS: dict[str, str] = {
    AttachmentStatus.CLASSIFIED.value: "已识别",
    AttachmentStatus.PENDING.value: "待确认",
    AttachmentStatus.UNKNOWN.value: "未识别",
    AttachmentStatus.ARCHIVED.value: "已归档",
    AttachmentStatus.DUPLICATE.value: "重复件",
}

RUN_STATUS_LABELS: dict[str, str] = {
    RunStatus.QUEUED.value: "排队中",
    RunStatus.RUNNING.value: "执行中",
    RunStatus.SUCCEEDED.value: "已完成",
    RunStatus.INTERRUPTED.value: "已中断（可续跑）",
    RunStatus.FAILED.value: "失败",
}

RUN_ITEM_OUTCOME_LABELS: dict[str, str] = {
    RunItemOutcome.ARCHIVED.value: "本批落盘",
    RunItemOutcome.DUPLICATE.value: "重复件",
    RunItemOutcome.PENDING.value: "待确认",
    RunItemOutcome.UNKNOWN.value: "未识别",
    RunItemOutcome.UNTOUCHED.value: "未处理",
    RunItemOutcome.REUSED.value: "已恢复复用",
    RunItemOutcome.REPAIRED.value: "补写恢复",
    RunItemOutcome.UNCHANGED.value: "已归档未变",
}

RECONCILE_STATUS_LABELS: dict[str, str] = {
    ReconcileStatus.MATCHED.value: "账实一致",
    ReconcileStatus.LEDGER_ONLY.value: "台账有、目录缺",
    ReconcileStatus.ORPHAN_FILE.value: "目录有、台账缺",
    ReconcileStatus.CONTENT_MISMATCH.value: "内容不一致",
    ReconcileStatus.NAME_MISMATCH.value: "命名冲突",
}

RUN_MODE_LABELS: dict[str, str] = {
    RunMode.DRY_RUN.value: "试运行",
    RunMode.ARCHIVE.value: "正式归档",
}

INSURANCE_TYPE_LABELS: dict[str, str] = {
    InsuranceType.MEDICAL.value: "医疗险",
    InsuranceType.CRITICAL.value: "重疾险",
    InsuranceType.ACCIDENT.value: "意外险",
    InsuranceType.AUTO.value: "车险",
    InsuranceType.LIFE.value: "寿险",
}

SOURCE_NAMES: dict[str, str] = {
    IngestSource.SHARED_DRIVE.value: "共享盘投件",
    IngestSource.MAILBOX.value: "理赔邮箱附件",
    IngestSource.PORTABLE.value: "移动盘补件",
}

SOURCE_LABELS: dict[str, str] = {
    IngestSource.SHARED_DRIVE.value: "\\\\fileserver\\claims-drop（共享盘投件）",
    IngestSource.MAILBOX.value: "claims@hangan-broker.cn（理赔邮箱附件）",
    IngestSource.PORTABLE.value: "移动盘补件（现场带回）",
}

# 每个险种应收材料（用于缺失材料提示与归档完整性）
EXPECTED_MATERIALS: dict[str, tuple[str, ...]] = {
    InsuranceType.MEDICAL.value: (
        MaterialType.INVOICE.value,
        MaterialType.DIAGNOSIS.value,
        MaterialType.ID_CARD.value,
        MaterialType.BANK_CARD.value,
    ),
    InsuranceType.CRITICAL.value: (
        MaterialType.DIAGNOSIS.value,
        MaterialType.DISCHARGE.value,
        MaterialType.ID_CARD.value,
        MaterialType.BANK_CARD.value,
    ),
    InsuranceType.ACCIDENT.value: (
        MaterialType.INVOICE.value,
        MaterialType.DIAGNOSIS.value,
        MaterialType.ID_CARD.value,
    ),
    InsuranceType.AUTO.value: (
        MaterialType.INVOICE.value,
        MaterialType.ID_CARD.value,
        MaterialType.BANK_CARD.value,
    ),
    InsuranceType.LIFE.value: (
        MaterialType.ID_CARD.value,
        MaterialType.BANK_CARD.value,
        MaterialType.DISCHARGE.value,
    ),
}

# 算法 1 的四路特征权重（后端真算，前端只展示）
FEATURE_WEIGHTS: dict[str, float] = {
    "filename": 0.45,
    "content": 0.35,
    "regex": 0.15,
    "extension": 0.05,
}
