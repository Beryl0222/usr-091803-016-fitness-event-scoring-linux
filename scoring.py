"""计分引擎。

所有计算都是纯函数式推导：输入是事件存储的当前投影和某站绑定的规则
版本，输出可直接发布为不可变快照。因为事件不可变、规则版本按比赛日
绑定，任何历史时刻的分站/赛季名次都可以按当时规则复现。

关键语义：
- 缺席绝不直接算零分：DNF / MED 给保底分，DNS / DQ 才是零分；
  已批准但未完成的补赛挂起为 PENDING，不进榜也不记零。
- 申诉开放期间，相关组别名次冻结，拒绝发布新正式快照。
- 更正后重发快照时，只有名次/奖金真正变化的选手产生红冲与补发。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from domain_models import AwardPosted, StandingsPublished
from rulebook import ABS_DNF, ABS_DNS, ABS_DQ, ABS_MED, ScoringRules
from store import EventStore, utcnow_iso

# 参赛状态
ST_FINISH = "FINISH"
ST_PENDING_CONFLICT = "PENDING_CONFLICT"
ST_PENDING_MAKEUP = "PENDING_MAKEUP"

# 成绩来源
SRC_DEVICE = "DEVICE"
SRC_ADJUDICATION = "ADJUDICATION"
SRC_MAKEUP = "MAKEUP"
SRC_APPEAL = "APPEAL_OVERRIDE"

_RANKED = {ST_FINISH}
_ZERO_POINT = {ABS_DNS, ABS_DQ}


class StandingsNotPublishable(RuntimeError):
    """名次当前不可发布为正式快照。"""


class StandingsFrozen(StandingsNotPublishable):
    """有未裁决申诉，相关名次冻结。"""

    def __init__(self, event_id: str, division_id: str, appeals: list[dict]):
        self.event_id = event_id
        self.division_id = division_id
        self.appeals = appeals
        super().__init__(f"名次冻结中：{event_id}/{division_id} 有 {len(appeals)} 件未决申诉")


class ResultsPending(StandingsNotPublishable):
    """存在挂起成绩（冲突待裁或补赛未到），不得发布。"""

    def __init__(self, owner_id: str, division_id: str, pending: list[dict]):
        self.owner_id = owner_id
        self.division_id = division_id
        self.pending = pending
        super().__init__(f"{owner_id}/{division_id} 有 {len(pending)} 名选手成绩挂起")


@dataclass(frozen=True)
class Mark:
    station_id: str
    value_s: float
    source: str
    source_id: str
    location: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass
class ParticipantResult:
    athlete_id: str
    status: str
    marks: dict[str, Mark] = field(default_factory=dict)
    penalty_s: float = 0.0
    total_time_s: float | None = None
    place: int | None = None
    points: int = 0
    component_times: dict[str, float] = field(default_factory=dict)
    component_places: dict[str, int | None] = field(default_factory=dict)
    pending_reason: str | None = None
    trace: dict[str, object] = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "athlete_id": self.athlete_id,
            "status": self.status,
            "place": self.place,
            "points": self.points,
            "total_time_s": self.total_time_s,
            "penalty_s": self.penalty_s,
            "component_times_s": dict(self.component_times),
            "component_places": dict(self.component_places),
            "pending_reason": self.pending_reason,
            "marks": [
                {
                    "station_id": m.station_id,
                    "value_s": m.value_s,
                    "source": m.source,
                    "source_id": m.source_id,
                    "location": m.location,
                    "evidence": list(m.evidence),
                }
                for m in sorted(self.marks.values(), key=lambda x: x.station_id)
            ],
            "trace": self.trace,
        }


def _competition_rank(values: list[tuple[str, float | None]], ties: str) -> dict[str, int]:
    """对可排名选手给名次。SHARE 使用 1,2,2,4 式并列。"""
    ranked = sorted(((k, v) for k, v in values if v is not None), key=lambda kv: kv[1])
    places: dict[str, int] = {}
    i = 0
    while i < len(ranked):
        j = i + 1
        if ties == "SHARE":
            while j < len(ranked) and ranked[j][1] == ranked[i][1]:
                j += 1
        for k, _ in ranked[i:j]:
            places[k] = i + 1
        i = j
    return places


class ScoringEngine:
    def __init__(self, store: EventStore):
        self.store = store

    # ---- 个人成绩解析 --------------------------------------------------
    def _resolve_marks(
        self, rules: ScoringRules, event_id: str, athlete_id: str
    ) -> tuple[dict[str, Mark], list[str]]:
        """把各来源解析成最终采用的分项成绩；返回 (成绩, 挂起原因)。"""
        marks: dict[str, Mark] = {}
        pending: list[str] = []
        state = self.store.state

        for station in rules.stations + ("running",):
            key = (event_id, athlete_id, station)

            # 1) 申诉认定值最优先
            override = state.appeal_overrides.get(key)
            if override:
                marks[station] = Mark(
                    station, override["value_s"], SRC_APPEAL, override["appeal_id"]
                )
                continue

            # 2) 冲突裁决值
            ruling = state.adjudications.get(key)
            if ruling:
                marks[station] = Mark(
                    station,
                    ruling["value_s"],
                    SRC_ADJUDICATION,
                    ruling["group_id"],
                    evidence=tuple(ruling["evidence"]),
                )
                continue

            # 3) 设备读数（单台 RAW 或去重后 KEPT / ADJUDICATED 可用）
            usable = [
                r
                for r in state.readings.get(key, [])
                if r["status"] in ("RAW", "KEPT", "ADJUDICATED")
            ]
            in_open_conflict = any(
                r["status"] == "CONFLICT"
                or any(
                    g["status"] == "OPEN" and r["reading_id"] in g["reading_ids"]
                    for g in state.conflicts.values()
                )
                for r in state.readings.get(key, [])
            )
            if in_open_conflict:
                pending.append(station)
                continue
            if usable:
                if station == "running":
                    # 多个跑步分段是正常的：累加，不视为重复
                    marks[station] = Mark(
                        station,
                        sum(r["value_s"] for r in usable),
                        SRC_DEVICE,
                        ",".join(r["reading_id"] for r in usable),
                    )
                elif len(usable) > 1:
                    # 非跑步站出现多条无法自动归并的值：不静默选值
                    pending.append(station)
                else:
                    r = usable[0]
                    marks[station] = Mark(station, r["value_s"], SRC_DEVICE, r["reading_id"])
                continue

            # 4) 异地补赛成绩（归属原站）
            makeup = state.makeup_results.get(key)
            if makeup:
                marks[station] = Mark(
                    station,
                    makeup["value_s"],
                    SRC_MAKEUP,
                    makeup["makeup_id"],
                    location=makeup["location"],
                )

        return marks, pending

    def _resolve_participant(
        self, rules: ScoringRules, event_id: str, athlete_id: str
    ) -> ParticipantResult:
        state = self.store.state
        result = ParticipantResult(athlete_id=athlete_id, status=ST_FINISH)
        marks, pending_stations = self._resolve_marks(rules, event_id, athlete_id)
        result.marks = marks

        calls = [
            c
            for c in state.calls.values()
            if c["event_id"] == event_id
            and c["athlete_id"] == athlete_id
            and c["status"] == "STANDING"
        ]
        dq = next((c for c in calls if c["call_type"] == "disqualification"), None)
        penalty = sum(c["penalty_s"] for c in calls if c["call_type"] != "disqualification")
        result.penalty_s = penalty
        if calls:
            result.trace["calls"] = [c["call_id"] for c in calls]

        exemption = state.exemptions.get((event_id, athlete_id))
        withdrawal = state.withdrawals.get((event_id, athlete_id))
        makeup_approved = (event_id, athlete_id) in state.makeup_approvals
        entry = state.entries.get((event_id, athlete_id), {})

        if dq:
            result.status = ABS_DQ
            result.trace["dq_call_id"] = dq["call_id"]
            result.points = rules.dq_points
            return result
        if exemption:
            result.status = ABS_MED
            result.points = rules.med_points
            result.trace["exemption_id"] = exemption["exemption_id"]
            return result
        if pending_stations:
            result.status = ST_PENDING_CONFLICT
            result.pending_reason = f"等待裁决的分项: {','.join(sorted(pending_stations))}"
            result.trace["open_conflict_stations"] = sorted(pending_stations)
            return result
        if withdrawal:
            result.status = ABS_DNF
            result.points = rules.dnf_points
            result.trace["withdrawal_on_course"] = withdrawal["on_course"]
            return result

        missing = sorted(s for s in (*rules.stations, "running") if s not in marks)
        if missing:
            if makeup_approved:
                result.status = ST_PENDING_MAKEUP
                result.pending_reason = f"等待补赛成绩: {','.join(missing)}"
                result.trace["missing_makeup_stations"] = missing
                return result
            if entry.get("checked_in") or marks:
                result.status = ABS_DNF
                result.points = rules.dnf_points
                result.trace["incomplete_stations"] = missing
                return result
            result.status = ABS_DNS
            result.points = rules.dns_points
            return result

        total = sum(m.value_s for m in marks.values()) + penalty
        result.total_time_s = round(total, 3)
        for component, stations in rules.components.items():
            if all(s in marks for s in stations):
                c_penalty = sum(
                    c["penalty_s"]
                    for c in calls
                    if c["station_id"] in stations and c["call_type"] != "disqualification"
                )
                result.component_times[component] = round(
                    sum(marks[s].value_s for s in stations) + c_penalty, 3
                )
        return result

    # ---- 整站整组 ------------------------------------------------------
    def compute_event_standings(self, event_id: str, division_id: str) -> dict:
        rules = self.store.bind_rules(event_id)
        state = self.store.state
        athlete_ids = sorted(
            athlete_id
            for athlete_id in state.athletes_for_event(event_id)
            if state.division_of(event_id, athlete_id) == division_id
        )
        participants = {
            aid: self._resolve_participant(rules, event_id, aid) for aid in athlete_ids
        }

        # 总名次
        times = [
            (aid, p.total_time_s)
            for aid, p in participants.items()
            if p.status in _RANKED
        ]
        places = _competition_rank(times, rules.ties)
        points_table = rules.points_for_place

        def place_points(place: int) -> int:
            if not points_table:
                return 0
            idx = min(place - 1, len(points_table) - 1)
            return points_table[idx]

        for aid, p in participants.items():
            if p.status in _RANKED:
                p.place = places[aid]
                p.points = place_points(p.place)

        # 跑步 / 负重 / 体操分项名次（仅在该分项有完整成绩者之间）
        for component in rules.components:
            comp_values = [
                (aid, p.component_times.get(component))
                for aid, p in participants.items()
                if p.status in _RANKED
            ]
            comp_places = _competition_rank(comp_values, rules.ties)
            for aid, p in participants.items():
                p.component_places[component] = comp_places.get(aid)

        status_order = {ABS_DNF: 2, ABS_MED: 3, ABS_DNS: 4, ABS_DQ: 5}
        order = sorted(
            participants.values(),
            key=lambda p: (
                0 if p.status == ST_FINISH else 1,
                p.place or 0 if p.status == ST_FINISH else status_order.get(p.status, 9),
                p.athlete_id,
            ),
        )
        rows = [p.to_row() for p in order if not p.status.startswith("PENDING")]
        pending_rows = sorted(
            (p for p in participants.values() if p.status.startswith("PENDING")),
            key=lambda p: p.athlete_id,
        )

        open_appeals = state.open_appeals_for(event_id, division_id)
        event = state.events[event_id]
        return {
            "event_id": event_id,
            "season_id": event["season_id"],
            "division_id": division_id,
            "rule_version_id": rules.version_id,
            "rank_basis": rules.rank_basis,
            "generated_at": utcnow_iso(),
            "frozen": bool(open_appeals),
            "open_appeals": [a["appeal_id"] for a in open_appeals],
            "rows": rows,
            "pending": [p.to_row() for p in pending_rows],
        }

    # ---- 正式快照与奖金 ------------------------------------------------
    def publish_event_standings(self, event_id: str, division_id: str) -> dict:
        snapshot = self.compute_event_standings(event_id, division_id)
        if snapshot["frozen"]:
            raise StandingsFrozen(
                event_id, division_id, self.store.state.open_appeals_for(event_id, division_id)
            )
        if snapshot["pending"]:
            raise ResultsPending(event_id, division_id, snapshot["pending"])

        history = self.store.state.snapshots.get(("event", event_id), [])
        previous = history[-1] if history else None
        snapshot_no = len(history) + 1
        snapshot_id = f"event:{event_id}#{snapshot_no}"
        payload = {
            **snapshot,
            "snapshot_id": snapshot_id,
            "supersedes": previous["snapshot_id"] if previous else None,
        }

        emitted = self.store.emit(
            StandingsPublished(
                issued_by="official",
                role="official",
                scope="event",
                owner_id=event_id,
                snapshot=payload,
            )
        )
        payload["published_event_seq"] = emitted.seq
        rules = self.store.rules.get(snapshot["rule_version_id"])
        self._post_purse_awards(
            scope="event",
            owner_id=event_id,
            rules=rules,
            purse=rules.event_purse,
            current_rows=snapshot["rows"],
            previous=previous,
            snapshot_id=snapshot_id,
        )
        return payload

    # ---- 跨站赛季积分 --------------------------------------------------
    def compute_season_standings(self, season_id: str, division_id: str) -> dict:
        """逐站按其比赛日绑定的规则取积分，再汇总（允许丢最差站）。"""
        state = self.store.state
        event_ids = sorted(
            (eid for eid, info in state.events.items() if info["season_id"] == season_id),
            key=lambda eid: (state.events[eid]["date"], eid),
        )
        per_athlete: dict[str, list[dict]] = defaultdict(list)
        frozen_by: list[str] = []
        versions: dict[str, str] = {}

        for event_id in event_ids:
            snap = self.compute_event_standings(event_id, division_id)
            versions[event_id] = snap["rule_version_id"]
            if snap["frozen"]:
                frozen_by.append(event_id)
            rows = {r["athlete_id"]: r for r in snap["rows"]}
            rows.update({r["athlete_id"]: r for r in snap["pending"]})
            for athlete_id in state.athletes_for_event(event_id):
                if state.division_of(event_id, athlete_id) != division_id:
                    continue
                row = rows.get(athlete_id)
                if row is None:
                    continue
                per_athlete[athlete_id].append(
                    {
                        "event_id": event_id,
                        "status": row["status"],
                        "points": row["points"],
                        "place": row["place"],
                        "pending_reason": row["pending_reason"],
                        "rule_version_id": snap["rule_version_id"],
                    }
                )

        season_rules = self._season_rules(season_id, event_ids)
        drops = season_rules.score_drops
        rows_out: list[dict] = []
        pending_out: list[dict] = []
        for athlete_id, legs in per_athlete.items():
            starts = [leg for leg in legs if leg["status"] not in (ABS_DNS,)]
            pending_legs = [leg for leg in legs if leg["status"].startswith("PENDING")]
            scored = sorted((leg["points"] for leg in starts), reverse=True)
            kept = scored[: len(scored) - drops] if drops else scored
            entry = {
                "athlete_id": athlete_id,
                "starts": len(starts),
                "legs": legs,
                "season_points": sum(kept),
                "dropped_points": sum(scored[len(kept):]),
                "qualified": len(starts) >= season_rules.min_events_for_qualification,
            }
            if pending_legs:
                entry["status"] = "PENDING"
                entry["pending_reason"] = (
                    f"等待 {len(pending_legs)} 站成绩确认（冲突/补赛/申诉）"
                )
                pending_out.append(entry)
            else:
                entry["status"] = "RANKED"
                rows_out.append(entry)

        ranked = [e for e in rows_out if e["qualified"]]
        places = _competition_rank(
            [(e["athlete_id"], -float(e["season_points"])) for e in ranked], season_rules.ties
        )
        for entry in rows_out:
            entry["place"] = places.get(entry["athlete_id"])
            entry["above_cut"] = (
                entry["place"] is not None and entry["place"] <= season_rules.qualify_places
            )
        for entry in pending_out:
            entry["place"] = None
            entry["above_cut"] = False

        rows_out.sort(key=lambda e: (e["place"] is None, e["place"] or 0, e["athlete_id"]))
        return {
            "scope": "season",
            "season_id": season_id,
            "division_id": division_id,
            "rule_version_id": season_rules.version_id,
            "event_rule_versions": versions,
            "generated_at": utcnow_iso(),
            "qualify_places": season_rules.qualify_places,
            "min_events_for_qualification": season_rules.min_events_for_qualification,
            "score_drops": drops,
            "frozen": bool(frozen_by),
            "frozen_events": frozen_by,
            "rows": rows_out,
            "pending": sorted(pending_out, key=lambda e: e["athlete_id"]),
        }

    def publish_season_standings(self, season_id: str, division_id: str) -> dict:
        snapshot = self.compute_season_standings(season_id, division_id)
        if snapshot["frozen"]:
            raise StandingsFrozen(season_id, division_id, [
                a
                for event_id in snapshot["frozen_events"]
                for a in self.store.state.open_appeals_for(event_id, division_id)
            ])
        if snapshot["pending"]:
            raise ResultsPending(season_id, division_id, snapshot["pending"])

        history = self.store.state.snapshots.get(("season", season_id), [])
        previous = history[-1] if history else None
        snapshot_id = f"season:{season_id}#{len(history) + 1}"
        payload = {
            **snapshot,
            "snapshot_id": snapshot_id,
            "supersedes": previous["snapshot_id"] if previous else None,
        }
        emitted = self.store.emit(
            StandingsPublished(
                issued_by="official", role="official", scope="season",
                owner_id=season_id, snapshot=payload,
            )
        )
        payload["published_event_seq"] = emitted.seq
        rules = self.store.rules.get(snapshot["rule_version_id"])
        self._post_purse_awards(
            scope="season",
            owner_id=season_id,
            rules=rules,
            purse=rules.season_purse,
            current_rows=snapshot["rows"],
            previous=previous,
            snapshot_id=snapshot_id,
        )
        return payload

    def _season_rules(self, season_id: str, event_ids: list[str]) -> ScoringRules:
        """赛季汇总参数取最后一站绑定的版本（汇总规则同样必须已在该站生效）。"""
        if not event_ids:
            raise LookupError(f"赛季 {season_id} 还没有任何分站")
        return self.store.bind_rules(event_ids[-1])

    # ---- 奖金账本：只动真正受影响的人 ----------------------------------
    def _post_purse_awards(
        self, *, scope, owner_id, rules, purse, current_rows, previous, snapshot_id
    ) -> None:
        def claims(rows: list[dict]) -> dict[str, tuple[int, float]]:
            out: dict[str, tuple[int, float]] = {}
            for row in rows:
                place = row.get("place")
                if row.get("status") in (ST_FINISH, "RANKED") and place and place <= len(purse):
                    out[row["athlete_id"]] = (place, float(purse[place - 1]))
            return out

        prev_rows = previous["snapshot"]["rows"] if previous else []
        old_claims = claims(prev_rows)
        new_claims = claims(current_rows)
        if not previous:
            for aid, (place, amount) in sorted(new_claims.items()):
                self._emit_award(scope, owner_id, snapshot_id, aid, place, amount,
                                 rules.currency, "POSTED", None)
            return

        # 该赛事下每人现存净应付与最近一次正向分录：即使上一版快照因金额
        # 未变而没有产生分录，红冲也必须能追溯到真正发过钱的那一笔。
        ledger = [
            a for a in self.store.state.awards
            if a["scope"] == scope and a["owner_id"] == owner_id
        ]
        net_before: dict[str, float] = defaultdict(float)
        last_positive: dict[str, dict] = {}
        for award in ledger:
            net_before[award["athlete_id"]] += award["amount"]
            if award["status"] in ("POSTED", "ADJUSTMENT"):
                last_positive[award["athlete_id"]] = award

        for aid in sorted(set(new_claims) | set(old_claims)):
            old_amount = round(net_before.get(aid, 0.0), 2)
            new_place, new_amount = new_claims.get(aid, (None, 0.0))
            if old_amount == round(new_amount, 2):
                continue  # 应付金额没变：此人不受影响，不红冲也不补发
            prior = last_positive.get(aid)
            if old_amount:
                self._emit_award(scope, owner_id, snapshot_id, aid,
                                 prior["place"] if prior else (old_claims.get(aid, (None, 0))[0]),
                                 -old_amount, rules.currency, "REVERSAL",
                                 prior["award_id"] if prior else None)
            if new_amount:
                self._emit_award(scope, owner_id, snapshot_id, aid, new_place, new_amount,
                                 rules.currency, "ADJUSTMENT",
                                 prior["award_id"] if prior else None)

    def _emit_award(self, scope, owner_id, snapshot_id, athlete_id, place, amount,
                    currency, status, supersedes) -> None:
        self.store.emit(
            AwardPosted(
                issued_by="official",
                role="official",
                award_id=f"awd_{uuid.uuid4().hex[:12]}",
                scope=scope,
                owner_id=owner_id,
                snapshot_id=snapshot_id,
                athlete_id=athlete_id,
                place=place or 0,
                amount=amount,
                currency=currency,
                kind=scope,
                status=status,
                supersedes_award_id=supersedes,
            )
        )

