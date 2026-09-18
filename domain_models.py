"""领域事件：规则与成绩服务的唯一事实来源。

所有状态变化都表现为一条不可变、仅追加的领域事件。事件不做删除，
更正通过新事件（裁决、申诉裁决、更正发布）表达，旧值保留可追溯。
每条事件都可携带证据引用（设备日志、裁判表、医疗表、录像等）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvidenceRef:
    """指向原始证据的引用，永远不保存证据本体之外的派生结论。"""

    type: str  # device_log | video | medical_form | judge_sheet | timing_file | appeal_doc
    ref: str  # 外部系统编号或 URI
    digest: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class DomainEvent:
    seq: int | None = None
    occurred_at: str = ""
    issued_by: str = "system"
    role: str = "system"  # admin | official | judge | medical | jury | device
    evidence: tuple[EvidenceRef, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SeasonCreated(DomainEvent):
    season_id: str = ""
    name: str = ""
    year: int = 0


@dataclass(frozen=True)
class EventScheduled(DomainEvent):
    event_id: str = ""
    season_id: str = ""
    city: str = ""
    date: str = ""  # YYYY-MM-DD，比赛日（规则版本按此日绑定）
    name: str = ""
    pinned_rule_version_id: str | None = None  # 显式钉选的规则版本


@dataclass(frozen=True)
class RuleVersionPublished(DomainEvent):
    """带生效日期的规则版本；赛前发布，赛后发布的版本不得回溯旧站。"""

    version_id: str = ""
    season_id: str = ""
    effective_date: str = ""
    published_at: str = ""  # 主办方公开发布日（YYYY-MM-DD），决定绑定窗口
    rules: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class DivisionRegistered(DomainEvent):
    season_id: str = ""
    division_id: str = ""
    name: str = ""


@dataclass(frozen=True)
class AthleteRegistered(DomainEvent):
    athlete_id: str = ""
    season_id: str = ""
    display_name: str = ""  # 公开榜单使用的代号/显示名
    legal_name: str = ""  # 身份细节，仅内部可见
    bib: str = ""
    region: str = ""


@dataclass(frozen=True)
class EligibilityGranted(DomainEvent):
    """报名/资格确认，确定选手在某站的组别。"""

    event_id: str = ""
    athlete_id: str = ""
    division_id: str = ""


@dataclass(frozen=True)
class CheckInRecorded(DomainEvent):
    event_id: str = ""
    athlete_id: str = ""
    checked_in_at: str = ""


@dataclass(frozen=True)
class DeviceReadingRecorded(DomainEvent):
    """一台设备对一个动作的一次原始读数。多设备读数先各自保留。"""

    reading_id: str = ""
    event_id: str = ""
    athlete_id: str = ""
    station_id: str = ""
    device_id: str = ""
    device_class: str = ""  # primary_timing | backup_chip | manual
    value_s: float = 0.0
    captured_at: str = ""


@dataclass(frozen=True)
class JudgeCallRecorded(DomainEvent):
    """裁判判罚（可在申诉成立时被撤销）。"""

    call_id: str = ""
    event_id: str = ""
    athlete_id: str = ""
    station_id: str = ""
    call_type: str = ""  # penalty | no_rep | disqualification
    penalty_s: float = 0.0
    reason: str = ""
    judge_id: str = ""


@dataclass(frozen=True)
class MedicalExemptionGranted(DomainEvent):
    """医疗豁免：缺席按规则记 MED 并给保底分，不按零分；细节仅内部可见。"""

    exemption_id: str = ""
    event_id: str = ""
    athlete_id: str = ""
    reason_code: str = ""
    valid_until: str = ""


@dataclass(frozen=True)
class WithdrawalRecorded(DomainEvent):
    """退赛（含赛中伤退）：记 DNF，与医疗豁免区分。"""

    event_id: str = ""
    athlete_id: str = ""
    reason: str = ""
    on_course: bool = False  # 是否在比赛过程中（如伤退）


@dataclass(frozen=True)
class MakeupApproved(DomainEvent):
    """批准补赛（可异地），补赛成绩归属原站。"""

    event_id: str = ""  # 原站
    athlete_id: str = ""
    reason: str = ""
    approved_by: str = ""


@dataclass(frozen=True)
class MakeupResultRecorded(DomainEvent):
    makeup_id: str = ""
    for_event_id: str = ""  # 成绩归属的原站（按原站规则计分）
    athlete_id: str = ""
    station_id: str = ""
    value_s: float = 0.0
    location: str = ""  # 实际补赛地点（可异地）
    captured_at: str = ""


@dataclass(frozen=True)
class ReadingsDeduplicated(DomainEvent):
    """多设备对同一动作的读数被判定为重复：保留一条，其余标记去重。"""

    dedupe_id: str = ""
    event_id: str = ""
    athlete_id: str = ""
    station_id: str = ""
    kept_reading_id: str = ""
    dropped_reading_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConflictFlagged(DomainEvent):
    """多设备读数无法自动判定：全部原值保留，等待裁决。"""

    group_id: str = ""
    event_id: str = ""
    athlete_id: str = ""
    station_id: str = ""
    reading_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarkAdjudicated(DomainEvent):
    """对冲突读数的人工裁决；可选取某条读数或直接给出认定值。"""

    group_id: str = ""
    event_id: str = ""
    athlete_id: str = ""
    station_id: str = ""
    chosen_reading_id: str | None = None
    value_s: float = 0.0


@dataclass(frozen=True)
class AppealFiled(DomainEvent):
    """申诉提出即冻结相关名次（该站该组）。"""

    appeal_id: str = ""
    event_id: str = ""
    athlete_id: str = ""
    contested: tuple[str, ...] = ()  # call_id 或 station_id
    summary: str = ""


@dataclass(frozen=True)
class AppealRuled(DomainEvent):
    """申诉裁决。成立时撤销判罚和/或按证据认定新成绩。"""

    appeal_id: str = ""
    event_id: str = ""
    athlete_id: str = ""
    decision: str = ""  # UPHELD 维持 | SUSTAINED 成立
    rescinded_call_ids: tuple[str, ...] = ()
    mark_overrides: tuple[dict[str, Any], ...] = ()  # [{station_id, value_s}]
    note: str = ""


@dataclass(frozen=True)
class CorrectionApplied(DomainEvent):
    """非申诉渠道的官方更正发布（如补送计时证据后改判），证据强制。"""

    correction_id: str = ""
    event_id: str = ""
    reason: str = ""
    trigger_refs: tuple[str, ...] = ()  # 触发来源（裁决号等）


@dataclass(frozen=True)
class StandingsPublished(DomainEvent):
    """不可变名次快照（分站或赛季）。更正产生新快照并指向上一版。"""

    scope: str = ""  # event | season
    owner_id: str = ""  # event_id 或 season_id
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AwardPosted(DomainEvent):
    """奖金账本分录。更正以红冲/补发表达，旧分录永不删除。"""

    award_id: str = ""
    scope: str = ""
    owner_id: str = ""
    snapshot_id: str = ""
    athlete_id: str = ""
    place: int = 0
    amount: float = 0.0  # 红冲为负数
    currency: str = "CNY"
    kind: str = ""  # event | season
    status: str = ""  # POSTED 首发 | REVERSAL 红冲 | ADJUSTMENT 更正后补发
    supersedes_award_id: str | None = None


_EVENT_CLASSES: dict[str, type[DomainEvent]] = {}


def _register() -> None:
    import inspect
    import sys

    module = sys.modules[__name__]
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, DomainEvent) and obj is not DomainEvent:
            _EVENT_CLASSES[obj.__name__] = obj


_register()


def event_to_dict(event: DomainEvent) -> dict[str, Any]:
    data = asdict(event)
    data["type"] = type(event).__name__
    return data


def event_from_dict(data: dict[str, Any]) -> DomainEvent:
    event_type = data["type"]
    cls = _EVENT_CLASSES.get(event_type)
    if cls is None:
        raise ValueError(f"未知事件类型: {event_type}")
    kwargs = {k: v for k, v in data.items() if k != "type"}
    raw_evidence = kwargs.get("evidence") or []
    kwargs["evidence"] = tuple(
        ref if isinstance(ref, EvidenceRef) else EvidenceRef(**ref) for ref in raw_evidence
    )
    return cls(**kwargs)
