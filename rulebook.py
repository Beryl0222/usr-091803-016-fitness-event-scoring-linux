"""规则版本与规则绑定。

规则以不可变版本预先发布，带 ``effective_date``。某站使用的版本由
比赛日决定：取在该日（含）之前已生效的最新版本；主办方也可显式钉选。
赛后才发布/生效的新版本永远不会作用于旧站——奖金重排只能引用比赛
发生时绑定的版本，保证跨站积分、晋级线和奖金可复现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

# 缺席处理策略
ABS_DNF = "DNF"  # 退赛/伤退：有参与证据的未完赛
ABS_MED = "MED"  # 医疗豁免：保底分
ABS_MAKEUP = "MAKEUP"  # 已批准补赛：等待/采用补赛成绩
ABS_DNS = "DNS"  # 无故缺席：零分
ABS_DQ = "DQ"  # 取消资格：零分

# HYROX 式八个标准功能站 + 跑步分段，供默认规则使用
DEFAULT_STATIONS: tuple[str, ...] = (
    "ski_erg",
    "sled_push",
    "sled_pull",
    "burpee_broad_jump",
    "row",
    "farmers_carry",
    "sandbag_lunge",
    "wall_balls",
)
RUNNING_STATION = "running"  # 八个跑步分段按一个跑步分项汇总比较


def _iso(value: str) -> date:
    return date.fromisoformat(value)


@dataclass(frozen=True)
class ScoringRules:
    """一个不可变计分版本的全部参数。"""

    version_id: str
    season_id: str
    effective_date: str
    published_at: str
    stations: tuple[str, ...]
    # 排名依据：TOTAL_TIME 总时间（含罚时），或 POINTS 分制
    rank_basis: str
    points_for_place: tuple[int, ...] | None  # 第 n 名得分；超出后取末位
    ties: str  # SHARE 并列同名次 | COMPETE 并列按证据打破
    # 缺席保底/计零策略
    dnf_points: int
    med_points: int
    dns_points: int
    dq_points: int
    # 多设备去重
    dedupe_window_s: float
    device_priority: tuple[str, ...]  # 读数一致时的设备优先序
    conflict_tolerance_s: float  # 差值超过该阈值即视为冲突，挂起待裁
    # 分项归组：跑步 / 负重 / 体操（可由版本配置）
    components: dict[str, tuple[str, ...]]
    # 跨站积分
    score_drops: int  # 计分时允许丢弃的最差站数（0 = 全部计入）
    # 晋级：按赛季积分排名取前 qualify_places，且分站门槛
    qualify_places: int
    min_events_for_qualification: int
    # 奖金表（名次 -> 金额）；分站与赛季分开
    event_purse: tuple[int, ...]
    season_purse: tuple[int, ...]
    currency: str
    note: str = ""

    @staticmethod
    def from_payload(
        version_id: str,
        season_id: str,
        effective_date: str,
        published_at: str,
        rules: dict[str, Any],
        note: str = "",
    ) -> "ScoringRules":
        merged = _default_rule_payload()
        merged.update(rules or {})
        return ScoringRules(
            version_id=version_id,
            season_id=season_id,
            effective_date=effective_date,
            published_at=published_at,
            stations=tuple(merged["stations"]),
            rank_basis=merged["rank_basis"],
            points_for_place=(
                tuple(merged["points_for_place"]) if merged.get("points_for_place") else None
            ),
            ties=merged["ties"],
            dnf_points=int(merged["dnf_points"]),
            med_points=int(merged["med_points"]),
            dns_points=int(merged["dns_points"]),
            dq_points=int(merged["dq_points"]),
            dedupe_window_s=float(merged["dedupe_window_s"]),
            device_priority=tuple(merged["device_priority"]),
            conflict_tolerance_s=float(merged["conflict_tolerance_s"]),
            components={
                name: tuple(stations) for name, stations in merged["components"].items()
            },
            score_drops=int(merged["score_drops"]),
            qualify_places=int(merged["qualify_places"]),
            min_events_for_qualification=int(merged["min_events_for_qualification"]),
            event_purse=tuple(int(x) for x in merged["event_purse"]),
            season_purse=tuple(int(x) for x in merged["season_purse"]),
            currency=str(merged["currency"]),
            note=note,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "season_id": self.season_id,
            "effective_date": self.effective_date,
            "published_at": self.published_at,
            "stations": list(self.stations),
            "rank_basis": self.rank_basis,
            "points_for_place": list(self.points_for_place) if self.points_for_place else None,
            "ties": self.ties,
            "dnf_points": self.dnf_points,
            "med_points": self.med_points,
            "dns_points": self.dns_points,
            "dq_points": self.dq_points,
            "dedupe_window_s": self.dedupe_window_s,
            "device_priority": list(self.device_priority),
            "conflict_tolerance_s": self.conflict_tolerance_s,
            "components": {name: list(stations) for name, stations in self.components.items()},
            "score_drops": self.score_drops,
            "qualify_places": self.qualify_places,
            "min_events_for_qualification": self.min_events_for_qualification,
            "event_purse": list(self.event_purse),
            "season_purse": list(self.season_purse),
            "currency": self.currency,
            "note": self.note,
        }


def _default_rule_payload() -> dict[str, Any]:
    # HYROX 式默认：总时间排名，前 50 名按 50→1 给分（与 HYROX 类似），
    # 其余完赛 1 分；医疗保底 10 分、DNF 5 分、DNS/DQ 0 分。
    points = list(range(50, 0, -1))
    return {
        "stations": list(DEFAULT_STATIONS),
        "rank_basis": "TOTAL_TIME",
        "points_for_place": points,
        "ties": "SHARE",
        "dnf_points": 5,
        "med_points": 10,
        "dns_points": 0,
        "dq_points": 0,
        "dedupe_window_s": 2.0,
        "device_priority": ["primary_timing", "backup_chip", "manual"],
        "conflict_tolerance_s": 1.0,
        # 跑步 / 负重 / 体操三大分项的站点归组，可随版本调整
        "components": {
            "running": ["running"],
            "weighted": ["sled_push", "sled_pull", "farmers_carry", "sandbag_lunge"],
            "gymnastics": ["ski_erg", "burpee_broad_jump", "row", "wall_balls"],
        },
        "score_drops": 0,
        "qualify_places": 10,
        "min_events_for_qualification": 4,
        "event_purse": [5000, 3000, 2000, 1000, 500],
        "season_purse": [20000, 12000, 8000, 4000, 2000],
        "currency": "CNY",
    }


class RuleRegistry:
    """内存中保存所有已发布版本，并决定某站绑定哪个版本。"""

    def __init__(self) -> None:
        self._versions: dict[str, ScoringRules] = {}

    def publish(self, rules: ScoringRules) -> None:
        if rules.version_id in self._versions:
            raise ValueError(f"规则版本已存在且不可变: {rules.version_id}")
        self._versions[rules.version_id] = rules

    def get(self, version_id: str) -> ScoringRules:
        try:
            return self._versions[version_id]
        except KeyError:
            raise KeyError(f"规则版本不存在: {version_id}") from None

    def versions_for_season(self, season_id: str) -> list[ScoringRules]:
        # 以"最新发布"为最新版本，生效日期为次序
        return sorted(
            (v for v in self._versions.values() if v.season_id == season_id),
            key=lambda v: (_iso(v.published_at), _iso(v.effective_date), v.version_id),
        )

    def bind(
        self,
        season_id: str,
        event_date: str,
        pinned_version_id: str | None = None,
    ) -> ScoringRules:
        """决定比赛日适用的规则版本。

        显式钉选优先；否则取比赛日当天满足以下两个条件的最新版本：
        ``effective_date <= 比赛日`` **且** 发布日 <= 比赛日。第二个条件
        堵住"赛后补发一个把生效日写得很早的版本重排旧站"的漏洞——
        规则必须在比赛发生时已经发布。
        """
        race_day = _iso(event_date)

        def in_force_at_race(version: ScoringRules) -> bool:
            return _iso(version.effective_date) <= race_day and _iso(
                version.published_at
            ) <= race_day

        if pinned_version_id is not None:
            pinned = self.get(pinned_version_id)
            if pinned.season_id != season_id:
                raise ValueError("钉选版本不属于该赛季")
            if _iso(pinned.effective_date) > race_day:
                raise ValueError(
                    f"禁止回溯：版本 {pinned.version_id} 生效日 {pinned.effective_date} 晚于比赛日 {event_date}"
                )
            if _iso(pinned.published_at) > race_day:
                raise ValueError(
                    f"禁止回溯：版本 {pinned.version_id} 发布于 {pinned.published_at[:10]}，晚于比赛日 {event_date}"
                )
            return pinned
        candidates = [
            v for v in self.versions_for_season(season_id) if in_force_at_race(v)
        ]
        if not candidates:
            raise LookupError(f"比赛日 {event_date} 没有任何已发布且已生效的规则版本")
        return candidates[-1]
