"""基于事件账本的比赛投影：采集、去重、裁决、冻结、更正与增量重算。

写入路径（Service 的命令方法）负责校验并追加事件；读取路径通过重放账本
重建投影，再按各站发生时生效的规则版本计算成绩。所有更正都是引用原始
证据的新事件，因此任意历史时刻的名次都可以完整复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .events import Event, EventStore, now_utc
from . import rules

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """领域规则冲突（参数语义不合法）。"""


class NotFound(DomainError):
    pass


class FrozenStandingsError(DomainError):
    """申诉冻结期间，不得绕过裁决直接改动相关成绩。"""


# ---------------------------------------------------------------------------
# 投影记录
# ---------------------------------------------------------------------------


@dataclass
class _Reading:
    event_id: str
    device_id: str
    value: float
    read_at: str
    attempt_id: str
    state: str = "active"          # active / duplicate / conflicted / voided / rejected
    superseded_by: str | None = None
    note: str = ""
    made_up: bool = False
    host_station_id: str | None = None


@dataclass
class _Penalty:
    event_id: str
    seconds: float
    reps: float
    reason: str
    judge_id: str
    voided: bool = False


@dataclass
class _Exemption:
    event_id: str
    scope: str                     # station / discipline
    discipline_id: str | None
    reason: str
    medical_ref: str
    granted_at: str


@dataclass
class _Makeup:
    event_id: str
    source_station_id: str
    host_station_id: str
    recorded_at: str


@dataclass
class _Station:
    id: str
    season_id: str
    name: str
    city: str
    occurs_at: str
    pinned_rule_version: str | None
    entries: dict[str, str] = field(default_factory=dict)          # competitor -> division
    checkins: dict[str, str] = field(default_factory=dict)         # competitor -> event id
    readings: dict[tuple[str, str], list[_Reading]] = field(default_factory=dict)
    penalties: dict[tuple[str, str], list[_Penalty]] = field(default_factory=dict)
    exemptions: dict[str, _Exemption] = field(default_factory=dict)
    makeups: dict[str, _Makeup] = field(default_factory=dict)
    frozen_appeals: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class CompetitionService:
    def __init__(self, store: EventStore | None = None, rule_registry: rules.RuleRegistry | None = None,
                 clock: Callable[[], str] = now_utc):
        self.store = store or EventStore(clock=clock)
        self.registry = rule_registry or rules.RuleRegistry()
        self._clock = clock
        self._snapshots: dict[str, dict[str, Any]] = {}   # appeal event id -> 冻结快照
        self._restore()

    def _restore(self) -> None:
        """从只追加账本重建规则注册表与申诉快照（服务重启后状态可复现）。"""
        for event in self.store.all():
            if event.type == "rules_published":
                rule_set = rules.RuleSet.from_dict(event.payload["rule_set"])
                if rule_set.version not in {r.version for r in self.registry.all()}:
                    self.registry.publish(rule_set)
            elif event.type == "appeal_snapshot":
                self._snapshots[event.payload["appeal_id"]] = event.payload["snapshot"]

    # ======================================================================
    # 规则版本
    # ======================================================================

    def publish_rules(self, data: dict[str, Any]) -> dict[str, Any]:
        rule_set = rules.RuleSet.from_dict(data)
        if rule_set.version in {r.version for r in self.registry.all()}:
            raise DomainError(f"规则版本已存在且不可变: {rule_set.version}")
        event = self.store.append("rules_published", {"rule_set": rule_set.to_dict()},
                                  occurred_at=rule_set.published_at, actor="rules-committee")
        self.registry.publish(rule_set)
        return {"event_id": event.id, "rule_version": rule_set.version}

    def list_rule_versions(self) -> list[dict[str, Any]]:
        return self.registry.to_list()

    # ======================================================================
    # 赛季与站点
    # ======================================================================

    def create_season(self, season_id: str, name: str) -> dict[str, Any]:
        state = self._project()
        if season_id in state["seasons"]:
            raise DomainError(f"赛季已存在: {season_id}")
        event = self.store.append("season_created", {"season_id": season_id, "name": name})
        return {"event_id": event.id, "season_id": season_id}

    def schedule_station(self, station_id: str, season_id: str, name: str, city: str,
                         occurs_at: str, rule_version: str | None = None) -> dict[str, Any]:
        state = self._project()
        if season_id not in state["seasons"]:
            raise NotFound(f"赛季不存在: {season_id}")
        if station_id in state["stations"]:
            raise DomainError(f"站点已存在: {station_id}")
        if rule_version is not None:
            rule_set = self.registry.get(rule_version)
            if rules.parse_ts(rule_set.effective_from) > rules.parse_ts(occurs_at):
                raise DomainError(
                    f"规则 {rule_version} 生效晚于比赛时间，不能用于该站")
        elif not self._candidate_version(occurs_at):
            raise DomainError(f"{occurs_at} 没有可用的生效规则版本，请先发布或显式指定")
        event = self.store.append(
            "station_scheduled",
            {"station_id": station_id, "season_id": season_id, "name": name, "city": city,
             "occurs_at": occurs_at, "rule_version": rule_version},
            occurred_at=occurs_at, actor="organizer")
        return {"event_id": event.id, "station_id": station_id}

    def _candidate_version(self, occurs_at: str) -> str | None:
        try:
            return self.registry.effective_version(occurs_at).version
        except LookupError:
            return None

    # ======================================================================
    # 选手、报名与签到
    # ======================================================================

    def register_competitor(self, competitor_id: str, name: str, season_id: str | None = None) -> dict[str, Any]:
        state = self._project()
        if competitor_id in state["competitors"]:
            raise DomainError(f"选手已注册: {competitor_id}")
        if season_id is not None and season_id not in state["seasons"]:
            raise NotFound(f"赛季不存在: {season_id}")
        event = self.store.append(
            "competitor_registered",
            {"competitor_id": competitor_id, "name": name, "season_id": season_id},
            actor="registration")
        return {"event_id": event.id, "competitor_id": competitor_id}

    def enter_station(self, station_id: str, competitor_id: str, division_id: str) -> dict[str, Any]:
        station = self._require_station(station_id)
        self._require_competitor(competitor_id)
        rule_set = self._station_rules(station)
        if division_id not in rule_set.division_ids:
            raise DomainError(f"规则 {rule_set.version} 中不存在组别: {division_id}")
        if competitor_id in station.entries:
            raise DomainError("选手已报名该站")
        event = self.store.append(
            "station_entry",
            {"station_id": station_id, "competitor_id": competitor_id,
             "division_id": division_id}, actor="registration")
        return {"event_id": event.id}

    def check_in(self, station_id: str, competitor_id: str, at: str | None = None) -> dict[str, Any]:
        station = self._require_station(station_id)
        self._require_entrant(station, competitor_id)
        if competitor_id in station.checkins:
            raise DomainError("选手已签到")
        event = self.store.append(
            "station_checkin",
            {"station_id": station_id, "competitor_id": competitor_id, "checked_in_at": at or self._clock()},
            occurred_at=at, actor="checkin-desk")
        return {"event_id": event.id}

    # ======================================================================
    # 设备计时：去重与冲突保留
    # ======================================================================

    def record_reading(self, station_id: str, competitor_id: str, discipline_id: str,
                       device_id: str, value: float, read_at: str | None = None,
                       attempt_id: str | None = None) -> dict[str, Any]:
        station = self._require_station(station_id)
        division_id = self._require_entrant(station, competitor_id)
        rule_set = self._station_rules(station)
        discipline = rule_set.discipline(discipline_id)
        when = read_at or self._clock()
        attempt = attempt_id or "primary"
        event = self.store.append(
            "device_reading",
            {"station_id": station_id, "competitor_id": competitor_id,
             "discipline_id": discipline_id, "device_id": device_id,
             "value": float(value), "measure": discipline.measure,
             "read_at": when, "attempt_id": attempt},
            occurred_at=when, actor=f"device:{device_id}")

        status, conflict_event = self._classify_reading(station, competitor_id, discipline_id,
                                                        event.id, float(value), device_id, when, attempt)
        return {"event_id": event.id, "status": status,
                "conflict_with": conflict_event,
                "measure": discipline.measure}

    @staticmethod
    def _classify_reading(station: _Station, cid: str, discipline_id: str, event_id: str,
                          value: float, device_id: str, when: str, attempt: str) -> tuple[str, str | None]:
        """把新读数挂到投影上，与同动作的既有读数比对。

        返回 (duplicate | conflicted | active, 冲突对端事件号)。
        """
        key = (cid, discipline_id)
        readings = station.readings.setdefault(key, [])
        same_attempt = [r for r in readings if r.attempt_id == attempt and r.state != "voided"]
        reading = _Reading(event_id=event_id, device_id=device_id, value=value,
                           read_at=when, attempt_id=attempt)
        readings.append(reading)
        active = [r for r in same_attempt if r.state in ("active", "conflicted")]
        if not active:
            return "active", None
        if any(r.value == value for r in active):
            # 多设备记录同一动作、数值一致：去重，保留先到者为准。
            reading.state = "duplicate"
            reading.superseded_by = active[0].event_id
            return "duplicate", active[0].event_id
        # 数值不一致且无法自动判断：双方都保留原值，等待裁决。
        for r in active:
            if r.state == "active":
                r.state = "conflicted"
        reading.state = "conflicted"
        return "conflicted", active[0].event_id

    # ======================================================================
    # 裁判判罚
    # ======================================================================

    def record_penalty(self, station_id: str, competitor_id: str, discipline_id: str,
                       judge_id: str, reason: str, seconds: float = 0.0,
                       reps: float = 0.0) -> dict[str, Any]:
        station = self._require_station(station_id)
        self._require_entrant(station, competitor_id)
        rule_set = self._station_rules(station)
        rule_set.discipline(discipline_id)
        if seconds < 0 or reps < 0:
            raise DomainError("罚时/罚次不能为负")
        if seconds == 0 and reps == 0:
            raise DomainError("判罚必须包含罚时或罚次")
        event = self.store.append(
            "judge_penalty",
            {"station_id": station_id, "competitor_id": competitor_id,
             "discipline_id": discipline_id, "seconds": float(seconds), "reps": float(reps),
             "reason": reason, "judge_id": judge_id},
            actor=f"judge:{judge_id}")
        return {"event_id": event.id}

    # ======================================================================
    # 医疗豁免与异地补赛
    # ======================================================================

    def grant_medical_exemption(self, station_id: str, competitor_id: str, medical_ref: str,
                                reason: str = "", scope: str = "station",
                                discipline_id: str | None = None, at: str | None = None) -> dict[str, Any]:
        station = self._require_station(station_id)
        self._require_entrant(station, competitor_id)
        if scope not in ("station", "discipline"):
            raise DomainError("豁免范围只能是 station 或 discipline")
        if scope == "discipline":
            if not discipline_id:
                raise DomainError("分项豁免必须指定 discipline_id")
            self._station_rules(station).discipline(discipline_id)
        when = at or self._clock()
        event = self.store.append(
            "medical_exemption",
            {"station_id": station_id, "competitor_id": competitor_id, "granted": True,
             "scope": scope, "discipline_id": discipline_id,
             "reason": reason, "medical_ref": medical_ref,
             "granted_at": when},
            occurred_at=when, actor="medical-officer")
        return {"event_id": event.id}

    def record_makeup(self, source_station_id: str, host_station_id: str, competitor_id: str,
                      discipline_values: dict[str, float], judge_id: str = "makeup",
                      recorded_at: str | None = None) -> dict[str, Any]:
        """登记异地补赛：成绩计入缺席的 source 站，host 仅为举办地证据。"""
        source = self._require_station(source_station_id)
        host = self._require_station(host_station_id)
        division_id = self._require_entrant(source, competitor_id)
        source_rules = self._station_rules(source)
        if not discipline_values:
            raise DomainError("补赛成绩不能为空")
        for discipline_id, value in discipline_values.items():
            source_rules.discipline(discipline_id)
        # 补赛只接受 source 站规则下的全部应赛分项（用于完整复现）。
        when = recorded_at or self._clock()
        event = self.store.append(
            "makeup_result",
            {"source_station_id": source_station_id, "host_station_id": host_station_id,
             "competitor_id": competitor_id, "division_id": division_id,
             "discipline_values": {k: float(v) for k, v in discipline_values.items()},
             "measure_rule_version": source_rules.version, "recorded_at": when},
            occurred_at=when, actor=f"judge:{judge_id}", causation_id=None)
        return {"event_id": event.id, "applies_to_station": source_station_id,
                "host_station_id": host_station_id}

    # ======================================================================
    # 人工裁决（读数冲突）
    # ======================================================================

    def resolve_reading_conflict(self, station_id: str, competitor_id: str, discipline_id: str,
                                 chosen_event_id: str, note: str = "",
                                 appeal_id: str | None = None) -> dict[str, Any]:
        station = self._require_station(station_id)
        self._require_entrant(station, competitor_id)
        if station.frozen_appeals and appeal_id is None:
            raise FrozenStandingsError(
                f"站点 {station_id} 名次已因申诉冻结，裁决须随申诉决定一起作出")
        key = (competitor_id, discipline_id)
        readings = station.readings.get(key, [])
        chosen = next((r for r in readings if r.event_id == chosen_event_id), None)
        if chosen is None:
            raise NotFound("指定的读数证据不存在")
        if chosen.state == "voided":
            raise DomainError("不能选择已作废的读数")
        event = self.store.append(
            "reading_conflict_resolved",
            {"station_id": station_id, "competitor_id": competitor_id,
             "discipline_id": discipline_id, "chosen_event_id": chosen_event_id,
             "choice": {"device_id": chosen.device_id, "value": chosen.value,
                        "read_at": chosen.read_at},
             "note": note, "appeal_id": appeal_id},
            actor="head-judge")
        return {"event_id": event.id}

    # ======================================================================
    # 申诉：冻结名次、留快照
    # ======================================================================

    def open_appeal(self, reason: str, station_id: str | None = None,
                    competitor_id: str | None = None) -> dict[str, Any]:
        scope = "station" if station_id else "season"
        if station_id:
            station = self._require_station(station_id)
            if competitor_id:
                self._require_entrant(station, competitor_id)
        event = self.store.append(
            "appeal_opened",
            {"scope": scope, "station_id": station_id, "competitor_id": competitor_id,
             "reason": reason, "opened_at": self._clock(), "freeze": True},
            actor="appeal-desk")
        # 冻结时的名次快照：此后一切更正都与该快照对比。
        snapshot = {"appeal_event_id": event.id, "scope": scope, "station_id": station_id,
                    "competitor_id": competitor_id, "captured_at": event.recorded_at,
                    "standings": self._frozen_standings(station_id)}
        self._snapshots[event.id] = snapshot
        self.store.append(
            "appeal_snapshot",
            {"appeal_id": event.id, "snapshot": snapshot},
            occurred_at=event.recorded_at, actor="system", causation_id=event.id)
        return {"appeal_id": event.id, "frozen_snapshot": snapshot["standings"]}

    def close_appeal(self, appeal_id: str, decision: str, note: str = "",
                     resolution: dict[str, Any] | None = None,
                     corrections: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """作出裁决并解冻。

        resolution: 读数冲突裁决 {competitor_id, discipline_id, chosen_event_id}
        corrections: 随裁决执行的更正（在解冻前的同一裁决通道内执行）。
        """
        appeal = self.store.get(appeal_id)
        if appeal is None or appeal.type != "appeal_opened":
            raise NotFound("申诉不存在")
        payload = appeal.payload
        station_id = payload.get("station_id")
        if decision not in ("upheld", "amended", "rejected"):
            raise DomainError("决定必须是 upheld / amended / rejected")

        effects: list[dict[str, Any]] = []
        before = self._frozen_standings(station_id) if station_id else None
        if resolution is not None:
            effects.append(self.resolve_reading_conflict(
                station_id, resolution["competitor_id"], resolution["discipline_id"],
                resolution["chosen_event_id"], note=resolution.get("note", ""), appeal_id=appeal_id))
        for correction in corrections or []:
            effects.append(self.apply_correction(reason=f"申诉裁决：{note}", appeal_id=appeal_id, **correction))

        event = self.store.append(
            "appeal_closed",
            {"appeal_id": appeal_id, "decision": decision, "note": note,
             "effects": effects}, actor="jury")
        affected = None
        if before is not None:
            after = self._frozen_standings(station_id)
            affected = self._diff_standings(before, after)
        return {"event_id": event.id, "appeal_id": appeal_id, "decision": decision,
                "effects": effects, "affected": affected}

    def get_appeal_snapshot(self, appeal_id: str) -> dict[str, Any]:
        if appeal_id not in self._snapshots:
            raise NotFound("申诉快照不存在")
        return self._snapshots[appeal_id]

    # ======================================================================
    # 更正：引用原始证据，只重算真正受影响的人
    # ======================================================================

    def apply_correction(self, station_id: str, competitor_id: str, kind: str,
                         target_event_id: str, reason: str, new_value: float | None = None,
                         appeal_id: str | None = None) -> dict[str, Any]:
        station = self._require_station(station_id)
        self._require_entrant(station, competitor_id)
        if station.frozen_appeals and appeal_id is None:
            raise FrozenStandingsError(
                f"站点 {station_id} 名次已冻结，更正须通过申诉裁决通道")
        target = self.store.get(target_event_id)
        if target is None:
            raise NotFound("被更正的原始证据不存在")
        if target.payload.get("station_id") != station_id:
            raise DomainError("证据与站点不匹配")
        if target.payload.get("competitor_id") != competitor_id:
            raise DomainError("证据与选手不匹配")
        if kind not in ("void_reading", "amend_reading", "void_penalty", "amend_penalty"):
            raise DomainError(f"未知更正类型: {kind}")

        before = self.station_results(station_id)
        event = self.store.append(
            "correction_applied",
            {"station_id": station_id, "competitor_id": competitor_id, "kind": kind,
             "target_event_id": target_event_id, "new_value": new_value,
             "reason": reason, "appeal_id": appeal_id,
             "original_payload": target.payload},
            actor="jury" if appeal_id else "official",
            causation_id=target_event_id)
        after = self.station_results(station_id)
        affected = self._diff_standings(before, after)
        season_effects = self._season_ripple(station_id)
        return {"event_id": event.id, "causation_id": target_event_id,
                "station_affected": affected, "season_affected": season_effects}

    # ======================================================================
    # 投影
    # ======================================================================

    def _project(self) -> dict[str, Any]:
        seasons: dict[str, dict[str, Any]] = {}
        competitors: dict[str, dict[str, Any]] = {}
        stations: dict[str, _Station] = {}

        for event in self.store.all():
            p = event.payload
            kind = event.type
            if kind == "season_created":
                seasons[p["season_id"]] = {"season_id": p["season_id"], "name": p["name"]}
            elif kind == "competitor_registered":
                competitors[p["competitor_id"]] = {
                    "competitor_id": p["competitor_id"], "name": p["name"]}
            elif kind == "station_scheduled":
                stations[p["station_id"]] = _Station(
                    id=p["station_id"], season_id=p["season_id"], name=p["name"],
                    city=p["city"], occurs_at=p["occurs_at"],
                    pinned_rule_version=p.get("rule_version"))
            elif kind == "station_entry":
                stations[p["station_id"]].entries[p["competitor_id"]] = p["division_id"]
            elif kind == "station_checkin":
                stations[p["station_id"]].checkins[p["competitor_id"]] = event.id
            elif kind == "device_reading":
                self._project_reading(stations[p["station_id"]], event)
            elif kind == "judge_penalty":
                station = stations[p["station_id"]]
                station.penalties.setdefault(
                    (p["competitor_id"], p["discipline_id"]), []).append(
                    _Penalty(event_id=event.id, seconds=p["seconds"], reps=p["reps"],
                             reason=p["reason"], judge_id=p["judge_id"]))
            elif kind == "medical_exemption" and p.get("granted"):
                station = stations[p["station_id"]]
                station.exemptions[p["competitor_id"]] = _Exemption(
                    event_id=event.id, scope=p["scope"], discipline_id=p.get("discipline_id"),
                    reason=p.get("reason", ""), medical_ref=p["medical_ref"],
                    granted_at=p["granted_at"])
            elif kind == "makeup_result":
                station = stations[p["source_station_id"]]
                station.makeups[p["competitor_id"]] = _Makeup(
                    event_id=event.id, source_station_id=p["source_station_id"],
                    host_station_id=p["host_station_id"], recorded_at=p["recorded_at"])
                for discipline_id, value in p["discipline_values"].items():
                    reading = _Reading(event_id=event.id, device_id=f"makeup@{p['host_station_id']}",
                                       value=value, read_at=p["recorded_at"], attempt_id="primary",
                                       made_up=True, host_station_id=p["host_station_id"])
                    station.readings.setdefault((p["competitor_id"], discipline_id), []).append(reading)
            elif kind == "reading_conflict_resolved":
                station = stations[p["station_id"]]
                for r in station.readings.get((p["competitor_id"], p["discipline_id"]), []):
                    if r.event_id == p["chosen_event_id"] and r.state in ("conflicted", "active"):
                        r.state = "active"
                        r.note = p.get("note", "")
                    elif r.state == "conflicted":
                        r.state = "rejected"
                        r.superseded_by = p["chosen_event_id"]
            elif kind == "correction_applied":
                self._project_correction(stations[p["station_id"]], event)
            elif kind == "appeal_opened":
                if p.get("station_id"):
                    stations[p["station_id"]].frozen_appeals.add(event.id)
            elif kind == "appeal_closed":
                appeal_id = p["appeal_id"]
                for station in stations.values():
                    station.frozen_appeals.discard(appeal_id)
        return {"seasons": seasons, "competitors": competitors, "stations": stations}

    @staticmethod
    def _project_reading(station: _Station, event: Event) -> None:
        p = event.payload
        key = (p["competitor_id"], p["discipline_id"])
        existing = station.readings.setdefault(key, [])
        attempt = p.get("attempt_id", "primary")
        same = [r for r in existing if r.attempt_id == attempt and r.state != "voided"]
        reading = _Reading(event_id=event.id, device_id=p["device_id"], value=p["value"],
                           read_at=p["read_at"], attempt_id=attempt)
        existing.append(reading)
        active = [r for r in same if r.state in ("active", "conflicted")]
        if not active:
            return
        if any(r.value == p["value"] for r in active):
            reading.state = "duplicate"
            reading.superseded_by = active[0].event_id
        else:
            reading.state = "conflicted"
            for r in active:
                if r.state == "active":
                    r.state = "conflicted"

    @staticmethod
    def _project_correction(station: _Station, event: Event) -> None:
        p = event.payload
        kind, target_id = p["kind"], p["target_event_id"]
        if kind in ("void_reading", "amend_reading"):
            for recs in station.readings.values():
                for r in recs:
                    if r.event_id != target_id:
                        continue
                    if kind == "void_reading":
                        r.state = "voided"
                        r.superseded_by = event.id
                    elif r.state not in ("voided",):
                        r.value = float(p["new_value"])
                        r.note = (r.note + " / 已更正").strip(" /")
                        r.superseded_by = event.id
        else:
            for recs in station.penalties.values():
                for pen in recs:
                    if pen.event_id == target_id:
                        # 改判：原条目作废，随后追加替换条目，避免新旧双重计分。
                        pen.voided = True
            if kind == "amend_penalty":
                original = event.payload.get("original_payload", {})
                key = (p["competitor_id"], original.get("discipline_id"))
                recs = station.penalties.setdefault(key, [])
                recs.append(_Penalty(
                    event_id=event.id,
                    seconds=float(p["new_value"]) if p.get("new_value") is not None else original.get("seconds", 0.0),
                    reps=original.get("reps", 0.0),
                    reason=f"更正判罚：{p['reason']}", judge_id="jury"))

    # ======================================================================
    # 成绩计算
    # ======================================================================

    def _require_station(self, station_id: str) -> _Station:
        state = self._project()
        if station_id not in state["stations"]:
            raise NotFound(f"站点不存在: {station_id}")
        return state["stations"][station_id]

    def _require_competitor(self, competitor_id: str) -> dict[str, Any]:
        state = self._project()
        if competitor_id not in state["competitors"]:
            raise NotFound(f"选手不存在: {competitor_id}")
        return state["competitors"][competitor_id]

    @staticmethod
    def _require_entrant(station: _Station, competitor_id: str) -> str:
        if competitor_id not in station.entries:
            raise DomainError(f"选手未报名该站: {competitor_id}")
        return station.entries[competitor_id]

    def _station_rules(self, station: _Station) -> rules.RuleSet:
        # 不回写投影：排期时固定版本，否则按发生时生效版本确定性解析。
        version = station.pinned_rule_version or self.registry.effective_version(station.occurs_at).version
        return self.registry.get(version)

    def _selected_reading(self, station: _Station, cid: str, discipline_id: str,
                          measure: str, prefer_makeup: bool = False) -> tuple[_Reading | None, bool]:
        """返回 (选中的读数, 是否存在待裁决冲突)。

        同一 attempt 内多设备同值去重；不同 attempt 取规则意义上的最优成绩；
        存在未裁决冲突且该动作无其他可用读数时返回 (None, True)。
        prefer_makeup 时该分项只采用异地补赛读数，忽略原站零散记录。
        """
        recs = station.readings.get((cid, discipline_id), [])
        if prefer_makeup and any(r.made_up and r.state not in ("voided", "duplicate", "rejected")
                                 for r in recs):
            recs = [r for r in recs if r.made_up]
        groups: dict[str, list[_Reading]] = {}
        for r in recs:
            if r.state in ("voided", "duplicate", "rejected"):
                continue
            groups.setdefault(r.attempt_id, []).append(r)
        chosen: _Reading | None = None
        open_conflict = False
        for attempt_recs in groups.values():
            conflicted = [r for r in attempt_recs if r.state == "conflicted"]
            usable = [r for r in attempt_recs if r.state == "active"]
            if conflicted and not usable:
                open_conflict = True
                continue
            for r in usable:
                if chosen is None:
                    chosen = r
                elif measure == rules.MEASURE_TIME and r.value < chosen.value:
                    chosen = r
                elif measure == rules.MEASURE_REPS and r.value > chosen.value:
                    chosen = r
        return chosen, open_conflict

    def station_results(self, station_id: str) -> dict[str, Any]:
        """计算一站全部组别的成绩、名次、积分、奖金与证据。"""
        state = self._project()
        station = state["stations"][station_id]
        rule_set = self._station_rules(station)

        divisions_out: dict[str, list[dict[str, Any]]] = {}
        revisions = self._revision_counters(station_id, self.store.all())
        for division_id in sorted(rule_set.division_ids):
            cids = [cid for cid, div in station.entries.items() if div == division_id]
            # 第一遍：取每名选手的分项构成与证据。
            parts = {cid: self._competitor_parts(station, cid, rule_set) for cid in sorted(cids)}
            # 豁免分项用同组其他选手该分项成绩的中位数替代，避免 0 秒优势。
            replacements = self._exemption_replacements(parts, rule_set)
            outcomes: list[rules.Outcome] = []
            evidence: dict[str, dict[str, Any]] = {}
            for cid in sorted(cids):
                outcome, ev = self._build_outcome(cid, division_id, rule_set,
                                                  parts[cid], replacements)
                outcomes.append(outcome)
                evidence[cid] = ev
            rankings = rules.rank_outcomes(outcomes)
            points = rules.award_station_points(rankings, rule_set)
            rows: list[dict[str, Any]] = []
            for row in rankings:
                cid = row["competitor_id"]
                point_row = points[cid]
                revision = revisions.get(cid, 0)
                prize = rules.prize_for_rank(row["rank"], division_id, rule_set)
                qual = rules.qualification_status(
                    division_id, row["rank"], row["value"],
                    total_points=None, rule_set=rule_set)
                rows.append({
                    "competitor_id": cid,
                    "name": state["competitors"][cid]["name"],
                    "division_id": division_id,
                    "status": row["status"],
                    "rank": row["rank"],
                    "value": row["value"],
                    "raw_value": row["raw_value"],
                    "penalty_seconds": row["penalty_seconds"],
                    "points": point_row["points"],
                    "prize": prize,
                    "qualification": qual,
                    "revision": revision,
                    "rule_version": rule_set.version,
                    "evidence": evidence[cid],
                })
            divisions_out[division_id] = rows
        return {"station_id": station_id, "city": station.city, "name": station.name,
                "occurs_at": station.occurs_at, "frozen": bool(station.frozen_appeals),
                "frozen_appeal_ids": sorted(station.frozen_appeals),
                "rule_version": rule_set.version,
                "divisions": divisions_out}

    def _competitor_parts(self, station: _Station, cid: str,
                          rule_set: rules.RuleSet) -> dict[str, Any]:
        """汇总一名选手在一站的分项构成（不含跨人比较，便于重放）。"""
        exemption = station.exemptions.get(cid)
        makeup = station.makeups.get(cid)
        parts: dict[str, Any] = {
            "station_exempt": bool(exemption and exemption.scope == "station"),
            "exemption": None,
            "makeup": None,
            "checked_in": cid in station.checkins,
            "disciplines": {},
            "evidence": {"readings": [], "penalties": [], "exemption": None,
                         "makeup": None, "rule_version": rule_set.version},
        }
        if exemption:
            parts["exemption"] = {
                "event_id": exemption.event_id, "scope": exemption.scope,
                "discipline_id": exemption.discipline_id, "medical_ref": exemption.medical_ref,
                "reason": exemption.reason, "granted_at": exemption.granted_at}
            parts["evidence"]["exemption"] = parts["exemption"]
        if makeup:
            parts["makeup"] = {"event_id": makeup.event_id,
                               "host_station_id": makeup.host_station_id,
                               "source_station_id": makeup.source_station_id,
                               "recorded_at": makeup.recorded_at}
            parts["evidence"]["makeup"] = parts["makeup"]

        for discipline in rule_set.disciplines:
            # 有异地补赛时，该分项以补赛成绩为准（免原站签到）。
            selected, conflict = self._selected_reading(
                station, cid, discipline.id, discipline.measure,
                prefer_makeup=makeup is not None)
            for r in station.readings.get((cid, discipline.id), []):
                if r.state not in ("duplicate", "voided", "rejected"):
                    parts["evidence"]["readings"].append(
                        {"event_id": r.event_id, "discipline_id": discipline.id,
                         "device_id": r.device_id, "value": r.value, "state": r.state,
                         "attempt_id": r.attempt_id, "made_up": r.made_up,
                         "host_station_id": r.host_station_id})
            active_penalties = [p for p in station.penalties.get((cid, discipline.id), [])
                                if not p.voided]
            for pen in active_penalties:
                parts["evidence"]["penalties"].append(
                    {"event_id": pen.event_id, "discipline_id": discipline.id,
                     "seconds": pen.seconds, "reps": pen.reps, "reason": pen.reason,
                     "judge_id": pen.judge_id})
            disc_exempt = bool(exemption and exemption.scope == "discipline"
                               and exemption.discipline_id == discipline.id)
            parts["disciplines"][discipline.id] = {
                "state": ("exempt" if disc_exempt else
                          "pending" if (selected is None and conflict) else
                          "ok" if selected is not None else "missing"),
                "raw": selected.value if selected else None,
                "penalty_seconds": sum(p.seconds for p in active_penalties),
                "penalty_reps": sum(p.reps for p in active_penalties),
                "made_up": bool(selected and selected.made_up),
            }
        return parts

    @staticmethod
    def _exemption_replacements(all_parts: dict[str, dict[str, Any]],
                                rule_set: rules.RuleSet) -> dict[str, float]:
        """分项豁免的替代成绩：同组其他选手该分项有效成绩（含罚）的中位数。

        time 取中位数（位次中性）；reps 同样取中位数。没有可参照样本时
        退化为 0，该分项不计入完成度。
        """
        replacements: dict[str, float] = {}
        per_discipline: dict[str, list[float]] = {d.id: [] for d in rule_set.disciplines}
        for part in all_parts.values():
            for discipline in rule_set.disciplines:
                cell = part["disciplines"][discipline.id]
                if cell["state"] != "ok":
                    continue
                value = (cell["raw"] + cell["penalty_seconds"]
                         if discipline.measure == rules.MEASURE_TIME
                         else max(0.0, cell["raw"] - cell["penalty_reps"]))
                per_discipline[discipline.id].append(value)
        for discipline_id, values in per_discipline.items():
            if not values:
                continue
            ordered = sorted(values)
            mid = len(ordered) // 2
            if len(ordered) % 2:
                replacements[discipline_id] = round(ordered[mid], 3)
            else:
                replacements[discipline_id] = round((ordered[mid - 1] + ordered[mid]) / 2, 3)
        return replacements

    def _build_outcome(self, cid: str, division_id: str, rule_set: rules.RuleSet,
                       part: dict[str, Any], replacements: dict[str, float]
                       ) -> tuple[rules.Outcome, dict[str, Any]]:
        evidence = part["evidence"]
        if part["station_exempt"]:
            return rules.Outcome(cid, division_id, rules.STATUS_EXEMPT), evidence

        total = 0.0
        raw_total = 0.0
        penalty_seconds = 0.0
        completed = 0
        started = False
        pending = False
        for discipline in rule_set.disciplines:
            cell = part["disciplines"][discipline.id]
            penalty_seconds += cell["penalty_seconds"]
            if cell["state"] == "exempt":
                replacement = replacements.get(discipline.id)
                if replacement is not None:
                    total += replacement
                    raw_total += replacement
                    completed += 1
                continue
            if cell["state"] == "pending":
                pending = True
                continue
            if cell["state"] == "missing":
                continue
            started = True
            if discipline.measure == rules.MEASURE_TIME:
                raw_total += cell["raw"]
                total += cell["raw"] + cell["penalty_seconds"]
            else:
                raw_total += cell["raw"]
                total += max(0.0, cell["raw"] - cell["penalty_reps"])
            completed += 1

        # 异地补赛免原站签到；否则未签到且无任何成绩记为缺席。
        if not part["checked_in"] and not part["makeup"] and not started:
            status = rules.STATUS_DNS
        elif pending:
            status = rules.STATUS_PENDING
        elif completed == len(rule_set.disciplines):
            status = rules.STATUS_FINISHED
        elif completed == 0 and not started:
            status = rules.STATUS_DNS
        else:
            status = rules.STATUS_DNF
        outcome = rules.Outcome(
            cid, division_id, status,
            value=round(total, 3) if status == rules.STATUS_FINISHED else None,
            raw_value=round(raw_total, 3) if raw_total else None,
            penalty_seconds=round(penalty_seconds, 3),
            completed_disciplines=completed, discipline_count=len(rule_set.disciplines))
        return outcome, evidence

    @staticmethod
    def _revision_counters(station_id: str, events: list[Event]) -> dict[str, int]:
        """每名选手成绩经补赛/裁决/更正而改变的次数（由事件流确定性重放）。"""
        counters: dict[str, int] = {}
        for event in events:
            p = event.payload
            if event.type in ("correction_applied", "reading_conflict_resolved"):
                if p.get("station_id") == station_id:
                    cid = p.get("competitor_id")
                    counters[cid] = counters.get(cid, 0) + 1
            elif event.type == "makeup_result" and p.get("source_station_id") == station_id:
                cid = p.get("competitor_id")
                counters[cid] = counters.get(cid, 0) + 1
        return counters

    # ======================================================================
    # 跨站积分与晋级
    # ======================================================================

    def season_standings(self, season_id: str, as_of: str | None = None) -> dict[str, Any]:
        """跨站总榜。各站积分按该站规则版本发放，汇总策略按 as_of 生效版本。"""
        state = self._project()
        if season_id not in state["seasons"]:
            raise NotFound(f"赛季不存在: {season_id}")
        stations = [s for s in state["stations"].values() if s.season_id == season_id]
        if not stations:
            return {"season_id": season_id, "divisions": {}}
        moments = sorted(s.occurs_at for s in stations)
        agg_moment = as_of or moments[-1]
        agg_rules = self.registry.effective_version(agg_moment)

        per_division: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        station_versions: dict[str, str] = {}
        for station in stations:
            result = self.station_results(station.id)
            station_versions[station.id] = result["rule_version"]
            for division_id, rows in result["divisions"].items():
                bucket = per_division.setdefault(division_id, {})
                bucket[station.id] = {
                    r["competitor_id"]: {
                        "points": r["points"] if r["points"] is not None else None,
                        "status": r["status"], "rank": r["rank"], "value": r["value"],
                        "exempt_replaced": r["status"] == rules.STATUS_EXEMPT
                        and self.registry.get(result["rule_version"]).exempt_policy == rules.EXEMPT_AVERAGE}
                    for r in rows}
        out_divisions: dict[str, list[dict[str, Any]]] = {}
        for division_id, by_station in per_division.items():
            totals = rules.cross_station_totals(by_station, agg_rules)
            ranked = rules.rank_totals(totals)
            rows = []
            for row in ranked:
                cid = row["competitor_id"]
                qual = rules.qualification_status(
                    division_id, station_rank=None, station_value=None,
                    total_points=row["total"], rule_set=agg_rules)
                rows.append({"competitor_id": cid,
                             "name": state["competitors"][cid]["name"],
                             "rank": row["rank"], "total": row["total"],
                             "races_counted": row["races_counted"],
                             "per_station": totals[cid]["stations"],
                             "dropped": totals[cid]["dropped"],
                             "qualification": qual,
                             "agg_rule_version": agg_rules.version})
            out_divisions[division_id] = rows
        return {"season_id": season_id, "as_of": agg_moment,
                "agg_rule_version": agg_rules.version,
                "station_rule_versions": station_versions,
                "stations": [{"station_id": s.id, "name": s.name, "city": s.city,
                              "occurs_at": s.occurs_at} for s in stations],
                "divisions": out_divisions}

    # ======================================================================
    # 冻结快照与差异
    # ======================================================================

    def _frozen_standings(self, station_id: str | None) -> Any:
        if station_id is None:
            return None
        result = self.station_results(station_id)
        return {"station_id": station_id, "rule_version": result["rule_version"],
                "divisions": {
                    division_id: [{"competitor_id": r["competitor_id"], "rank": r["rank"],
                                   "status": r["status"], "value": r["value"],
                                   "points": r["points"], "revision": r["revision"]}
                                  for r in rows]
                    for division_id, rows in result["divisions"].items()}}

    @staticmethod
    def _diff_standings(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
        """对比两次名次，只返回真正发生变化的选手。"""
        old = {}
        for div_rows in before.get("divisions", {}).values():
            for r in div_rows:
                old[r["competitor_id"]] = r
        new = {}
        for div_rows in after.get("divisions", {}).values():
            for r in div_rows:
                new[r["competitor_id"]] = r
        affected = []
        for cid in sorted(set(old) | set(new)):
            a, b = old.get(cid), new.get(cid)
            if a is None or b is None:
                affected.append({"competitor_id": cid, "before": a, "after": b,
                                 "change": "added" if a is None else "removed"})
                continue
            fields = ("rank", "status", "value", "points")
            changes = {f: {"before": a.get(f), "after": b.get(f)}
                       for f in fields if a.get(f) != b.get(f)}
            if changes:
                affected.append({"competitor_id": cid, "before": a, "after": b,
                                 "change": "updated", "fields": changes})
        return affected

    def _season_ripple(self, station_id: str) -> list[dict[str, Any]]:
        """给出该站所在赛季总榜受影响的选手（修订后总积分/名次变化）。

        调用点已在更正事件之后，故这里返回新的跨站行供调用方留痕；
        与冻结快照的精确 diff 由申诉通道完成。
        """
        state = self._project()
        station = state["stations"][station_id]
        standings = self.season_standings(station.season_id)
        ripple = []
        for rows in standings["divisions"].values():
            for r in rows:
                if station_id in r["per_station"]:
                    ripple.append({"competitor_id": r["competitor_id"],
                                   "season_rank": r["rank"], "total": r["total"],
                                   "station_points": r["per_station"][station_id]})
        return ripple

    # ======================================================================
    # 审计：事件链
    # ======================================================================

    def evidence_chain(self, event_id: str) -> list[dict[str, Any]]:
        """沿 causation_id 追溯一个裁决/更正到原始证据。"""
        chain = []
        current = self.store.get(event_id)
        seen = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            chain.append(current.to_dict())
            if not current.causation_id:
                break
            current = self.store.get(current.causation_id)
        return chain

    def history(self, station_id: str, competitor_id: str) -> dict[str, Any]:
        """一名选手在一站相关的全部事件（内部追溯用）。"""
        events = []
        for event in self.store.all():
            p = event.payload
            if p.get("station_id") == station_id and p.get("competitor_id") == competitor_id:
                events.append(event.to_dict())
            elif event.type == "correction_applied" and p.get("station_id") == station_id \
                    and p.get("competitor_id") == competitor_id:
                events.append(event.to_dict())
        return {"station_id": station_id, "competitor_id": competitor_id, "events": events}
