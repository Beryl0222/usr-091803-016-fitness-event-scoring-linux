"""成绩与资格采集服务。

负责把外部世界的输入登记为领域事件：资格、签到、多设备计时、
裁判判罚、医疗豁免、伤退、异地补赛。设备读数写入后立即按比赛日
绑定的规则版本做去重：能自动判定的合并为一条，无法判定的保留全部
原值并挂起为冲突，等待人工裁决——系统永远不静默选值。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from domain_models import (
    ConflictFlagged,
    DeviceReadingRecorded,
    EvidenceRef,
    JudgeCallRecorded,
    MakeupApproved,
    MakeupResultRecorded,
    MarkAdjudicated,
    MedicalExemptionGranted,
    ReadingsDeduplicated,
    WithdrawalRecorded,
)
from store import EventStore


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class IngestService:
    def __init__(self, store: EventStore):
        self.store = store

    # ---- 设备计时：写入即去重 ------------------------------------------
    def record_reading(
        self,
        event_id: str,
        athlete_id: str,
        station_id: str,
        device_id: str,
        device_class: str,
        value_s: float,
        captured_at: str,
        issued_by: str = "device",
    ) -> str:
        reading_id = _new_id("rdg")
        self.store.emit(
            DeviceReadingRecorded(
                issued_by=issued_by,
                role="device",
                reading_id=reading_id,
                event_id=event_id,
                athlete_id=athlete_id,
                station_id=station_id,
                device_id=device_id,
                device_class=device_class,
                value_s=float(value_s),
                captured_at=captured_at,
            )
        )
        self._resolve_readings(event_id, athlete_id, station_id)
        return reading_id

    def _resolve_readings(self, event_id: str, athlete_id: str, station_id: str) -> None:
        """按绑定规则版本对同一动作的未决读数做聚类、去重或挂冲突。"""
        rules = self.store.bind_rules(event_id)
        # 跑步由多个分段门计时，多条读数是常态（计分端累加），不参与重复聚类
        if station_id == "running":
            return
        key = (event_id, athlete_id, station_id)
        raw = [r for r in self.store.state.readings[key] if r["status"] == "RAW"]
        if len(raw) < 2:
            return

        # 按采集时间单链聚类：相邻读数间隔 <= dedupe_window_s 视为同一动作的多次采集
        ordered = sorted(raw, key=lambda r: _parse_ts(r["captured_at"]))
        clusters: list[list[dict]] = [[ordered[0]]]
        for reading in ordered[1:]:
            gap = (
                _parse_ts(reading["captured_at"])
                - _parse_ts(clusters[-1][-1]["captured_at"])
            ).total_seconds()
            if gap <= rules.dedupe_window_s:
                clusters[-1].append(reading)
            else:
                clusters.append([reading])

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            values = [r["value_s"] for r in cluster]
            if max(values) - min(values) <= rules.conflict_tolerance_s:
                self._dedupe(event_id, athlete_id, station_id, cluster, rules)
            else:
                self._flag_conflict(event_id, athlete_id, station_id, cluster)

    def _dedupe(self, event_id, athlete_id, station_id, cluster, rules) -> None:
        priority = {name: i for i, name in enumerate(rules.device_priority)}
        kept = min(
            cluster,
            key=lambda r: (priority.get(r["device_class"], len(priority)), r["captured_at"]),
        )
        dropped = [r["reading_id"] for r in cluster if r["reading_id"] != kept["reading_id"]]
        self.store.emit(
            ReadingsDeduplicated(
                issued_by="system",
                dedupe_id=_new_id("ddp"),
                event_id=event_id,
                athlete_id=athlete_id,
                station_id=station_id,
                kept_reading_id=kept["reading_id"],
                dropped_reading_ids=tuple(dropped),
            )
        )

    def _flag_conflict(self, event_id, athlete_id, station_id, cluster) -> None:
        # 已在开放冲突组中的读数不重复挂起
        existing = {
            rid
            for group in self.store.state.conflicts.values()
            if group["status"] == "OPEN"
            for rid in group["reading_ids"]
        }
        reading_ids = tuple(r["reading_id"] for r in cluster if r["reading_id"] not in existing)
        if len(reading_ids) < 2:
            return
        self.store.emit(
            ConflictFlagged(
                issued_by="system",
                group_id=_new_id("cfl"),
                event_id=event_id,
                athlete_id=athlete_id,
                station_id=station_id,
                reading_ids=reading_ids,
            )
        )

    def adjudicate_mark(
        self,
        group_id: str,
        *,
        chosen_reading_id: str | None = None,
        value_s: float | None = None,
        issued_by: str = "official",
        evidence: list[EvidenceRef] | None = None,
    ) -> None:
        """人工裁决冲突。必须引用证据；可选取某条设备读数或直接认定数值。"""
        group = self.store.state.conflicts.get(group_id)
        if group is None:
            raise KeyError(f"冲突组不存在: {group_id}")
        if group["status"] != "OPEN":
            raise ValueError(f"冲突组已裁决: {group_id}")
        if not evidence:
            raise ValueError("裁决必须引用原始证据")
        if chosen_reading_id is None and value_s is None:
            raise ValueError("必须选定一条读数或给出认定值")
        if chosen_reading_id is not None and chosen_reading_id not in group["reading_ids"]:
            raise ValueError("选定读数不属于该冲突组")
        if value_s is None:
            value_s = self.store.state.reading_by_id[chosen_reading_id]["value_s"]
        self.store.emit(
            MarkAdjudicated(
                issued_by=issued_by,
                role="official",
                evidence=tuple(evidence),
                group_id=group_id,
                event_id=group["event_id"],
                athlete_id=group["athlete_id"],
                station_id=group["station_id"],
                chosen_reading_id=chosen_reading_id,
                value_s=float(value_s),
            )
        )

    # ---- 裁判判罚 ------------------------------------------------------
    def record_judge_call(
        self,
        event_id: str,
        athlete_id: str,
        station_id: str,
        call_type: str,
        judge_id: str,
        *,
        penalty_s: float = 0.0,
        reason: str = "",
        evidence: list[EvidenceRef] | None = None,
    ) -> str:
        call_id = _new_id("cal")
        self.store.emit(
            JudgeCallRecorded(
                issued_by=judge_id,
                role="judge",
                evidence=tuple(evidence or []),
                call_id=call_id,
                event_id=event_id,
                athlete_id=athlete_id,
                station_id=station_id,
                call_type=call_type,
                penalty_s=float(penalty_s),
                reason=reason,
                judge_id=judge_id,
            )
        )
        return call_id

    # ---- 医疗豁免 / 伤退 / 补赛 ----------------------------------------
    def grant_medical_exemption(
        self,
        event_id: str,
        athlete_id: str,
        reason_code: str,
        valid_until: str,
        *,
        issued_by: str = "medical",
        evidence: list[EvidenceRef] | None = None,
    ) -> str:
        if not evidence:
            raise ValueError("医疗豁免必须附医疗证明证据")
        exemption_id = _new_id("med")
        self.store.emit(
            MedicalExemptionGranted(
                issued_by=issued_by,
                role="medical",
                evidence=tuple(evidence),
                exemption_id=exemption_id,
                event_id=event_id,
                athlete_id=athlete_id,
                reason_code=reason_code,
                valid_until=valid_until,
            )
        )
        return exemption_id

    def record_withdrawal(
        self, event_id: str, athlete_id: str, reason: str, *, on_course: bool = False
    ) -> None:
        self.store.emit(
            WithdrawalRecorded(
                issued_by="official",
                role="official",
                event_id=event_id,
                athlete_id=athlete_id,
                reason=reason,
                on_course=on_course,
            )
        )

    def approve_makeup(
        self, event_id: str, athlete_id: str, reason: str, approved_by: str
    ) -> None:
        self.store.emit(
            MakeupApproved(
                issued_by=approved_by,
                role="official",
                event_id=event_id,
                athlete_id=athlete_id,
                reason=reason,
                approved_by=approved_by,
            )
        )

    def record_makeup_result(
        self,
        for_event_id: str,
        athlete_id: str,
        station_id: str,
        value_s: float,
        location: str,
        captured_at: str,
        *,
        evidence: list[EvidenceRef] | None = None,
    ) -> str:
        if (for_event_id, athlete_id) not in self.store.state.makeup_approvals:
            raise ValueError("补赛成绩必须先有补赛批准")
        makeup_id = _new_id("mkp")
        self.store.emit(
            MakeupResultRecorded(
                issued_by="official",
                role="official",
                evidence=tuple(evidence or []),
                makeup_id=makeup_id,
                for_event_id=for_event_id,
                athlete_id=athlete_id,
                station_id=station_id,
                value_s=float(value_s),
                location=location,
                captured_at=captured_at,
            )
        )
        return makeup_id
