"""公开 / 内部两套视图。

公开榜单只保留名次、成绩、积分、奖金与晋级结果，隐藏医疗信息、证件/姓名等
身份细节，以及设备证据、裁判与裁决链路。内部视图保留全部证据编号、规则
版本与修订号，支持从排名变化追到补赛、裁决和发布版本。
"""

from __future__ import annotations

from typing import Any

# 公开榜单允许出现的选手字段（其余一律剔除）
_PUBLIC_COMPETITOR_FIELDS = ("competitor_id", "division_id", "status", "rank",
                             "value", "penalty_seconds", "points", "prize",
                             "qualification")
_PUBLIC_STATUS = {
    "FINISHED": "FINISHED",
    "DNF": "DNF",
    "DNS": "DNS",
    "EXEMPT": "EXEMPT",
    "PENDING": "PENDING",
}


def _masked(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row[k] for k in _PUBLIC_COMPETITOR_FIELDS if k in row}
    out["status"] = _PUBLIC_STATUS.get(row.get("status"), row.get("status"))
    return out


def public_station(result: dict[str, Any]) -> dict[str, Any]:
    """公开的分站榜：无姓名、无医疗原因、无证据链、无修订号。"""
    return {
        "station_id": result["station_id"],
        "name": result["name"],
        "city": result["city"],
        "occurs_at": result["occurs_at"],
        "rule_version": result["rule_version"],
        "frozen": result["frozen"],
        "divisions": {
            division_id: [_masked(row) for row in rows]
            for division_id, rows in result["divisions"].items()
        },
    }


def public_season(standings: dict[str, Any]) -> dict[str, Any]:
    """公开的跨站总榜：只给总分、名次、晋级与每站积分，不给豁免原因。"""
    return {
        "season_id": standings["season_id"],
        "as_of": standings["as_of"],
        "agg_rule_version": standings["agg_rule_version"],
        "stations": standings["stations"],
        "divisions": {
            division_id: [
                {"competitor_id": row["competitor_id"], "rank": row["rank"],
                 "total": row["total"], "races_counted": row["races_counted"],
                 "per_station": row["per_station"], "dropped": row["dropped"],
                 "qualification": row["qualification"]}
                for row in rows
            ]
            for division_id, rows in standings["divisions"].items()
        },
    }


def internal_station(result: dict[str, Any]) -> dict[str, Any]:
    """内部视图：公开字段之外保留姓名、原始成绩、修订号与全部证据。"""
    enriched = json_safe(result)
    for rows in enriched["divisions"].values():
        for row in rows:
            row["trace"] = {
                "revision": row.get("revision", 0),
                "rule_version": row.get("rule_version"),
                "made_up": any(r.get("made_up") for r in row["evidence"]["readings"]),
                "evidence_event_ids": sorted(
                    {r["event_id"] for r in row["evidence"]["readings"]}
                    | {p["event_id"] for p in row["evidence"]["penalties"]}
                    | ({row["evidence"]["exemption"]["event_id"]}
                       if row["evidence"].get("exemption") else set())
                    | ({row["evidence"]["makeup"]["event_id"]}
                       if row["evidence"].get("makeup") else set())),
            }
    return enriched


def internal_season(standings: dict[str, Any]) -> dict[str, Any]:
    """内部跨站视图：附每站适用的规则版本，便于按站复现。"""
    return json_safe(standings)


def json_safe(value: Any) -> Any:
    """把投影结果转换为可 JSON 序列化的纯结构（已是纯数据时为浅拷贝）。"""
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(json_safe(v) for v in value)
    return value
