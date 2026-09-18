"""仅追加事件存储与状态投影。

事件是唯一事实来源；投影只是事件流的派生物，任何时候都可以整条重放。
更正从不修改旧事件，而是追加新事件，因此排名变化总能追到来源事件。
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from domain_models import DomainEvent, event_from_dict, event_to_dict
from rulebook import RuleRegistry, ScoringRules


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Projection:
    """事件流的内存投影。重建它只需重放事件。"""

    def __init__(self) -> None:
        self.seasons: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.divisions: dict[str, dict] = {}
        self.athletes: dict[str, dict] = {}
        self.entries: dict[tuple[str, str], dict] = {}
        self.readings: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        self.reading_by_id: dict[str, dict] = {}
        self.calls: dict[str, dict] = {}
        self.exemptions: dict[tuple[str, str], dict] = {}
        self.withdrawals: dict[tuple[str, str], dict] = {}
        self.makeup_approvals: dict[tuple[str, str], dict] = {}
        self.makeup_results: dict[tuple[str, str, str], dict] = {}
        self.conflicts: dict[str, dict] = {}
        self.adjudications: dict[tuple[str, str, str], dict] = {}
        self.appeals: dict[str, dict] = {}
        self.appeal_overrides: dict[tuple[str, str, str], dict] = {}
        self.corrections: list[dict] = []
        self.snapshots: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.awards: list[dict] = []

    def athletes_for_event(self, event_id: str) -> list[str]:
        return [athlete_id for (eid, athlete_id) in self.entries if eid == event_id]

    def division_of(self, event_id: str, athlete_id: str) -> str | None:
        entry = self.entries.get((event_id, athlete_id))
        return entry["division_id"] if entry else None

    def open_appeals_for(self, event_id: str, division_id: str | None = None) -> list[dict]:
        result = []
        for appeal in self.appeals.values():
            if appeal["event_id"] != event_id or appeal["status"] != "OPEN":
                continue
            if division_id is not None and self.division_of(event_id, appeal["athlete_id"]) != division_id:
                continue
            result.append(appeal)
        return result


class EventStore:
    """线程安全的仅追加日志，可选择落盘为 JSONL 便于重放与审计。"""

    def __init__(self, path: str | Path | None = None, clock: Callable[[], str] = utcnow_iso):
        self._events: list[DomainEvent] = []
        self._lock = threading.RLock()
        self._clock = clock
        self.path = Path(path) if path else None
        self._projection = Projection()
        self.rules = RuleRegistry()
        self._replaying = False
        if self.path and self.path.exists():
            self._replay_file()

    # ---- 写入 ----------------------------------------------------------
    def emit(self, event: DomainEvent) -> DomainEvent:
        with self._lock:
            values = dict(event.__dict__)
            if not values.get("occurred_at"):
                values["occurred_at"] = self._clock()
            values["seq"] = len(self._events) + 1
            stored = type(event)(**values)
            self._events.append(stored)
            self._apply(stored)
            if self.path and not self._replaying:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event_to_dict(stored), ensure_ascii=False) + "\n")
            return stored

    def _replay_file(self) -> None:
        self._replaying = True
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.emit(event_from_dict(json.loads(line)))
        finally:
            self._replaying = False

    # ---- 读取 ----------------------------------------------------------
    @property
    def events(self) -> list[DomainEvent]:
        with self._lock:
            return list(self._events)

    @property
    def state(self) -> Projection:
        return self._projection

    def bind_rules(self, event_id: str) -> ScoringRules:
        info = self.state.events[event_id]
        return self.rules.bind(info["season_id"], info["date"], info.get("pinned_rule_version_id"))

    def bind_rules_safe(self, event_id: str) -> str | None:
        """返回绑定版本号；尚无已生效版本时返回 None（不抛错）。"""
        try:
            return self.bind_rules(event_id).version_id
        except (LookupError, KeyError):
            return None

    # ---- 投影分发 ------------------------------------------------------
    def _apply(self, event: DomainEvent) -> None:
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler:
            handler(event)

    @staticmethod
    def _evidence_refs(event: DomainEvent) -> list[str]:
        return [ref.ref for ref in event.evidence]

    def _on_RuleVersionPublished(self, e) -> None:
        self.rules.publish(
            ScoringRules.from_payload(
                e.version_id,
                e.season_id,
                e.effective_date,
                e.published_at or e.occurred_at[:10],
                e.rules,
                e.note,
            )
        )

    def _on_SeasonCreated(self, e) -> None:
        self.state.seasons[e.season_id] = {"season_id": e.season_id, "name": e.name, "year": e.year}

    def _on_EventScheduled(self, e) -> None:
        self.state.events[e.event_id] = {
            "event_id": e.event_id,
            "season_id": e.season_id,
            "city": e.city,
            "date": e.date,
            "name": e.name,
            "pinned_rule_version_id": e.pinned_rule_version_id,
        }

    def _on_DivisionRegistered(self, e) -> None:
        self.state.divisions[e.division_id] = {
            "division_id": e.division_id,
            "season_id": e.season_id,
            "name": e.name,
        }

    def _on_AthleteRegistered(self, e) -> None:
        self.state.athletes[e.athlete_id] = {
            "athlete_id": e.athlete_id,
            "season_id": e.season_id,
            "display_name": e.display_name,
            "legal_name": e.legal_name,
            "bib": e.bib,
            "region": e.region,
        }

    def _on_EligibilityGranted(self, e) -> None:
        self.state.entries[(e.event_id, e.athlete_id)] = {
            "event_id": e.event_id,
            "athlete_id": e.athlete_id,
            "division_id": e.division_id,
            "checked_in": False,
        }

    def _on_CheckInRecorded(self, e) -> None:
        entry = self.state.entries.get((e.event_id, e.athlete_id))
        if entry:
            entry["checked_in"] = True
            entry["checked_in_at"] = e.checked_in_at

    def _on_DeviceReadingRecorded(self, e) -> None:
        reading = {
            "reading_id": e.reading_id,
            "device_id": e.device_id,
            "device_class": e.device_class,
            "value_s": e.value_s,
            "captured_at": e.captured_at,
            "status": "RAW",  # RAW | DEDUPED | CONFLICT | ADJUDICATED | DEAD
            "group_id": None,
        }
        self.state.readings[(e.event_id, e.athlete_id, e.station_id)].append(reading)
        self.state.reading_by_id[e.reading_id] = reading

    def _on_JudgeCallRecorded(self, e) -> None:
        self.state.calls[e.call_id] = {
            "call_id": e.call_id,
            "event_id": e.event_id,
            "athlete_id": e.athlete_id,
            "station_id": e.station_id,
            "call_type": e.call_type,
            "penalty_s": e.penalty_s,
            "reason": e.reason,
            "judge_id": e.judge_id,
            "status": "STANDING",  # STANDING | RESCINDED
            "evidence": self._evidence_refs(e),
        }

    def _on_MedicalExemptionGranted(self, e) -> None:
        self.state.exemptions[(e.event_id, e.athlete_id)] = {
            "exemption_id": e.exemption_id,
            "reason_code": e.reason_code,
            "valid_until": e.valid_until,
            "evidence": self._evidence_refs(e),
        }

    def _on_WithdrawalRecorded(self, e) -> None:
        self.state.withdrawals[(e.event_id, e.athlete_id)] = {
            "reason": e.reason,
            "on_course": e.on_course,
        }

    def _on_MakeupApproved(self, e) -> None:
        self.state.makeup_approvals[(e.event_id, e.athlete_id)] = {
            "reason": e.reason,
            "approved_by": e.approved_by,
        }

    def _on_MakeupResultRecorded(self, e) -> None:
        self.state.makeup_results[(e.for_event_id, e.athlete_id, e.station_id)] = {
            "makeup_id": e.makeup_id,
            "value_s": e.value_s,
            "location": e.location,
            "captured_at": e.captured_at,
        }

    def _on_ReadingsDeduplicated(self, e) -> None:
        kept = self.state.reading_by_id.get(e.kept_reading_id)
        if kept:
            kept["status"] = "ADJUDICATED" if kept["status"] == "CONFLICT" else "KEPT"
        for reading_id in e.dropped_reading_ids:
            reading = self.state.reading_by_id.get(reading_id)
            if reading and reading["status"] == "RAW":
                reading["status"] = "DEDUPED"

    def _on_ConflictFlagged(self, e) -> None:
        self.state.conflicts[e.group_id] = {
            "group_id": e.group_id,
            "event_id": e.event_id,
            "athlete_id": e.athlete_id,
            "station_id": e.station_id,
            "reading_ids": list(e.reading_ids),
            "status": "OPEN",  # OPEN | RESOLVED
        }
        for reading_id in e.reading_ids:
            reading = self.state.reading_by_id.get(reading_id)
            if reading:
                reading["status"] = "CONFLICT"
                reading["group_id"] = e.group_id

    def _on_MarkAdjudicated(self, e) -> None:
        group = self.state.conflicts.get(e.group_id)
        if group:
            group["status"] = "RESOLVED"
            group["resolution_event_seq"] = e.seq
            for reading_id in group["reading_ids"]:
                reading = self.state.reading_by_id.get(reading_id)
                if reading:
                    reading["status"] = "ADJUDICATED" if reading_id == e.chosen_reading_id else "DEAD"
        self.state.adjudications[(e.event_id, e.athlete_id, e.station_id)] = {
            "group_id": e.group_id,
            "chosen_reading_id": e.chosen_reading_id,
            "value_s": e.value_s,
            "event_seq": e.seq,
            "evidence": self._evidence_refs(e),
        }

    def _on_AppealFiled(self, e) -> None:
        self.state.appeals[e.appeal_id] = {
            "appeal_id": e.appeal_id,
            "event_id": e.event_id,
            "athlete_id": e.athlete_id,
            "contested": list(e.contested),
            "summary": e.summary,
            "status": "OPEN",
            "filed_event_seq": e.seq,
        }

    def _on_AppealRuled(self, e) -> None:
        appeal = self.state.appeals.get(e.appeal_id)
        if appeal:
            appeal["status"] = e.decision  # UPHELD | SUSTAINED
            appeal["ruled_event_seq"] = e.seq
        for call_id in e.rescinded_call_ids:
            call = self.state.calls.get(call_id)
            if call:
                call["status"] = "RESCINDED"
                call["rescinded_by_appeal"] = e.appeal_id
        for override in e.mark_overrides:
            self.state.appeal_overrides[(e.event_id, e.athlete_id, override["station_id"])] = {
                "appeal_id": e.appeal_id,
                "value_s": override["value_s"],
                "event_seq": e.seq,
            }

    def _on_CorrectionApplied(self, e) -> None:
        self.state.corrections.append(
            {
                "correction_id": e.correction_id,
                "event_id": e.event_id,
                "reason": e.reason,
                "trigger_refs": list(e.trigger_refs),
                "evidence": self._evidence_refs(e),
                "event_seq": e.seq,
            }
        )

    def _on_StandingsPublished(self, e) -> None:
        history = self.state.snapshots[(e.scope, e.owner_id)]
        history.append(
            {
                "snapshot_id": f"{e.scope}:{e.owner_id}#{len(history) + 1}",
                "event_seq": e.seq,
                "published_at": e.occurred_at,
                "snapshot": e.snapshot,
            }
        )

    def _on_AwardPosted(self, e) -> None:
        self.state.awards.append(
            {
                "award_id": e.award_id,
                "scope": e.scope,
                "owner_id": e.owner_id,
                "snapshot_id": e.snapshot_id,
                "athlete_id": e.athlete_id,
                "place": e.place,
                "amount": e.amount,
                "currency": e.currency,
                "kind": e.kind,
                "status": e.status,  # POSTED | REVERSAL | ADJUSTMENT
                "supersedes_award_id": e.supersedes_award_id,
                "event_seq": e.seq,
            }
        )
