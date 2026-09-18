"""应用门面：把事件存储、管理、采集、申诉与计分服务装在一起。

HTTP 层与测试都只依赖这个类。默认使用内存事件存储；传入路径则把事件
落盘为仅追加 JSONL，重启后自动重放。
"""

from __future__ import annotations

from pathlib import Path

from admin import AdminService
from appeals import AppealsService
from ingest import IngestService
from scoring import ScoringEngine
from store import EventStore
import views
from domain_models import EvidenceRef


class Application:
    def __init__(self, store_path: str | Path | None = None):
        self.store = EventStore(store_path)
        self.admin = AdminService(self.store)
        self.ingest = IngestService(self.store)
        self.appeals = AppealsService(self.store)
        self.scoring = ScoringEngine(self.store)

    # ---- 证据辅助 ------------------------------------------------------
    @staticmethod
    def _evidence(items: list[dict] | None) -> list[EvidenceRef]:
        return [
            EvidenceRef(
                type=item["type"],
                ref=item["ref"],
                digest=item.get("digest"),
                note=item.get("note"),
            )
            for item in (items or [])
        ]

    # ---- 管理 ----------------------------------------------------------
    def create_season(self, payload: dict) -> dict:
        season_id = self.admin.create_season(
            payload["name"], int(payload["year"]), payload.get("season_id")
        )
        return {"season_id": season_id}

    def publish_rules(self, payload: dict) -> dict:
        version_id = self.admin.publish_rules(
            payload["season_id"],
            payload["effective_date"],
            payload.get("rules"),
            version_id=payload.get("version_id"),
            published_at=payload.get("published_at"),
            note=payload.get("note", ""),
        )
        return {"version_id": version_id, "binding_note": "按比赛日自动绑定，晚于比赛日生效的版本不得回溯"}

    def schedule_event(self, payload: dict) -> dict:
        event_id = self.admin.schedule_event(
            payload["season_id"],
            payload["city"],
            payload["date"],
            payload["name"],
            event_id=payload.get("event_id"),
            pinned_rule_version_id=payload.get("pinned_rule_version_id"),
        )
        return {"event_id": event_id, "bound_rule_version_id": self.store.bind_rules_safe(event_id)}

    def register_division(self, payload: dict) -> dict:
        return {"division_id": self.admin.register_division(
            payload["season_id"], payload["name"], payload.get("division_id")
        )}

    def register_athlete(self, payload: dict) -> dict:
        return {"athlete_id": self.admin.register_athlete(
            payload["season_id"], payload["display_name"], payload["legal_name"],
            payload["bib"], payload.get("region", ""), athlete_id=payload.get("athlete_id"),
        )}

    def grant_eligibility(self, payload: dict) -> dict:
        self.admin.grant_eligibility(payload["event_id"], payload["athlete_id"], payload["division_id"])
        return {"ok": True}

    def check_in(self, payload: dict) -> dict:
        self.admin.check_in(payload["event_id"], payload["athlete_id"], payload["checked_in_at"])
        return {"ok": True}

    # ---- 采集 ----------------------------------------------------------
    def record_reading(self, payload: dict) -> dict:
        reading_id = self.ingest.record_reading(
            payload["event_id"], payload["athlete_id"], payload["station_id"],
            payload["device_id"], payload["device_class"],
            float(payload["value_s"]), payload["captured_at"],
        )
        return self._reading_status(payload["event_id"], payload["athlete_id"], payload["station_id"], reading_id)

    def _reading_status(self, event_id, athlete_id, station_id, reading_id) -> dict:
        reading = self.store.state.reading_by_id[reading_id]
        response = {"reading_id": reading_id, "status": reading["status"]}
        if reading["status"] == "CONFLICT":
            response["group_id"] = reading["group_id"]
            response["note"] = "读数冲突，原值全部保留，等待裁决"
        return response

    def list_open_conflicts(self, event_id: str | None = None) -> dict:
        groups = [
            {
                "group_id": g["group_id"],
                "event_id": g["event_id"],
                "athlete_id": g["athlete_id"],
                "station_id": g["station_id"],
                "reading_ids": g["reading_ids"],
                "values_s": [self.store.state.reading_by_id[r]["value_s"] for r in g["reading_ids"]],
            }
            for g in self.store.state.conflicts.values()
            if g["status"] == "OPEN" and (event_id is None or g["event_id"] == event_id)
        ]
        return {"conflicts": groups}

    def adjudicate(self, payload: dict) -> dict:
        self.ingest.adjudicate_mark(
            payload["group_id"],
            chosen_reading_id=payload.get("chosen_reading_id"),
            value_s=payload.get("value_s"),
            issued_by=payload.get("issued_by", "official"),
            evidence=self._evidence(payload.get("evidence")),
        )
        return {"ok": True}

    def record_call(self, payload: dict) -> dict:
        call_id = self.ingest.record_judge_call(
            payload["event_id"], payload["athlete_id"], payload["station_id"],
            payload["call_type"], payload["judge_id"],
            penalty_s=float(payload.get("penalty_s", 0.0)),
            reason=payload.get("reason", ""),
            evidence=self._evidence(payload.get("evidence")),
        )
        return {"call_id": call_id}

    def grant_exemption(self, payload: dict) -> dict:
        exemption_id = self.ingest.grant_medical_exemption(
            payload["event_id"], payload["athlete_id"], payload["reason_code"],
            payload["valid_until"], issued_by=payload.get("issued_by", "medical"),
            evidence=self._evidence(payload.get("evidence")),
        )
        return {"exemption_id": exemption_id}

    def record_withdrawal(self, payload: dict) -> dict:
        self.ingest.record_withdrawal(
            payload["event_id"], payload["athlete_id"], payload["reason"],
            on_course=bool(payload.get("on_course", False)),
        )
        return {"ok": True}

    def approve_makeup(self, payload: dict) -> dict:
        self.ingest.approve_makeup(
            payload["event_id"], payload["athlete_id"], payload["reason"], payload["approved_by"]
        )
        return {"ok": True}

    def record_makeup_result(self, payload: dict) -> dict:
        makeup_id = self.ingest.record_makeup_result(
            payload["for_event_id"], payload["athlete_id"], payload["station_id"],
            float(payload["value_s"]), payload["location"], payload["captured_at"],
            evidence=self._evidence(payload.get("evidence")),
        )
        return {"makeup_id": makeup_id, "scored_against_event": payload["for_event_id"]}

    # ---- 申诉 / 更正 ----------------------------------------------------
    def file_appeal(self, payload: dict) -> dict:
        appeal_id = self.appeals.file_appeal(
            payload["event_id"], payload["athlete_id"], payload["contested"],
            payload["summary"], issued_by=payload.get("issued_by", "official"),
            evidence=self._evidence(payload.get("evidence")),
        )
        return {"appeal_id": appeal_id, "frozen": True}

    def rule_appeal(self, payload: dict) -> dict:
        self.appeals.rule_appeal(
            payload["appeal_id"], payload["decision"],
            rescinded_call_ids=payload.get("rescinded_call_ids"),
            mark_overrides=payload.get("mark_overrides"),
            note=payload.get("note", ""),
            evidence=self._evidence(payload.get("evidence")),
        )
        return {"ok": True}

    def apply_correction(self, payload: dict) -> dict:
        correction_id = self.appeals.apply_correction(
            payload["event_id"], payload["reason"], payload["trigger_refs"],
            self._evidence(payload.get("evidence")),
            division_id=payload.get("division_id"),
        )
        return {"correction_id": correction_id}

    # ---- 榜单 ----------------------------------------------------------
    def event_standings(self, event_id: str, division_id: str, internal: bool) -> dict:
        snapshot = self.scoring.compute_event_standings(event_id, division_id)
        if internal:
            return views.internal_event_view(snapshot, self.store)
        return views.public_event_view(snapshot, self.store)

    def publish_event_standings(self, event_id: str, division_id: str) -> dict:
        return self.scoring.publish_event_standings(event_id, division_id)

    def season_standings(self, season_id: str, division_id: str, internal: bool) -> dict:
        snapshot = self.scoring.compute_season_standings(season_id, division_id)
        if internal:
            return snapshot  # 内部直接看完整结构（含每站规则版本与挂起明细）
        return views.public_season_view(snapshot, self.store)

    def publish_season_standings(self, season_id: str, division_id: str) -> dict:
        return self.scoring.publish_season_standings(season_id, division_id)

    def event_diff(self, event_id: str) -> dict:
        history = self.store.state.snapshots.get(("event", event_id), [])
        if len(history) < 2:
            return {"event_id": event_id, "changes": [], "note": "正式快照不足两版，暂无差异"}
        previous, current = history[-2], history[-1]
        prev_snap, cur_snap = previous["snapshot"], current["snapshot"]
        return {
            "event_id": event_id,
            "from_snapshot": prev_snap["snapshot_id"],
            "to_snapshot": cur_snap["snapshot_id"],
            "changes": views.diff_event_snapshots(
                prev_snap, cur_snap, self.store,
                prev_seq=previous["event_seq"], cur_seq=current["event_seq"],
            ),
        }

    def award_ledger(self, scope: str | None = None, owner_id: str | None = None) -> dict:
        awards = [
            a for a in self.store.state.awards
            if (scope is None or a["scope"] == scope)
            and (owner_id is None or a["owner_id"] == owner_id)
        ]
        balances: dict[tuple[str, str], float] = {}
        for a in awards:
            balances[(a["scope"], a["athlete_id"])] = (
                balances.get((a["scope"], a["athlete_id"]), 0.0) + a["amount"]
            )
        return {
            "entries": awards,
            "net_payable": [
                {"scope": s, "athlete_id": aid, "amount": round(amount, 2)}
                for (s, aid), amount in sorted(balances.items())
            ],
        }

    def audit_trail(self) -> dict:
        return {"events": views.internal_audit_trail(self.store)}
