"""公开与内部两套视图。

公开榜单：
- 只显示代号/显示名与号码布，隐藏法定姓名等身份细节；
- 医疗细节（原因、证明、有效期）隐藏，仅显示"豁免/保底分"这一事实；
- 不暴露设备编号、证据引用、判罚编号与申诉细节。

内部视图：
- 完整身份、每个分项的来源（设备/裁决/补赛/申诉认定）与证据链；
- 名次版本对比，名次变化可追到补赛、裁决、申诉或更正事件；
- 奖金账本（含红冲/补发）与规则版本绑定情况。
"""

from __future__ import annotations

from rulebook import ABS_MED
from store import EventStore

# 公开榜单允许看到的状态（医疗原因不外显）
_PUBLIC_STATUS = {
    "FINISH": "FINISH",
    "DNF": "DNF",
    "MED": "EXEMPT",
    "DNS": "DNS",
    "DQ": "DQ",
    "PENDING_CONFLICT": "UNDER_REVIEW",
    "PENDING_MAKEUP": "MAKEUP_PENDING",
}


def _athlete_public(store: EventStore, athlete_id: str) -> dict:
    info = store.state.athletes.get(athlete_id, {})
    return {
        "athlete_id": athlete_id,
        "display_name": info.get("display_name", athlete_id),
        "bib": info.get("bib", ""),
        "region": info.get("region", ""),
    }


def _public_row(store: EventStore, row: dict) -> dict:
    out = {
        **_athlete_public(store, row["athlete_id"]),
        "status": _PUBLIC_STATUS.get(row["status"], row["status"]),
        "place": row["place"],
        "points": row["points"],
        "total_time_s": row["total_time_s"],
        "component_times_s": row.get("component_times_s", {}),
        "component_places": row.get("component_places", {}),
    }
    if row["status"].startswith("PENDING"):
        # 对外只说成绩核定中，不暴露冲突设备/站点细节
        out["note"] = "成绩核定中"
    return out


def public_event_view(snapshot: dict, store: EventStore) -> dict:
    rows = [_public_row(store, r) for r in snapshot["rows"]]
    rows += [_public_row(store, r) for r in snapshot["pending"]]
    return {
        "view": "public",
        "event_id": snapshot["event_id"],
        "division_id": snapshot["division_id"],
        "rule_version_id": snapshot["rule_version_id"],
        "generated_at": snapshot["generated_at"],
        "frozen": snapshot["frozen"],
        "rows": rows,
        "under_review": len(snapshot["pending"]),
    }


def public_season_view(snapshot: dict, store: EventStore) -> dict:
    def row(row: dict) -> dict:
        return {
            **_athlete_public(store, row["athlete_id"]),
            "status": "RANKED" if row["status"] == "RANKED" else "UNDER_REVIEW",
            "place": row["place"],
            "season_points": row["season_points"],
            "starts": row["starts"],
            "qualified": row["qualified"],
            "above_cut": row["above_cut"],
            "qualify_places": snapshot["qualify_places"],
        }

    return {
        "view": "public",
        "scope": "season",
        "season_id": snapshot["season_id"],
        "division_id": snapshot["division_id"],
        "rule_version_id": snapshot["rule_version_id"],
        "generated_at": snapshot["generated_at"],
        "frozen": snapshot["frozen"],
        "rows": [row(r) for r in snapshot["rows"]],
        "under_review": len(snapshot["pending"]),
    }


# ---- 内部视图 -------------------------------------------------------------

def _athlete_full(store: EventStore, athlete_id: str) -> dict:
    info = store.state.athletes.get(athlete_id, {})
    return {
        "athlete_id": athlete_id,
        "display_name": info.get("display_name"),
        "legal_name": info.get("legal_name"),  # 身份细节：仅内部
        "bib": info.get("bib"),
        "region": info.get("region"),
    }


def _internal_row(store: EventStore, row: dict) -> dict:
    athlete_id = row["athlete_id"]
    out = {
        **_athlete_full(store, athlete_id),
        **row,
    }
    if row["status"] == ABS_MED:
        exemption = store.state.exemptions.get((row.get("event_id"), athlete_id))
        if exemption:
            out["medical"] = {
                "reason_code": exemption["reason_code"],
                "valid_until": exemption["valid_until"],
                "evidence": exemption["evidence"],
            }
    return out


def internal_event_view(snapshot: dict, store: EventStore) -> dict:
    event_id = snapshot["event_id"]
    return {
        "view": "internal",
        "event_id": event_id,
        "event": store.state.events.get(event_id),
        "division_id": snapshot["division_id"],
        "rule_version_id": snapshot["rule_version_id"],
        "generated_at": snapshot["generated_at"],
        "frozen": snapshot["frozen"],
        "open_appeals": snapshot["open_appeals"],
        "rows": [_internal_row(store, {**r, "event_id": event_id}) for r in snapshot["rows"]],
        "pending": [_internal_row(store, {**r, "event_id": event_id}) for r in snapshot["pending"]],
        "open_conflicts": [
            {
                "group_id": g["group_id"],
                "athlete_id": g["athlete_id"],
                "station_id": g["station_id"],
                "reading_ids": g["reading_ids"],
                "values_s": [
                    store.state.reading_by_id[r]["value_s"] for r in g["reading_ids"]
                ],
                "devices": [
                    store.state.reading_by_id[r]["device_class"] for r in g["reading_ids"]
                ],
            }
            for g in store.state.conflicts.values()
            if g["event_id"] == event_id and g["status"] == "OPEN"
        ],
        "snapshot_history": [
            {"snapshot_id": s["snapshot_id"], "published_at": s["published_at"], "event_seq": s["event_seq"]}
            for s in store.state.snapshots.get(("event", event_id), [])
        ],
        "awards_ledger": [a for a in store.state.awards if a["scope"] == "event" and a["owner_id"] == event_id],
    }


