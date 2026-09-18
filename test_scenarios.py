"""端到端场景测试：用一个三城赛季叙事覆盖全部业务规则。

人物：A1 猎豹、A2 磐石、A3 疾风、A4 远山；组别：男子精英组 DIV。
赛程：上海（3 月，规则 v1）→ 北京（6 月，规则 v1）→ 深圳（9 月，规则 v2）。

叙事要点：
1. 上海站 A2 出现多设备读数（自动去重 + 超阈值冲突挂起，裁决后才发布）；
2. A4 赛中伤退 = DNF 保底分，绝不是零分；
3. 上海站判罚引发申诉：申诉即冻结，冻结期间禁发布/禁更正；裁决引用证据；
4. 裁决翻盘后重发快照：只有真正受影响的 A1/A2 发生红冲与补发；
5. 北京站 A3 医疗豁免（保底分、公开榜脱敏）、A4 签到后未出发（DNF）、
   A1 异地补赛成绩按北京站规则计入；
6. 深圳站自动绑定 v2；赛后补发的"旧生效日版本"不能回溯上海；
7. 跨站积分、晋级线、赛季奖金按各站比赛时规则复现；
8. 事件日志落盘后重放，结果一致。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Application
from domain_models import EvidenceRef
from scoring import ResultsPending, StandingsFrozen
from appeals import AppealError
from service import Handler

STATIONS = [
    "ski_erg", "sled_push", "sled_pull", "burpee_broad_jump",
    "row", "farmers_carry", "sandbag_lunge", "wall_balls",
]

V1 = {
    "points_for_place": [10, 8, 6, 4, 2],
    "dnf_points": 1,
    "med_points": 3,
    "dns_points": 0,
    "dq_points": 0,
    "dedupe_window_s": 2.0,
    "conflict_tolerance_s": 1.0,
    "score_drops": 0,
    "qualify_places": 2,
    "min_events_for_qualification": 2,
    "event_purse": [100, 60, 40],
    "season_purse": [500, 300, 200],
    "currency": "CNY",
}
V2 = {
    **V1,
    "points_for_place": [12, 9, 7, 5, 3],
    "dnf_points": 2,
    "med_points": 4,
    "score_drops": 1,
    "event_purse": [200, 120, 80],
    "season_purse": [600, 400, 250],
}

TZ = timezone(timedelta(hours=8))


def ev(type_: str, ref: str) -> EvidenceRef:
    return EvidenceRef(type=type_, ref=ref)


def ts(date: str, minute: int) -> str:
    return (datetime.fromisoformat(date).replace(hour=10, tzinfo=TZ) + timedelta(minutes=minute)).isoformat()


class SeasonScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Application()
        cls._run_scenario()

    # ---- 场景驱动 ------------------------------------------------------
    @classmethod
    def _run_scenario(cls):
        app = cls.app
        a = app.admin
        ing = app.ingest

        cls.season_id = a.create_season("HYROX 中国系列赛", 2026, season_id="sea_2026")
        cls.div_id = a.register_division(cls.season_id, "男子精英组", division_id="div_elite_m")
        # 规则必须先于排赛发布：v1 于 1 月 1 日发布并生效
        cls.v1 = a.publish_rules(cls.season_id, "2026-01-01", V1, version_id="rules_v1",
                                 published_at="2026-01-01", note="赛季初版")
        cls.sh = a.schedule_event(cls.season_id, "上海", "2026-03-14", "上海站",
                                  event_id="evt_shanghai")
        cls.bj = a.schedule_event(cls.season_id, "北京", "2026-06-14", "北京站",
                                  event_id="evt_beijing")
        cls.sz = a.schedule_event(cls.season_id, "深圳", "2026-09-20", "深圳站",
                                  event_id="evt_shenzhen")

        cls.a1 = a.register_athlete(cls.season_id, "猎豹", "厉胜利", "B001", "华东",
                                    athlete_id="ath_a1")
        cls.a2 = a.register_athlete(cls.season_id, "磐石", "潘安", "B002", "华北",
                                    athlete_id="ath_a2")
        cls.a3 = a.register_athlete(cls.season_id, "疾风", "季锋（心脏隐忧代码 CARDIO-K2）",
                                    "B003", "华南", athlete_id="ath_a3")
        cls.a4 = a.register_athlete(cls.season_id, "远山", "袁山", "B004", "华西",
                                    athlete_id="ath_a4")
        for eid in (cls.sh, cls.bj, cls.sz):
            for aid in (cls.a1, cls.a2, cls.a3, cls.a4):
                a.grant_eligibility(eid, aid, cls.div_id)

        cls._run_shanghai()
        cls._run_beijing()
        cls._run_shenzhen()

    @classmethod
    def _full_readings(cls, event_id: str, athlete_id: str, date: str, per_station: int,
                       running: int, start_minute: int = 0, skip: set[str] | None = None):
        skip = skip or set()
        for i, station in enumerate(STATIONS):
            if station in skip:
                continue
            cls.app.ingest.record_reading(
                event_id, athlete_id, station, f"dev_{station}", "primary_timing",
                float(per_station + i), ts(date, start_minute + i))
        if "running" not in skip:
            cls.app.ingest.record_reading(
                event_id, athlete_id, "running", "dev_gate", "primary_timing",
                float(running), ts(date, start_minute + 9))

    @classmethod
    def _run_shanghai(cls):
        app, a, ing = cls.app, cls.app.admin, cls.app.ingest
        date = "2026-03-14"
        for aid in (cls.a1, cls.a2, cls.a3, cls.a4):
            a.check_in(cls.sh, aid, ts(date, -30))

        # A1 总时间 1328；A2 原始 1315 但有 +15s 判罚 → 1330（暂时第二）
        cls._full_readings(cls.sh, cls.a1, date, 100, 500)
        cls._full_readings(cls.sh, cls.a2, date, 99, 495)
        # A3 的 row 站留给下面的冲突场景
        cls._full_readings(cls.sh, cls.a3, date, 103, 510, skip={"row"})

        # A4 只完成前三站后赛中伤退
        for i, station in enumerate(STATIONS[:3]):
            ing.record_reading(cls.sh, cls.a4, station, "dev_x", "primary_timing",
                               float(110 + i), ts(date, i))
        ing.record_withdrawal(cls.sh, cls.a4, "赛中腿部拉伤", on_course=True)

        # A2 wall_balls：主计时 106（full_readings 已发），备份芯片 106.4
        # 同一秒采集、差值 0.4s <= 阈值 → 自动去重，主计时优先，不产生冲突
        dedupe = app.record_reading({
            "event_id": cls.sh, "athlete_id": cls.a2, "station_id": "wall_balls",
            "device_id": "dev_backup", "device_class": "backup_chip", "value_s": 106.4,
            "captured_at": ts(date, 7),
        })
        cls.dedupe_status = dedupe["status"]

        # A3 row：主计时 110.0 vs 人工秒表 114.5，差 4.5s → 冲突挂起，原值全保留
        ing.record_reading(cls.sh, cls.a3, "row", "dev_primary", "primary_timing",
                           110.0, ts(date, 4))
        conflict = app.record_reading({
            "event_id": cls.sh, "athlete_id": cls.a3, "station_id": "row",
            "device_id": "dev_manual", "device_class": "manual", "value_s": 114.5,
            "captured_at": (datetime.fromisoformat(ts(date, 4)) + timedelta(seconds=1)).isoformat(),
        })
        cls.conflict_group = conflict["group_id"]

        # A2 被判 no_rep 罚 15s（sled_push）
        cls.call_id = ing.record_judge_call(
            cls.sh, cls.a2, "sled_push", "penalty", "judge_07", penalty_s=15.0,
            reason="推雪阻力不足", evidence=[ev("judge_sheet", "SH-JS-07")])

    @classmethod
    def _run_beijing(cls):
        app, a, ing = cls.app, cls.app.admin, cls.app.ingest
        date = "2026-06-14"
        for aid in (cls.a1, cls.a2, cls.a3, cls.a4):
            a.check_in(cls.bj, aid, ts(date, -30))

        # A1：row 站设备故障，批准异地补赛（在杭州训练基地完成）
        ing.approve_makeup(cls.bj, cls.a1, "北京站 row 计时设备故障", "official_01")
        cls._full_readings(cls.bj, cls.a1, date, 98, 490, skip={"row"})
        ing.record_makeup_result(
            cls.bj, cls.a1, "row", 104.0, "杭州训练基地", "2026-06-16T09:00:00+08:00",
            evidence=[ev("video", "HZ-MAKEUP-01"), ev("timing_file", "HZ-TIM-01")])

        # A2 正常完赛，总时间略慢于 A1
        cls._full_readings(cls.bj, cls.a2, date, 101, 504)

        # A3 热身受伤，医疗豁免（公开榜不得出现原因/证明细节）
        cls.exemption_id = ing.grant_medical_exemption(
            cls.bj, cls.a3, "CARDIO", "2026-12-31",
            evidence=[ev("medical_form", "BJ-MED-03")])

        # A4 签到但未出发 → DNF（区别于完全没签到的 DNS）

    @classmethod
    def _run_shenzhen(cls):
        app, a = cls.app, cls.app.admin
        # v2 于 9 月 1 日发布并生效，深圳站 9 月 20 日自动绑定
        cls.v2 = a.publish_rules(cls.season_id, "2026-09-01", V2, version_id="rules_v2",
                                 published_at="2026-09-01",
                                 note="扩城后修订：积分与奖金调整、允许丢一站")
        date = "2026-09-20"
        for aid in (cls.a1, cls.a2, cls.a3):
            a.check_in(cls.sz, aid, ts(date, -30))
            a.grant_eligibility(cls.sz, aid, cls.div_id)
        # A4 未签到、无任何成绩 → DNS
        # A2 夺冠、A1 第二、A3 第三
        cls._full_readings(cls.sz, cls.a2, date, 98, 492)
        cls._full_readings(cls.sz, cls.a1, date, 100, 498)
        cls._full_readings(cls.sz, cls.a3, date, 103, 510)

    # ---- 断言（严格按叙事时间线）---------------------------------------
    def test_01_dedupe_is_automatic_conflict_is_preserved(self):
        # wall_balls 第二条备份读数被去重（主计时优先）
        self.assertEqual(self.dedupe_status, "DEDUPED")
        open_conflicts = self.app.list_open_conflicts(self.sh)["conflicts"]
        self.assertEqual([c["group_id"] for c in open_conflicts], [self.conflict_group])
        group = open_conflicts[0]
        self.assertEqual(sorted(group["values_s"]), [110.0, 114.5])

        preview = self.app.event_standings(self.sh, self.div_id, internal=False)
        a3 = next(r for r in preview["rows"] if r["athlete_id"] == self.a3)
        self.assertEqual(a3["status"], "UNDER_REVIEW")
        self.assertIsNone(a3["place"])
        # 挂起期间禁止发布正式名次
        with self.assertRaises(ResultsPending):
            self.app.publish_event_standings(self.sh, self.div_id)

        # 裁决必须有证据
        with self.assertRaises(ValueError):
            self.app.adjudicate({"group_id": self.conflict_group,
                                 "chosen_reading_id": group["reading_ids"][0]})
        self.app.adjudicate({
            "group_id": self.conflict_group,
            "chosen_reading_id": group["reading_ids"][0],  # 认定主计时 150.0
            "evidence": [{"type": "video", "ref": "SH-ROW-CAM-09"}],
        })
        self.assertEqual(self.app.list_open_conflicts(self.sh)["conflicts"], [])

    def test_02_injury_withdrawal_is_dnf_not_zero(self):
        snap = self.app.scoring.compute_event_standings(self.sh, self.div_id)
        row = {r["athlete_id"]: r for r in snap["rows"]}
        self.assertEqual(row[self.a4]["status"], "DNF")
        self.assertEqual(row[self.a4]["points"], 1)  # v1 DNF 保底 1 分，绝非 0
        self.assertTrue(row[self.a4]["trace"]["withdrawal_on_course"])

    def test_03_first_publish_posts_initial_awards(self):
        # 判罚在身：A1 第一、A2 第二、A3 第三
        snap = self.app.publish_event_standings(self.sh, self.div_id)
        places = {r["athlete_id"]: r["place"] for r in snap["rows"]}
        self.assertEqual(places[self.a1], 1)
        self.assertEqual(places[self.a2], 2)
        self.assertEqual(places[self.a3], 3)
        ledger = self.app.award_ledger("event", self.sh)
        net = {x["athlete_id"]: x["amount"] for x in ledger["net_payable"]}
        self.assertEqual(net[self.a1], 100.0)
        self.assertEqual(net[self.a2], 60.0)
        self.assertEqual(net[self.a3], 40.0)

        # 跑步/负重/体操分项名次可用
        a1 = next(r for r in snap["rows"] if r["athlete_id"] == self.a1)
        self.assertEqual(set(a1["component_places"]), {"running", "weighted", "gymnastics"})

    def test_04_appeal_freezes_and_requires_evidence(self):
        self.app.file_appeal({
            "event_id": self.sh, "athlete_id": self.a2, "contested": [self.call_id],
            "summary": "sled_push 判罚有误，录像显示动作达标",
            "evidence": [{"type": "video", "ref": "SH-SLED-CAM-02"}],
        })
        preview = self.app.event_standings(self.sh, self.div_id, internal=True)
        self.assertTrue(preview["frozen"])
        self.assertEqual(len(preview["open_appeals"]), 1)
        # 冻结期间：不得发布新快照
        with self.assertRaises(StandingsFrozen):
            self.app.publish_event_standings(self.sh, self.div_id)
        # 冻结期间：官方更正也被拒绝
        with self.assertRaises(AppealError):
            self.app.apply_correction({
                "event_id": self.sh, "reason": "计时补送", "trigger_refs": ["rdg_x"],
                "evidence": [{"type": "timing_file", "ref": "SH-TIM-FIX"}],
                "division_id": self.div_id,
            })
        # 申诉成立但不带证据 → 拒绝
        appeal_id = preview["open_appeals"][0]
        with self.assertRaises(AppealError):
            self.app.rule_appeal({"appeal_id": appeal_id, "decision": "SUSTAINED",
                                  "rescinded_call_ids": [self.call_id]})
        # 带证据裁决成立，撤销判罚
        self.app.rule_appeal({
            "appeal_id": appeal_id, "decision": "SUSTAINED",
            "rescinded_call_ids": [self.call_id],
            "note": "录像证实动作达标",
            "evidence": [{"type": "video", "ref": "SH-SLED-CAM-02"}],
        })

    def test_05_republish_only_touches_affected_athletes(self):
        snap2 = self.app.publish_event_standings(self.sh, self.div_id)
        places = {r["athlete_id"]: r["place"] for r in snap2["rows"]}
        self.assertEqual(places[self.a2], 1)  # 罚时撤销后翻盘
        self.assertEqual(places[self.a1], 2)
        self.assertEqual(snap2["rule_version_id"], "rules_v1")

        ledger = self.app.award_ledger("event", self.sh)["entries"]
        new_entries = [a for a in ledger if a["snapshot_id"] == snap2["snapshot_id"]]
        touched = {a["athlete_id"] for a in new_entries}
        self.assertEqual(touched, {self.a1, self.a2})  # A3 金额未变，不动账
        kinds = {(a["athlete_id"], a["status"]): a["amount"] for a in new_entries}
        self.assertEqual(kinds[(self.a1, "REVERSAL")], -100.0)
        self.assertEqual(kinds[(self.a1, "ADJUSTMENT")], 60.0)
        self.assertEqual(kinds[(self.a2, "REVERSAL")], -60.0)
        self.assertEqual(kinds[(self.a2, "ADJUSTMENT")], 100.0)
        # 红冲必须指回原始分录
        reversals = [a for a in new_entries if a["status"] == "REVERSAL"]
        self.assertTrue(all(a["supersedes_award_id"] for a in reversals))

        net = {x["athlete_id"]: x["amount"]
               for x in self.app.award_ledger("event", self.sh)["net_payable"]}
        self.assertEqual(net[self.a1], 60.0)
        self.assertEqual(net[self.a2], 100.0)
        self.assertEqual(net[self.a3], 40.0)

        # 内部差异视图：能从名次变化追到申诉裁决
        diff = self.app.event_diff(self.sh)
        changed = {c["athlete_id"]: c for c in diff["changes"]}
        self.assertNotIn(self.a3, changed)
        self.assertEqual(changed[self.a2]["movement"], 1)
        self.assertTrue(any("申诉裁决" in r for r in changed[self.a2]["reasons"]))
        self.assertEqual(changed[self.a1]["movement"], -1)

    def test_06_beijing_makeup_exemption_and_dnf(self):
        snap = self.app.publish_event_standings(self.bj, self.div_id)
        rows = {r["athlete_id"]: r for r in snap["rows"]}
        self.assertEqual(rows[self.a1]["place"], 1)
        self.assertEqual(rows[self.a2]["place"], 2)
        # A1 的 row 站来自异地补赛，按北京站（v1）计分
        a1_row = next(r for r in snap["rows"] if r["athlete_id"] == self.a1)
        row_mark = next(m for m in a1_row["marks"] if m["station_id"] == "row")
        self.assertEqual(row_mark["source"], "MAKEUP")
        self.assertEqual(row_mark["location"], "杭州训练基地")
        self.assertTrue(any(m["station_id"] == "row" and m["source"] == "MAKEUP"
                            for m in a1_row["marks"]))
        # 医疗豁免：保底 3 分，不是零分
        self.assertEqual(rows[self.a3]["status"], "MED")
        self.assertEqual(rows[self.a3]["points"], 3)
        # 签到未出发：DNF 1 分
        self.assertEqual(rows[self.a4]["status"], "DNF")
        self.assertEqual(rows[self.a4]["points"], 1)

        # 公开榜脱敏：无身份/医疗细节
        public = self.app.event_standings(self.bj, self.div_id, internal=False)
        blob = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("CARDIO", blob)
        self.assertNotIn("BJ-MED-03", blob)
        self.assertNotIn("legal_name", blob)
        self.assertNotIn("季锋", blob)  # 含医疗代码的法定名不外露
        a3_public = next(r for r in public["rows"] if r["athlete_id"] == self.a3)
        self.assertEqual(a3_public["status"], "EXEMPT")
        self.assertEqual(a3_public["points"], 3)  # 保底分事实可见

        # 内部视图可见全部细节与证据链
        internal = self.app.event_standings(self.bj, self.div_id, internal=True)
        a3_internal = next(r for r in internal["rows"] if r["athlete_id"] == self.a3)
        self.assertEqual(a3_internal["legal_name"], "季锋（心脏隐忧代码 CARDIO-K2）")
        self.assertEqual(a3_internal["medical"]["evidence"], ["BJ-MED-03"])

    def test_07_medical_exemption_requires_evidence(self):
        # 无证据的豁免一律拒绝
        from ingest import IngestService
        with self.assertRaises(ValueError):
            IngestService(self.app.store).grant_medical_exemption(
                self.sz, self.a4, "X", "2026-12-31", evidence=[])

    def test_08_new_rules_bind_shenzhen_but_never_retroact(self):
        self.assertEqual(self.app.store.bind_rules(self.sz).version_id, "rules_v2")
        self.assertEqual(self.app.store.bind_rules(self.sh).version_id, "rules_v1")
        self.assertEqual(self.app.store.bind_rules(self.bj).version_id, "rules_v1")

        # 深圳站：A4 未签到 = DNS 零分（与伤退/豁免严格区分）
        snap = self.app.publish_event_standings(self.sz, self.div_id)
        rows = {r["athlete_id"]: r for r in snap["rows"]}
        self.assertEqual(rows[self.a2]["place"], 1)
        self.assertEqual(rows[self.a1]["place"], 2)
        self.assertEqual(rows[self.a3]["place"], 3)
        self.assertEqual(rows[self.a4]["status"], "DNS")
        self.assertEqual(rows[self.a4]["points"], 0)
        self.assertEqual(snap["rule_version_id"], "rules_v2")

        # 上海快照历史永远是 v1，且可原样复现
        history = self.app.store.state.snapshots[("event", self.sh)]
        self.assertTrue(all(s["snapshot"]["rule_version_id"] == "rules_v1" for s in history))

        # 赛后（10 月 1 日）再发一个把生效日写成 1 月 1 日的版本：
        # 发布日晚于任何一站，三站都不得回溯；它只可能影响未来的新站
        self.app.admin.publish_rules(self.season_id, "2026-01-01", V1,
                                     version_id="rules_v11_retro",
                                     published_at="2026-10-01")
        self.assertEqual(self.app.store.bind_rules(self.sh).version_id, "rules_v1")
        self.assertEqual(self.app.store.bind_rules(self.bj).version_id, "rules_v1")
        self.assertEqual(self.app.store.bind_rules(self.sz).version_id, "rules_v2")

        # 显式把 v2 钉给上海站必须被拒绝
        with self.assertRaises(ValueError):
            self.app.admin.schedule_event(
                self.season_id, "广州", "2026-03-15", "广州站",
                event_id="evt_gz_illegal", pinned_rule_version_id="rules_v2")

    def test_09_season_points_cut_and_purse_use_period_rules(self):
        season = self.app.publish_season_standings(self.season_id, self.div_id)
        by_id = {r["athlete_id"]: r for r in season["rows"]}
        # v2 允许丢最差一站：
        # A1: 沪 8 + 京 10 + 深 9 → 丢 8 = 19
        # A2: 沪 10 + 京 8 + 深 12 → 丢 8 = 22
        # A3: 沪 6 + 京 3(MED) + 深 7 → 丢 3 = 13
        # A4: 沪 1 + 京 1 + 深 DNS(不计参赛) → 丢 1 = 1
        self.assertEqual(by_id[self.a2]["season_points"], 22)
        self.assertEqual(by_id[self.a1]["season_points"], 19)
        self.assertEqual(by_id[self.a3]["season_points"], 13)
        self.assertEqual(by_id[self.a4]["season_points"], 1)
        self.assertEqual(by_id[self.a2]["place"], 1)
        self.assertEqual(by_id[self.a1]["place"], 2)
        # 晋级线：前 2 名
        self.assertTrue(by_id[self.a2]["above_cut"])
        self.assertTrue(by_id[self.a1]["above_cut"])
        self.assertFalse(by_id[self.a3]["above_cut"])
        # 各站积分来自当时绑定的版本
        legs = {l["event_id"]: l for l in by_id[self.a1]["legs"]}
        self.assertEqual(legs[self.sh]["rule_version_id"], "rules_v1")
        self.assertEqual(legs[self.sz]["rule_version_id"], "rules_v2")
        # 赛季奖金按 v2 发放（前三：600/400/250）
        net = {x["athlete_id"]: x["amount"]
               for x in self.app.award_ledger("season", self.season_id)["net_payable"]}
        self.assertEqual(net[self.a2], 600.0)
        self.assertEqual(net[self.a1], 400.0)
        self.assertEqual(net[self.a3], 250.0)
        self.assertNotIn(self.a4, net)  # 积分垫底且未过参赛门槛，无奖金

        # 公开赛季榜同样不含医疗/身份信息
        public = self.app.season_standings(self.season_id, self.div_id, internal=False)
        blob = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("CARDIO", blob)
        self.assertNotIn("legal_name", blob)

    def test_10_event_log_replays_to_identical_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/events.jsonl"
            seeded = Application(path)
            sid = seeded.admin.create_season("测试赛季", 2026, season_id="sea_t")
            seeded.admin.publish_rules(sid, "2026-01-01", V1, version_id="rv_t",
                                       published_at="2026-01-01")
            eid = seeded.admin.schedule_event(sid, "测试城", "2026-04-01", "测试站",
                                              event_id="evt_t")
            did = seeded.admin.register_division(sid, "组", division_id="div_t")
            x = seeded.admin.register_athlete(sid, "X", "薛某", "X1", athlete_id="ath_x")
            y = seeded.admin.register_athlete(sid, "Y", "杨某", "Y1", athlete_id="ath_y")
            for aid in (x, y):
                seeded.admin.grant_eligibility(eid, aid, did)
                seeded.admin.check_in(eid, aid, "2026-04-01T09:00:00+08:00")
            for i, station in enumerate(STATIONS):
                seeded.ingest.record_reading(eid, x, station, "d", "primary_timing",
                                             float(100 + i), f"2026-04-01T10:{i:02d}:00+08:00")
                seeded.ingest.record_reading(eid, y, station, "d", "primary_timing",
                                             float(102 + i), f"2026-04-01T10:{i:02d}:00+08:00")
            seeded.ingest.record_reading(eid, x, "running", "d", "primary_timing",
                                         500.0, "2026-04-01T10:09:00+08:00")
            seeded.ingest.record_reading(eid, y, "running", "d", "primary_timing",
                                         510.0, "2026-04-01T10:09:00+08:00")
            before = seeded.publish_event_standings(eid, did)

            replayed = Application(path)
            after = replayed.scoring.compute_event_standings(eid, did)
            self.assertEqual(
                [(r["athlete_id"], r["place"], r["points"], r["total_time_s"]) for r in after["rows"]],
                [(r["athlete_id"], r["place"], r["points"], r["total_time_s"]) for r in before["rows"]],
            )
            # 奖金账本也随事件重放
            self.assertEqual(
                len(replayed.store.state.awards), len(seeded.store.state.awards)
            )


class HttpApiTest(unittest.TestCase):
    """HTTP 层：冻结返回 409、公开视图脱敏、健康检查契约保持。"""

    @classmethod
    def setUpClass(cls):
        cls.app = Application()
        sid = cls.app.admin.create_season("HTTP 赛季", 2026, season_id="sea_h")
        cls.app.admin.publish_rules(sid, "2026-01-01", V1, version_id="rv_h",
                                    published_at="2026-01-01")
        cls.eid = cls.app.admin.schedule_event(sid, "测试城", "2026-05-01", "测试站",
                                               event_id="evt_h")
        cls.did = cls.app.admin.register_division(sid, "组", division_id="div_h")
        cls.aid = cls.app.admin.register_athlete(
            sid, "代号X", "隐名真人", "X9", athlete_id="ath_hx")
        cls.app.admin.grant_eligibility(cls.eid, cls.aid, cls.did)
        cls.app.ingest.grant_medical_exemption(
            cls.eid, cls.aid, "SECRET_CODE", "2026-12-31",
            evidence=[ev("medical_form", "H-MED-1")])

        class ConfiguredHandler(Handler):
            app = cls.app

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ConfiguredHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, path: str, payload: dict):
        request = Request(
            f"{self.base}{path}", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        return urlopen(request, timeout=5)

    def test_health_contract_holds(self):
        with urlopen(f"{self.base}/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["service"], "fitness-event-scoring")

    def test_frozen_standings_return_409(self):
        # 申诉提出 → 同组冻结 → 发布返回 409
        appeal = self._post("/appeals", {
            "event_id": self.eid, "athlete_id": self.aid, "contested": ["station_x"],
            "summary": "测试冻结",
        })
        self.assertTrue(json.load(appeal)["frozen"])
        with self.assertRaises(HTTPError) as error:
            self._post(f"/events/{self.eid}/standings/publish", {"division_id": self.did})
        self.assertEqual(error.exception.code, 409)
        body = json.load(error.exception)
        self.assertEqual(body["error"], "StandingsFrozen")
        error.exception.close()

    def test_no_rules_for_early_date_is_404(self):
        with self.assertRaises(HTTPError) as error:
            self._post("/admin/events", {"season_id": "sea_h", "city": "无规则城",
                                         "date": "2025-01-01", "name": "早期站"})
        self.assertEqual(error.exception.code, 404)  # 比赛日无已发布且生效的版本
        error.exception.close()

    def test_public_endpoint_hides_medical(self):
        with urlopen(
            f"{self.base}/events/{self.eid}/standings?division_id={self.did}", timeout=5
        ) as response:
            blob = response.read().decode("utf-8")
        self.assertNotIn("SECRET_CODE", blob)
        self.assertNotIn("隐名真人", blob)
        self.assertNotIn("H-MED-1", blob)


if __name__ == "__main__":
    unittest.main()
