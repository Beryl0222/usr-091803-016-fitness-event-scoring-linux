"""端到端领域场景：去重、冲突保留、豁免、补赛、冻结、更正与旧规则复现。"""

import copy
import unittest

from domain import rules
from domain.competition import (
    CompetitionService, DomainError, FrozenStandingsError, NotFound)
from domain import views


def build_service() -> CompetitionService:
    svc = CompetitionService()
    v1 = rules.make_default_rules(version="v1", effective_from="2026-01-01",
                                  published_at="2025-12-01")
    svc.publish_rules(v1.to_dict())
    # v2：提高奖金、调整晋级线、豁免改为零分——只能影响 v2 生效后的站。
    v2 = v1.to_dict()
    v2.update({
        "version": "v2", "published_at": "2026-04-15", "effective_from": "2026-05-01",
        "rank_points": (120, 100, 85), "dnf_points": 5,
        "exempt_policy": rules.EXEMPT_ZERO,
        "qualification": [{"division_id": "men", "method": "points", "threshold": 200},
                          {"division_id": "women", "method": "points", "threshold": 200}],
        "station_purse": {"men": (8000.0, 4000.0, 2000.0),
                          "women": (8000.0, 4000.0, 2000.0)},
    })
    svc.publish_rules(v2)
    svc.create_season("s2026", "2026 城市联赛")
    return svc


def seed_station(svc: CompetitionService, station_id: str, city: str, occurs_at: str,
                 competitors: dict[str, str]) -> None:
    svc.schedule_station(station_id, "s2026", f"{city}站", city, occurs_at)
    for cid, name in competitors.items():
        svc.register_competitor(cid, name)
        svc.enter_station(station_id, cid, "men")
        svc.check_in(station_id, cid)


def full_race(svc: CompetitionService, station_id: str, cid: str,
              values: dict[str, float]) -> None:
    for discipline_id, value in values.items():
        svc.record_reading(station_id, cid, discipline_id, "mat-A", value,
                           read_at=f"{station_id}T10:00:00+00:00")


def men_rows(svc: CompetitionService, station_id: str) -> dict[str, dict]:
    result = svc.station_results(station_id)
    return {r["competitor_id"]: r for r in result["divisions"]["men"]}


class CaptureAndDedupTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_station(self.svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强", "b": "阿伟"})

    def test_second_device_same_value_is_deduplicated(self):
        r1 = self.svc.record_reading("bj", "a", "run", "mat-A", 300.0,
                                     read_at="2026-03-10T10:00:00+00:00")
        r2 = self.svc.record_reading("bj", "a", "run", "backup-B", 300.0,
                                     read_at="2026-03-10T10:00:01+00:00")
        self.assertEqual(r1["status"], "active")
        self.assertEqual(r2["status"], "duplicate")
        self.assertEqual(r2["conflict_with"], r1["event_id"])

    def test_conflicting_values_are_both_retained_until_adjudication(self):
        r1 = self.svc.record_reading("bj", "a", "run", "mat-A", 300.0,
                                     read_at="2026-03-10T10:00:00+00:00")
        r2 = self.svc.record_reading("bj", "a", "run", "chip-C", 320.0,
                                     read_at="2026-03-10T10:00:02+00:00")
        self.assertEqual(r2["status"], "conflicted")
        # 冲突未裁决：a 的 run 分项待裁决，成绩不进榜。
        full_race(self.svc, "bj", "a", {"sled": 400.0, "gym": 200.0})
        full_race(self.svc, "bj", "b", {"run": 305.0, "sled": 410.0, "gym": 210.0})
        rows = men_rows(self.svc, "bj")
        self.assertEqual(rows["a"]["status"], rules.STATUS_PENDING)
        self.assertIsNone(rows["a"]["rank"])
        self.assertEqual(rows["b"]["rank"], 1)
        # 两条原值都保留在证据里。
        raw = [e for e in rows["a"]["evidence"]["readings"] if e["discipline_id"] == "run"]
        self.assertEqual(sorted(e["value"] for e in raw), [300.0, 320.0])
        self.assertTrue(all(e["state"] == "conflicted" for e in raw))

        # 裁决选择 320 那条后，a 以 920 秒完赛，名次重算。
        self.svc.resolve_reading_conflict("bj", "a", "run", r2["event_id"], note="芯片为准")
        rows = men_rows(self.svc, "bj")
        self.assertEqual(rows["a"]["status"], rules.STATUS_FINISHED)
        self.assertEqual(rows["a"]["value"], 920.0)
        self.assertEqual(rows["a"]["rank"], 1)
        self.assertEqual(rows["b"]["rank"], 2)


class ExemptionAndMakeupTest(unittest.TestCase):
    def test_injury_without_checkin_is_dns_zero(self):
        svc = build_service()
        seed_station(svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强", "b": "阿伟"})
        full_race(svc, "bj", "b", {"run": 305.0, "sled": 410.0, "gym": 210.0})
        rows = men_rows(svc, "bj")
        self.assertEqual(rows["a"]["status"], rules.STATUS_DNS)
        self.assertEqual(rows["a"]["points"], 0)
        self.assertIsNone(rows["a"]["rank"])

    def test_station_medical_exemption_uses_last_finisher_points(self):
        svc = build_service()
        seed_station(svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强", "b": "阿伟", "c": "阿杰"})
        full_race(svc, "bj", "b", {"run": 305.0, "sled": 410.0, "gym": 210.0})
        full_race(svc, "bj", "c", {"run": 330.0, "sled": 430.0, "gym": 230.0})
        svc.grant_medical_exemption("bj", "a", "MED-77", reason="跟腱伤")
        rows = men_rows(svc, "bj")
        self.assertEqual(rows["a"]["status"], rules.STATUS_EXEMPT)
        # v1 政策：豁免按最后完赛者积分（2 人完赛 -> 92）。
        self.assertEqual(rows["a"]["points"], 92)

    def test_discipline_exemption_uses_median_replacement(self):
        svc = build_service()
        seed_station(svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强", "b": "阿伟"})
        full_race(svc, "bj", "b", {"run": 300.0, "sled": 400.0, "gym": 200.0})
        full_race(svc, "bj", "a", {"run": 290.0, "gym": 190.0})  # sled 伤退
        svc.grant_medical_exemption("bj", "a", "MED-78", scope="discipline",
                                    discipline_id="sled")
        rows = men_rows(svc, "bj")
        # sled 替代值取同组中位数 400；总分 290+400+190=880，第一名。
        self.assertEqual(rows["a"]["status"], rules.STATUS_FINISHED)
        self.assertEqual(rows["a"]["value"], 880.0)
        self.assertEqual(rows["a"]["rank"], 1)

    def test_makeup_at_another_city_counts_for_source_station(self):
        svc = build_service()
        svc.schedule_station("bj", "s2026", "北京站", "北京", "2026-03-10T09:00:00+00:00")
        svc.schedule_station("sh", "s2026", "上海站", "上海", "2026-03-24T09:00:00+00:00")
        svc.register_competitor("a", "阿强")
        svc.register_competitor("b", "阿伟")
        svc.enter_station("bj", "a", "men")
        svc.enter_station("bj", "b", "men")
        # a 伤退未签到北京站；b 正常签到。
        svc.check_in("bj", "b")
        full_race(svc, "bj", "b", {"run": 305.0, "sled": 410.0, "gym": 210.0})
        self.assertEqual(men_rows(svc, "bj")["a"]["status"], rules.STATUS_DNS)
        # a 在上海异地补赛，成绩计入北京站。
        mk = svc.record_makeup(
            "bj", "sh", "a", {"run": 295.0, "sled": 395.0, "gym": 195.0},
            recorded_at="2026-03-24T12:00:00+00:00")
        self.assertEqual(mk["applies_to_station"], "bj")
        rows = men_rows(svc, "bj")
        self.assertEqual(rows["a"]["status"], rules.STATUS_FINISHED)
        self.assertEqual(rows["a"]["value"], 885.0)
        self.assertEqual(rows["a"]["rank"], 1)
        self.assertEqual(rows["a"]["evidence"]["makeup"]["host_station_id"], "sh")


class AppealFreezeAndCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_station(self.svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强", "b": "阿伟", "c": "阿杰"})
        full_race(self.svc, "bj", "a", {"run": 300.0, "sled": 400.0, "gym": 200.0})
        full_race(self.svc, "bj", "b", {"run": 310.0, "sled": 410.0, "gym": 210.0})
        full_race(self.svc, "bj", "c", {"run": 320.0, "sled": 420.0, "gym": 220.0})

    def test_freeze_blocks_direct_correction_and_snapshot_is_kept(self):
        before = men_rows(self.svc, "bj")
        self.assertEqual([before[k]["rank"] for k in ("a", "b", "c")], [1, 2, 3])
        appeal = self.svc.open_appeal("a 申诉 sled 计时有误", station_id="bj",
                                      competitor_id="a")
        snap = self.svc.get_appeal_snapshot(appeal["appeal_id"])
        self.assertEqual(snap["standings"]["rule_version"], "v1")
        self.assertTrue(self.svc.station_results("bj")["frozen"])

        target = next(e for e in self.svc.store.all()
                      if e.type == "device_reading"
                      and e.payload["competitor_id"] == "a"
                      and e.payload["discipline_id"] == "sled")
        with self.assertRaises(FrozenStandingsError):
            self.svc.apply_correction("bj", "a", "amend_reading", target.id,
                                      "绕过申诉更正", new_value=500.0)

        # 通过申诉裁决通道更正：a 的 sled 400->500，掉到第 3，b/c 上移。
        closed = self.svc.close_appeal(
            appeal["appeal_id"], "amended", note="计时设备故障，按备份修正",
            corrections=[{"station_id": "bj", "competitor_id": "a",
                          "kind": "amend_reading", "target_event_id": target.id,
                          "new_value": 500.0}])
        affected = {x["competitor_id"]: x for x in closed["affected"]}
        self.assertEqual(set(affected), {"a", "b", "c"})
        self.assertEqual(affected["a"]["fields"]["rank"], {"before": 1, "after": 3})
        after = men_rows(self.svc, "bj")
        self.assertEqual(after["a"]["value"], 1000.0)
        self.assertFalse(self.svc.station_results("bj")["frozen"])

        # 更正引用了原始证据，可沿因果链追溯。
        corr = next(e for e in self.svc.store.all() if e.type == "correction_applied")
        chain = self.svc.evidence_chain(corr.id)
        self.assertEqual([e["type"] for e in chain], ["correction_applied", "device_reading"])

    def test_only_affected_people_are_recomputed_on_void_penalty(self):
        # 给 b 一个 30 秒罚时：b(960) 落到 c(960) 之后？构造让 b/c 接近。
        pen = self.svc.record_penalty("bj", "b", "sled", judge_id="J1",
                                      reason="站位违规", seconds=40.0)
        rows = men_rows(self.svc, "bj")
        self.assertEqual([rows[k]["rank"] for k in ("a", "b", "c")], [1, 3, 2])
        appeal = self.svc.open_appeal("b 申诉判罚", station_id="bj", competitor_id="b")
        closed = self.svc.close_appeal(
            appeal["appeal_id"], "upheld", note="撤销判罚",
            corrections=[{"station_id": "bj", "competitor_id": "b",
                          "kind": "void_penalty", "target_event_id": pen["event_id"],
                          "new_value": None}])
        affected = {x["competitor_id"] for x in closed["affected"]}
        # a 名次不变，不应出现在受影响名单里；只有 b、c 交换。
        self.assertEqual(affected, {"b", "c"})
        rows = men_rows(self.svc, "bj")
        self.assertEqual([rows[k]["rank"] for k in ("a", "b", "c")], [1, 2, 3])


class RuleVersionReproductionTest(unittest.TestCase):
    def test_old_station_keeps_v1_while_new_station_uses_v2(self):
        svc = build_service()
        seed_station(svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强", "b": "阿伟"})
        seed_station(svc, "sh", "上海", "2026-05-20T09:00:00+00:00", {})
        for cid in ("a", "b"):  # 同一批选手参加两站
            svc.enter_station("sh", cid, "men")
            svc.check_in("sh", cid)
        full_race(svc, "bj", "a", {"run": 300.0, "sled": 400.0, "gym": 200.0})
        full_race(svc, "bj", "b", {"run": 310.0, "sled": 410.0, "gym": 210.0})
        full_race(svc, "sh", "a", {"run": 300.0, "sled": 400.0, "gym": 200.0})
        full_race(svc, "sh", "b", {"run": 310.0, "sled": 410.0, "gym": 210.0})

        bj = men_rows(svc, "bj")
        sh = men_rows(svc, "sh")
        # 旧站仍按 v1：积分 100、奖金 5000、按名次晋级。
        self.assertEqual(bj["a"]["rule_version"], "v1")
        self.assertEqual(bj["a"]["points"], 100)
        self.assertEqual(bj["a"]["prize"], 5000.0)
        self.assertTrue(bj["a"]["qualification"]["qualified"])  # 前 3
        # 新站按 v2：积分 120、奖金 8000、按累计积分晋级（单站不直接判）。
        self.assertEqual(sh["a"]["rule_version"], "v2")
        self.assertEqual(sh["a"]["points"], 120)
        self.assertEqual(sh["a"]["prize"], 8000.0)
        self.assertFalse(sh["a"]["qualification"]["qualified"])  # 单站 120 < 200

        standings = svc.season_standings("s2026")
        # 两站积分各自按当站规则发放；a=100+120=220 达到 v2 跨站晋级线。
        totals = {r["competitor_id"]: r for row in standings["divisions"].values()
                  for r in row}
        self.assertEqual(totals["a"]["total"], 220)
        self.assertTrue(totals["a"]["qualification"]["qualified"])
        self.assertEqual(standings["station_rule_versions"], {"bj": "v1", "sh": "v2"})

    def test_replay_from_event_log_reproduces_identical_results(self):
        svc = build_service()
        seed_station(svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强", "b": "阿伟"})
        full_race(svc, "bj", "a", {"run": 300.0, "sled": 400.0, "gym": 200.0})
        full_race(svc, "bj", "b", {"run": 330.0, "sled": 430.0, "gym": 230.0})
        appeal = svc.open_appeal("b 申诉", station_id="bj", competitor_id="b")
        target = next(e for e in svc.store.all()
                      if e.type == "device_reading" and e.payload["competitor_id"] == "b"
                      and e.payload["discipline_id"] == "run")
        svc.close_appeal(appeal["appeal_id"], "amended",
                         corrections=[{"station_id": "bj", "competitor_id": "b",
                                       "kind": "amend_reading", "target_event_id": target.id,
                                       "new_value": 305.0}])
        snapshot = copy.deepcopy(svc.station_results("bj"))

        from domain.events import EventStore
        rebuilt_store = EventStore.from_list(svc.store.to_list())
        rebuilt = CompetitionService(store=rebuilt_store)  # 注册表与快照由账本重建
        self.assertEqual(rebuilt.station_results("bj"), snapshot)
        self.assertEqual(rebuilt.get_appeal_snapshot(appeal["appeal_id"])["standings"],
                         svc.get_appeal_snapshot(appeal["appeal_id"])["standings"])


class ViewsTest(unittest.TestCase):
    def test_public_view_hides_medical_and_identity_details(self):
        svc = build_service()
        seed_station(svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强", "b": "阿伟"})
        full_race(svc, "bj", "b", {"run": 305.0, "sled": 410.0, "gym": 210.0})
        svc.grant_medical_exemption("bj", "a", "MED-99", reason="心脏病史")
        internal = views.internal_station(svc.station_results("bj"))
        public = views.public_station(svc.station_results("bj"))

        pub_a = next(r for r in public["divisions"]["men"] if r["competitor_id"] == "a")
        int_a = next(r for r in internal["divisions"]["men"] if r["competitor_id"] == "a")
        self.assertNotIn("name", pub_a)
        self.assertNotIn("evidence", pub_a)
        self.assertNotIn("revision", pub_a)
        self.assertEqual(pub_a["status"], "EXEMPT")
        # 内部视图保留姓名、医疗证件号与证据事件号。
        self.assertEqual(int_a["name"], "阿强")
        self.assertEqual(int_a["evidence"]["exemption"]["medical_ref"], "MED-99")
        self.assertTrue(int_a["trace"]["evidence_event_ids"])


class ValidationTest(unittest.TestCase):
    def test_scheduling_without_effective_rules_rejected(self):
        svc = CompetitionService()
        rules_v = rules.make_default_rules(version="late", effective_from="2026-06-01",
                                           published_at="2026-05-01")
        svc.publish_rules(rules_v.to_dict())
        svc.create_season("s", "赛季")
        with self.assertRaises(DomainError):
            svc.schedule_station("early", "s", "三月站", "北京", "2026-03-01")

    def test_correction_must_reference_matching_evidence(self):
        svc = build_service()
        seed_station(svc, "bj", "北京", "2026-03-10T09:00:00+00:00",
                     {"a": "阿强"})
        full_race(svc, "bj", "a", {"run": 300.0, "sled": 400.0, "gym": 200.0})
        with self.assertRaises(NotFound):
            svc.apply_correction("bj", "a", "void_reading", "evt_missing", "无此证据")


if __name__ == "__main__":
    unittest.main()