def diff_event_snapshots(
    previous: dict | None,
    current: dict,
    store: EventStore,
    prev_seq: int = 0,
    cur_seq: int | None = None,
) -> list[dict]:
    """对比两版分站快照，给出名次变化及可追溯原因。

    原因来自两个版本之间发生的领域事件（补赛、裁决、申诉、更正等），
    每条都带事件编号，可继续跳到审计轨迹核实原始证据。
    """
    old = {r["athlete_id"]: r for r in (previous or {}).get("rows", [])}
    new = {r["athlete_id"]: r for r in current["rows"]}
    event_id = current["event_id"]
    changes = []
    for aid in sorted(set(old) | set(new)):
        before, after = old.get(aid), new.get(aid)
        before_place = before["place"] if before else None
        after_place = after["place"] if after else None
        before_points = before["points"] if before else 0
        after_points = after["points"] if after else 0
        if (
            before_place == after_place
            and before_points == after_points
            and (before or {}).get("status") == (after or {}).get("status")
            and (before or {}).get("penalty_s") == (after or {}).get("penalty_s")
        ):
            continue
        changes.append(
            {
                "athlete_id": aid,
                "before_place": before_place,
                "after_place": after_place,
                "movement": (before_place - after_place)
                if (before_place and after_place)
                else None,
                "before_points": before_points,
                "after_points": after_points,
                "before_status": before["status"] if before else None,
                "after_status": after["status"] if after else None,
                "reasons": _change_reasons(store, event_id, aid, prev_seq, cur_seq, after),
            }
        )
    return changes


_CAUSE_TYPES = (
    "MakeupResultRecorded",
    "MarkAdjudicated",
    "AppealRuled",
    "CorrectionApplied",
    "JudgeCallRecorded",
    "MedicalExemptionGranted",
    "WithdrawalRecorded",
    "ReadingsDeduplicated",
)


def _change_reasons(
    store: EventStore,
    event_id: str,
    athlete_id: str,
    prev_seq: int,
    cur_seq: int | None,
    row: dict | None,
) -> list[str]:
    reasons: list[str] = []
    upper = cur_seq if cur_seq is not None else 10**12
    target_division = store.state.division_of(event_id, athlete_id)
    # 申诉裁决/更正是组级事件：即使直接对象是别人，同组名次也会连锁移动
    division_wide = {"AppealRuled", "CorrectionApplied"}
    for event in store.events:
        if not (prev_seq < (event.seq or 0) <= upper):
            continue
        name = type(event).__name__
        if name not in _CAUSE_TYPES:
            continue
        if getattr(event, "event_id", None) not in (event_id, None):
            continue
        if hasattr(event, "athlete_id") and event.athlete_id != athlete_id:
            if name not in division_wide:
                continue
            if store.state.division_of(event_id, event.athlete_id) != target_division:
                continue
        label = _cause_label(name, event)
        if label and label not in reasons:
            reasons.append(label)
    if not reasons and row is not None and row.get("status") != "FINISH":
        reasons.append(f"状态为 {row['status']}")
    return reasons


def _cause_label(name: str, event) -> str:
    if name == "MakeupResultRecorded":
        return f"异地补赛成绩插入({event.makeup_id}@{event.location})"
    if name == "MarkAdjudicated":
        return f"设备冲突裁决({event.group_id})"
    if name == "AppealRuled":
        detail = "撤销判罚/认定成绩" if event.decision == "SUSTAINED" else "维持原判"
        return f"申诉裁决({event.appeal_id} {event.decision} {detail})"
    if name == "CorrectionApplied":
        return f"官方更正({event.correction_id})"
    if name == "JudgeCallRecorded":
        return f"裁判判罚({event.call_id} +{event.penalty_s}s)"
    if name == "MedicalExemptionGranted":
        return f"医疗豁免({event.exemption_id})"
    if name == "WithdrawalRecorded":
        return "赛中伤退/退赛登记"
    if name == "ReadingsDeduplicated":
        return f"多设备读数去重({event.dedupe_id})"
    return ""


def internal_audit_trail(store: EventStore) -> list[dict]:
    """精简的全量事件轨迹，供内部从排名变化反查发布与裁决过程。"""
    trail = []
    for event in store.events:
        trail.append(
            {
                "seq": event.seq,
                "type": type(event).__name__,
                "occurred_at": event.occurred_at,
                "issued_by": event.issued_by,
                "role": event.role,
                "evidence": [{"type": r.type, "ref": r.ref} for r in event.evidence],
            }
        )
    return trail
