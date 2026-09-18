"""申诉与官方更正。

- 申诉一提出，相关站（同组）名次立即冻结，直到裁决；
- 裁决只能引用原始证据；成立时撤销判罚或按证据认定新成绩；
- 更正（非申诉渠道）同样强制证据，且只能在名次未冻结时发布；
- 冻结期间计算仍可预览，但正式快照与奖金分录被计分引擎拒绝。
"""

from __future__ import annotations

import uuid

from domain_models import (
    AppealFiled,
    AppealRuled,
    CorrectionApplied,
    EvidenceRef,
)
from store import EventStore

SUSTAINED = "SUSTAINED"  # 申诉成立
UPHELD = "UPHELD"  # 维持原判


class AppealError(ValueError):
    pass


class AppealsService:
    def __init__(self, store: EventStore):
        self.store = store

    def file_appeal(
        self,
        event_id: str,
        athlete_id: str,
        contested: list[str],
        summary: str,
        *,
        issued_by: str = "official",
        evidence: list[EvidenceRef] | None = None,
    ) -> str:
        if (event_id, athlete_id) not in self.store.state.entries:
            raise AppealError("选手未报名该站，无法提出申诉")
        if not contested:
            raise AppealError("申诉必须指明争议对象（判罚号或分项）")
        appeal_id = f"apl_{uuid.uuid4().hex[:12]}"
        self.store.emit(
            AppealFiled(
                issued_by=issued_by,
                role="official",
                evidence=tuple(evidence or []),
                appeal_id=appeal_id,
                event_id=event_id,
                athlete_id=athlete_id,
                contested=tuple(contested),
                summary=summary,
            )
        )
        return appeal_id

    def rule_appeal(
        self,
        appeal_id: str,
        decision: str,
        *,
        rescinded_call_ids: list[str] | None = None,
        mark_overrides: list[dict] | None = None,
        note: str = "",
        evidence: list[EvidenceRef] | None = None,
    ) -> None:
        appeal = self.store.state.appeals.get(appeal_id)
        if appeal is None:
            raise AppealError(f"申诉不存在: {appeal_id}")
        if appeal["status"] not in ("OPEN",):
            raise AppealError(f"申诉已裁决: {appeal_id}")
        if decision not in (SUSTAINED, UPHELD):
            raise AppealError("裁决结果必须是 SUSTAINED 或 UPHELD")
        if decision == SUSTAINED:
            if not evidence:
                raise AppealError("申诉成立的裁决必须引用原始证据")
            if not rescinded_call_ids and not mark_overrides:
                raise AppealError("申诉成立必须给出撤销判罚或成绩认定")
            for call_id in rescinded_call_ids or []:
                call = self.store.state.calls.get(call_id)
                if call is None or call["event_id"] != appeal["event_id"]:
                    raise AppealError(f"判罚不存在或不属于该站: {call_id}")
            for override in mark_overrides or []:
                if set(override) != {"station_id", "value_s"}:
                    raise AppealError("成绩认定需包含 station_id 与 value_s")
        self.store.emit(
            AppealRuled(
                issued_by="jury",
                role="jury",
                evidence=tuple(evidence or []),
                appeal_id=appeal_id,
                event_id=appeal["event_id"],
                athlete_id=appeal["athlete_id"],
                decision=decision,
                rescinded_call_ids=tuple(rescinded_call_ids or []),
                mark_overrides=tuple(mark_overrides or []),
                note=note,
            )
        )

    def apply_correction(
        self,
        event_id: str,
        reason: str,
        trigger_refs: list[str],
        evidence: list[EvidenceRef],
        *,
        division_id: str | None = None,
    ) -> str:
        """非申诉更正入口。证据强制；相关名次冻结时拒绝。"""
        if not evidence:
            raise AppealError("官方更正必须引用原始证据")
        if not trigger_refs:
            raise AppealError("更正必须引用触发来源（如冲突裁决号、补赛编号）")
        if self.store.state.open_appeals_for(event_id, division_id):
            raise AppealError("相关名次处于申诉冻结期，更正须等待申诉裁决")
        correction_id = f"cor_{uuid.uuid4().hex[:12]}"
        self.store.emit(
            CorrectionApplied(
                issued_by="official",
                role="official",
                evidence=tuple(evidence),
                correction_id=correction_id,
                event_id=event_id,
                reason=reason,
                trigger_refs=tuple(trigger_refs),
            )
        )
        return correction_id
