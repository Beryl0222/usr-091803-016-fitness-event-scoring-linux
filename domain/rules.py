"""不可变的规则版本与纯函数计分。

赛事方可以预先发布多个规则版本（组别、分项、积分、晋级线、奖金、医疗豁免
政策），每个版本带有生效日期。任意一场比赛都按其发生时已生效的最新版本
复现名次、积分、晋级线与奖金，新版本不会回溯改写旧站结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CATEGORY_RUN = "run"
CATEGORY_WEIGHTED = "weighted"
CATEGORY_GYMNASTICS = "gymnastics"

MEASURE_TIME = "time"          # 数值越小越好（秒）
MEASURE_REPS = "reps"          # 数值越大越好（次数）

STATUS_FINISHED = "FINISHED"
STATUS_DNF = "DNF"             # 伤退未完赛
STATUS_DNS = "DNS"             # 缺席/未出发（无豁免时按零分）
STATUS_EXEMPT = "EXEMPT"       # 经医疗等豁免的缺席
STATUS_PENDING = "PENDING"     # 证据冲突待裁决，暂不排名

# 豁免者本站积分的算法
EXEMPT_ZERO = "zero"                    # 仍记零分
EXEMPT_DNF = "dnf"                      # 按未完赛积分
EXEMPT_LAST_FINISHER = "last_finisher"  # 按本站最后一名完赛者的积分
EXEMPT_AVERAGE = "average"              # 按本人其他已计分站积分的平均值

_RANKED_STATUSES = (STATUS_FINISHED, STATUS_DNF)


def parse_ts(value: str) -> datetime:
    """把日期或 ISO 时间解析为带时区的 datetime，便于比较生效区间。"""
    text = value.strip()
    if len(text) == 10:  # YYYY-MM-DD
        text = text + "T00:00:00+00:00"
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# 不可变规则对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Division:
    id: str
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Division":
        return cls(id=str(data["id"]), name=str(data.get("name", data["id"])))


@dataclass(frozen=True)
class Discipline:
    """分项定义，例如 8km 跑步组、雪橇负重组、立卧操体操组。"""

    id: str
    name: str
    category: str          # run / weighted / gymnastics
    measure: str = MEASURE_TIME

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Discipline":
        category = str(data["category"])
        if category not in (CATEGORY_RUN, CATEGORY_WEIGHTED, CATEGORY_GYMNASTICS):
            raise ValueError(f"未知分项类别: {category}")
        measure = str(data.get("measure", MEASURE_TIME))
        if measure not in (MEASURE_TIME, MEASURE_REPS):
            raise ValueError(f"未知计量方式: {measure}")
        return cls(id=str(data["id"]), name=str(data.get("name", data["id"])),
                   category=category, measure=measure)


@dataclass(frozen=True)
class QualificationRule:
    """晋级线。

    method:
      rank   —— 分站名次达到 threshold（如每站前 3 名）
      points —— 跨站累计积分达到 threshold
      time   —— 完赛成绩达到 threshold（秒，仅 time 计量分项）
    """

    division_id: str
    method: str
    threshold: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QualificationRule":
        method = str(data["method"])
        if method not in ("rank", "points", "time"):
            raise ValueError(f"未知晋级方式: {method}")
        return cls(division_id=str(data["division_id"]), method=method,
                   threshold=float(data["threshold"]))


@dataclass(frozen=True)
class RuleSet:
    """一个不可变的规则版本。"""

    version: str
    name: str
    published_at: str
    effective_from: str
    divisions: tuple[Division, ...]
    disciplines: tuple[Discipline, ...]
    rank_points: tuple[int, ...] = field(default_factory=tuple)
    dnf_points: int = 0
    dns_points: int = 0
    exempt_policy: str = EXEMPT_LAST_FINISHER
    drop_races: int = 0
    qualification: tuple[QualificationRule, ...] = field(default_factory=tuple)
    # division_id -> 按名次发放的奖金（第 1、2、3… 名）
    station_purse: dict[str, tuple[float, ...]] = field(default_factory=dict)
    effective_to: str | None = None
    notes: str = ""

    def __post_init__(self):
        if not self.version:
            raise ValueError("规则版本号不能为空")
        parse_ts(self.published_at)
        parse_ts(self.effective_from)
        if self.effective_to:
            if parse_ts(self.effective_to) <= parse_ts(self.effective_from):
                raise ValueError("生效结束时间必须晚于开始时间")
        if not self.divisions:
            raise ValueError("至少需要一个组别")
        if not self.disciplines:
            raise ValueError("至少需要一个分项")
        if not self.rank_points:
            raise ValueError("至少需要一个名次积分")
        if self.exempt_policy not in (EXEMPT_ZERO, EXEMPT_DNF, EXEMPT_LAST_FINISHER, EXEMPT_AVERAGE):
            raise ValueError(f"未知豁免政策: {self.exempt_policy}")
        if self.drop_races < 0:
            raise ValueError("可丢弃站数不能为负")
        ids = {d.id for d in self.divisions}
        for q in self.qualification:
            if q.division_id not in ids:
                raise ValueError(f"晋级规则引用了未定义组别: {q.division_id}")
        for div_id in self.station_purse:
            if div_id not in ids:
                raise ValueError(f"奖金规则引用了未定义组别: {div_id}")

    @property
    def division_ids(self) -> set[str]:
        return {d.id for d in self.divisions}

    def discipline(self, discipline_id: str) -> Discipline:
        for d in self.disciplines:
            if d.id == discipline_id:
                return d
        raise KeyError(f"未定义分项: {discipline_id}")

    def qualification_for(self, division_id: str) -> QualificationRule | None:
        for q in self.qualification:
            if q.division_id == division_id:
                return q
        return None

    def points_for_rank(self, rank: int) -> int:
        """名次对应积分；超出积分表长度时取表末值。"""
        if rank < 1:
            return 0
        idx = rank - 1
        if idx >= len(self.rank_points):
            return self.rank_points[-1]
        return self.rank_points[idx]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "published_at": self.published_at,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "notes": self.notes,
            "divisions": [{"id": d.id, "name": d.name} for d in self.divisions],
            "disciplines": [{"id": d.id, "name": d.name, "category": d.category,
                             "measure": d.measure} for d in self.disciplines],
            "rank_points": list(self.rank_points),
            "dnf_points": self.dnf_points,
            "dns_points": self.dns_points,
            "exempt_policy": self.exempt_policy,
            "drop_races": self.drop_races,
            "qualification": [{"division_id": q.division_id, "method": q.method,
                               "threshold": q.threshold} for q in self.qualification],
            "station_purse": {k: list(v) for k, v in self.station_purse.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleSet":
        return cls(
            version=str(data["version"]),
            name=str(data.get("name", data["version"])),
            published_at=str(data["published_at"]),
            effective_from=str(data["effective_from"]),
            effective_to=data.get("effective_to"),
            notes=str(data.get("notes", "")),
            divisions=tuple(Division.from_dict(d) for d in data["divisions"]),
            disciplines=tuple(Discipline.from_dict(d) for d in data["disciplines"]),
            rank_points=tuple(int(x) for x in data.get("rank_points", (100,))),
            dnf_points=int(data.get("dnf_points", 0)),
            dns_points=int(data.get("dns_points", 0)),
            exempt_policy=str(data.get("exempt_policy", EXEMPT_LAST_FINISHER)),
            drop_races=int(data.get("drop_races", 0)),
            qualification=tuple(QualificationRule.from_dict(q)
                                for q in data.get("qualification", ())),
            station_purse={str(k): tuple(float(x) for x in v)
                           for k, v in data.get("station_purse", {}).items()},
        )


# ---------------------------------------------------------------------------
# 规则注册表：预先发布、按生效日期解析
# ---------------------------------------------------------------------------

class RuleRegistry:
    """保存所有已发布版本；版本一经发布即不可变、不可覆盖。"""

    def __init__(self) -> None:
        self._versions: dict[str, RuleSet] = {}

    def publish(self, rules: RuleSet) -> RuleSet:
        if rules.version in self._versions:
            raise ValueError(f"规则版本已存在且不可变: {rules.version}")
        self._versions[rules.version] = rules
        return rules

    def get(self, version: str) -> RuleSet:
        return self._versions[version]

    def all(self) -> list[RuleSet]:
        return sorted(self._versions.values(), key=lambda r: parse_ts(r.effective_from))

    def effective_version(self, as_of: str) -> RuleSet:
        """返回 as_of 时刻已生效且发布时间不晚于 as_of 的最新版本。

        同时要求 published_at <= as_of：不能用赛后才发布的规则重排旧站。
        多个候选时取生效时间最晚者。
        """
        moment = parse_ts(as_of)
        candidates = [
            r for r in self._versions.values()
            if parse_ts(r.effective_from) <= moment
            and parse_ts(r.published_at) <= moment
            and (r.effective_to is None or parse_ts(r.effective_to) > moment)
        ]
        if not candidates:
            raise LookupError(f"{as_of} 时尚无生效的规则版本")
        return max(candidates, key=lambda r: parse_ts(r.effective_from))

    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.all()]


# ---------------------------------------------------------------------------
# 纯函数：成绩、名次、积分、奖金、晋级
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """一名选手在一站一个组别的原始成绩汇总（裁判判罚已并入）。"""

    competitor_id: str
    division_id: str
    status: str
    value: float | None = None        # time: 秒（含罚时）；reps: 次数
    raw_value: float | None = None    # 设备记录的原始值（不含罚时）
    penalty_seconds: float = 0.0
    completed_disciplines: int = 0
    discipline_count: int = 0


def _better(value: float, other: float, measure: str) -> bool:
    return value < other if measure == MEASURE_TIME else value > other


def rank_outcomes(outcomes: Sequence[Outcome], measure: str = MEASURE_TIME) -> list[dict[str, Any]]:
    """按成绩排序并列名次（标准竞赛排名 1,1,3）。

    完赛者在前（按成绩），DNF 其后（按完成分项数），DNS/EXEMPT 不排名次。
    返回 {competitor_id, status, value, rank, ranked: bool} 列表。
    """
    rows: list[dict[str, Any]] = []
    finished = [o for o in outcomes if o.status == STATUS_FINISHED and o.value is not None]
    dnfs = [o for o in outcomes if o.status == STATUS_DNF]
    unranked = [o for o in outcomes if o.status in (STATUS_DNS, STATUS_EXEMPT, STATUS_PENDING)]

    finished.sort(key=lambda o: o.value if measure == MEASURE_TIME else -o.value)  # type: ignore[operator]
    dnfs.sort(key=lambda o: -o.completed_disciplines)

    last_value: float | None = None
    rank = 0
    for position, o in enumerate(finished, start=1):
        if last_value is None or o.value != last_value:
            rank = position
            last_value = o.value
        rows.append({"competitor_id": o.competitor_id, "division_id": o.division_id,
                     "status": STATUS_FINISHED, "value": o.value, "raw_value": o.raw_value,
                     "penalty_seconds": o.penalty_seconds, "rank": rank, "ranked": True})

    dnf_rank = len(finished) + 1
    for o in dnfs:
        rows.append({"competitor_id": o.competitor_id, "division_id": o.division_id,
                     "status": STATUS_DNF, "value": o.value, "raw_value": o.raw_value,
                     "penalty_seconds": o.penalty_seconds, "rank": dnf_rank, "ranked": True})

    for o in unranked:
        rows.append({"competitor_id": o.competitor_id, "division_id": o.division_id,
                     "status": o.status, "value": None, "raw_value": o.raw_value,
                     "penalty_seconds": o.penalty_seconds, "rank": None, "ranked": False})
    return rows


def award_station_points(
    rankings: Sequence[dict[str, Any]], rules: RuleSet
) -> dict[str, dict[str, Any]]:
    """根据名次与状态发放单站积分。豁免者先标记，其积分由跨站汇总补齐。"""
    result: dict[str, dict[str, Any]] = {}
    last_finisher_points: int | None = None
    ranked_finishers = [r for r in rankings if r["status"] == STATUS_FINISHED]
    if ranked_finishers:
        last_finisher_points = rules.points_for_rank(len(ranked_finishers))

    for row in rankings:
        cid = row["competitor_id"]
        status = row["status"]
        if status == STATUS_FINISHED:
            points = rules.points_for_rank(row["rank"])
        elif status == STATUS_DNF:
            points = rules.dnf_points
        elif status == STATUS_PENDING:
            points = 0  # 待裁决期间暂记零分，裁决后只重算受影响者
        elif status == STATUS_EXEMPT:
            if rules.exempt_policy == EXEMPT_ZERO:
                points = 0
            elif rules.exempt_policy == EXEMPT_DNF:
                points = rules.dnf_points
            elif rules.exempt_policy == EXEMPT_LAST_FINISHER:
                points = last_finisher_points if last_finisher_points is not None else rules.dnf_points
            else:  # EXEMPT_AVERAGE：跨站汇总时计算
                points = None
        else:  # DNS
            points = rules.dns_points
        result[cid] = {"points": points, "status": status, "rank": row["rank"],
                       "value": row["value"], "exempt_replaced": status == STATUS_EXEMPT
                       and rules.exempt_policy == EXEMPT_AVERAGE}
    return result


def cross_station_totals(
    station_points: dict[str, dict[str, dict[str, Any]]],
    rules: RuleSet,
) -> dict[str, dict[str, Any]]:
    """汇总跨站积分。

    station_points: {station_id: {competitor_id: 单站积分行}}
    豁免 average 政策用本人其他已计分站积分均值补齐；drop_races 丢弃最低的若干站。
    """
    competitors: set[str] = set()
    for rows in station_points.values():
        competitors.update(rows)

    totals: dict[str, dict[str, Any]] = {}
    for cid in competitors:
        per_station: dict[str, float] = {}
        counted_average_inputs: list[float] = []
        exempt_stations: list[str] = []
        for station_id, rows in station_points.items():
            row = rows.get(cid)
            if row is None:
                continue
            if row["exempt_replaced"] and row["points"] is None:
                exempt_stations.append(station_id)
                continue
            per_station[station_id] = float(row["points"])
            counted_average_inputs.append(float(row["points"]))

        for station_id in exempt_stations:
            replacement = (sum(counted_average_inputs) / len(counted_average_inputs)
                           if counted_average_inputs else float(rules.dnf_points))
            per_station[station_id] = round(replacement, 3)

        values = list(per_station.values())
        dropped: list[float] = []
        if rules.drop_races > 0 and len(values) > rules.drop_races:
            ordered = sorted(values)
            dropped = ordered[: rules.drop_races]
            values = ordered[rules.drop_races:]
        totals[cid] = {
            "total": round(sum(values), 3),
            "stations": per_station,
            "dropped": dropped,
            "races_counted": len(values),
        }
    return totals


def rank_totals(totals: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """跨站总积分排序并列名次。"""
    items = sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    rows: list[dict[str, Any]] = []
    last_total: float | None = None
    rank = 0
    for position, (cid, data) in enumerate(items, start=1):
        if last_total is None or data["total"] != last_total:
            rank = position
            last_total = data["total"]
        rows.append({"competitor_id": cid, "rank": rank, "total": data["total"],
                     "races_counted": data["races_counted"]})
    return rows


def prize_for_rank(rank: int | None, division_id: str, rules: RuleSet) -> float:
    """按比赛时版本的奖金表发奖；无名次或超出表外为 0。"""
    purse = rules.station_purse.get(division_id, ())
    if rank is None or rank < 1 or rank > len(purse):
        return 0.0
    return float(purse[rank - 1])


def qualification_status(
    division_id: str,
    station_rank: int | None,
    station_value: float | None,
    total_points: float | None,
    rule_set: RuleSet,
) -> dict[str, Any] | None:
    """依据比赛时版本的晋级线判断是否晋级。"""
    q = rule_set.qualification_for(division_id)
    if q is None:
        return None
    if q.method == "rank":
        qualified = station_rank is not None and station_rank <= q.threshold
        detail = f"分站名次 {station_rank if station_rank is not None else '-'} / 线 {int(q.threshold)}"
    elif q.method == "points":
        qualified = total_points is not None and total_points >= q.threshold
        detail = f"累计积分 {total_points if total_points is not None else '-'} / 线 {q.threshold:g}"
    else:  # time
        qualified = station_value is not None and station_value <= q.threshold
        detail = f"完赛成绩 {station_value if station_value is not None else '-'}s / 线 {q.threshold:g}s"
    return {"method": q.method, "threshold": q.threshold, "qualified": qualified, "detail": detail}


# ---------------------------------------------------------------------------
# 默认版本（联调与测试便利）
# ---------------------------------------------------------------------------

def make_default_rules(
    version: str = "2026-v1",
    effective_from: str = "2026-01-01",
    published_at: str = "2025-12-01",
) -> RuleSet:
    """生成包含跑步/负重/体操三大分项与男女组的默认规则。"""
    return RuleSet(
        version=version,
        name=f"HYROX 类联赛 {version} 规则",
        published_at=published_at,
        effective_from=effective_from,
        divisions=(Division("men", "男子组"), Division("women", "女子组")),
        disciplines=(
            Discipline("run", "跑步分项", CATEGORY_RUN),
            Discipline("sled", "雪橇负重分项", CATEGORY_WEIGHTED),
            Discipline("gym", "体操分项", CATEGORY_GYMNASTICS),
        ),
        rank_points=(100, 92, 85, 79, 74, 70, 67, 64, 62, 60),
        dnf_points=10,
        dns_points=0,
        exempt_policy=EXEMPT_LAST_FINISHER,
        drop_races=0,
        qualification=(
            QualificationRule("men", "rank", 3),
            QualificationRule("women", "rank", 3),
        ),
        station_purse={
            "men": (5000.0, 3000.0, 1500.0),
            "women": (5000.0, 3000.0, 1500.0),
        },
    )
