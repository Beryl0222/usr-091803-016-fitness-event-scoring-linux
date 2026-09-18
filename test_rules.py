"""规则版本与纯函数计分测试。"""

import unittest

from domain import rules


def make_rules(**overrides):
    data = rules.make_default_rules().to_dict()
    data.update(overrides)
    return rules.RuleSet.from_dict(data)


class RulesVersioningTest(unittest.TestCase):
    def test_effective_version_uses_latest_published_before_moment(self):
        registry = rules.RuleRegistry()
        registry.publish(make_rules(version="v1", effective_from="2026-01-01",
                                    published_at="2025-12-01"))
        registry.publish(make_rules(version="v2", effective_from="2026-04-01",
                                    published_at="2026-03-15"))
        self.assertEqual(registry.effective_version("2026-03-01").version, "v1")
        self.assertEqual(registry.effective_version("2026-04-15").version, "v2")

    def test_version_published_after_race_cannot_reorder_it(self):
        registry = rules.RuleRegistry()
        registry.publish(make_rules(version="v1", effective_from="2026-01-01",
                                    published_at="2025-12-01"))
        # 生效日虽然早，但发布时间晚于 3 月的比赛。
        registry.publish(make_rules(version="v-late", effective_from="2026-01-15",
                                    published_at="2026-06-01"))
        self.assertEqual(registry.effective_version("2026-03-01").version, "v1")

    def test_no_version_before_first_effective_date(self):
        registry = rules.RuleRegistry()
        registry.publish(make_rules(version="v1", effective_from="2026-01-01"))
        with self.assertRaises(LookupError):
            registry.effective_version("2025-12-31")

    def test_published_version_is_immutable(self):
        registry = rules.RuleRegistry()
        registry.publish(make_rules(version="v1"))
        with self.assertRaises(ValueError):
            registry.publish(make_rules(version="v1", dnf_points=99))


class ScoringFunctionsTest(unittest.TestCase):
    def setUp(self):
        self.rules = make_rules()

    def _mk(self, cid, status, value=None):
        return rules.Outcome(cid, "men", status, value=value)

    def test_ranking_points_prize_and_ties(self):
        outcomes = [
            self._mk("a", rules.STATUS_FINISHED, 1100),
            self._mk("b", rules.STATUS_FINISHED, 1100),
            self._mk("c", rules.STATUS_FINISHED, 1150),
        ]
        ranked = rules.rank_outcomes(outcomes)
        by_id = {r["competitor_id"]: r for r in ranked}
        self.assertEqual((by_id["a"]["rank"], by_id["b"]["rank"], by_id["c"]["rank"]),
                         (1, 1, 3))  # 标准竞赛排名
        points = rules.award_station_points(ranked, self.rules)
        self.assertEqual([points[c]["points"] for c in ("a", "b", "c")], [100, 100, 85])
        self.assertEqual(rules.prize_for_rank(1, "men", self.rules), 5000)
        self.assertEqual(rules.prize_for_rank(3, "men", self.rules), 1500)
        self.assertEqual(rules.prize_for_rank(4, "men", self.rules), 0)

    def test_dnf_ranks_after_finishers_dns_and_exempt_unranked(self):
        outcomes = [
            self._mk("a", rules.STATUS_FINISHED, 1100),
            rules.Outcome("b", "men", rules.STATUS_DNF, completed_disciplines=1),
            self._mk("c", rules.STATUS_DNS),
            self._mk("e", rules.STATUS_EXEMPT),
        ]
        ranked = rules.rank_outcomes(outcomes)
        by_id = {r["competitor_id"]: r for r in ranked}
        self.assertEqual(by_id["a"]["rank"], 1)
        self.assertEqual(by_id["b"]["rank"], 2)
        self.assertIsNone(by_id["c"]["rank"])
        self.assertIsNone(by_id["e"]["rank"])
        points = rules.award_station_points(ranked, self.rules)
        self.assertEqual(points["a"]["points"], 100)
        self.assertEqual(points["b"]["points"], 10)     # DNF 分
        self.assertEqual(points["c"]["points"], 0)      # 缺席零分
        self.assertEqual(points["e"]["points"], 100)    # 豁免=最后完赛者积分（仅 1 人完赛）

    def test_cross_station_drop_races_and_average_exemption(self):
        rs = make_rules(exempt_policy=rules.EXEMPT_AVERAGE, drop_races=1)
        station_points = {
            "s1": {"a": {"points": 100, "status": "FINISHED", "rank": 1, "value": 1,
                         "exempt_replaced": False},
                   "b": {"points": 92, "status": "FINISHED", "rank": 2, "value": 2,
                         "exempt_replaced": False}},
            "s2": {"a": {"points": 85, "status": "FINISHED", "rank": 3, "value": 3,
                         "exempt_replaced": False},
                   "b": {"points": None, "status": "EXEMPT", "rank": None, "value": None,
                         "exempt_replaced": True}},
            "s3": {"a": {"points": 92, "status": "FINISHED", "rank": 2, "value": 2,
                         "exempt_replaced": False},
                   "b": {"points": 100, "status": "FINISHED", "rank": 1, "value": 1,
                         "exempt_replaced": False}},
        }
        totals = rules.cross_station_totals(station_points, rs)
        # b 的豁免站按其他两站均值 96 补齐；两人均丢弃各自最低站。
        self.assertEqual(totals["b"]["stations"]["s2"], 96.0)
        self.assertEqual(totals["a"]["total"], 192)   # 85 被丢：100+92
        self.assertEqual(totals["b"]["total"], 196)   # 92 被丢：96+100
        ranked = {r["competitor_id"]: r for r in rules.rank_totals(totals)}
        self.assertEqual(ranked["a"]["rank"], 2)
        self.assertEqual(ranked["b"]["rank"], 1)

    def test_qualification_methods(self):
        q_rank = rules.qualification_status("men", 2, None, None, self.rules)
        self.assertTrue(q_rank["qualified"])
        rs_points = make_rules(qualification=[{"division_id": "men", "method": "points",
                                               "threshold": 200}])
        self.assertTrue(rules.qualification_status(
            "men", None, None, 210, rs_points)["qualified"])
        self.assertFalse(rules.qualification_status(
            "men", None, None, 190, rs_points)["qualified"])
        rs_time = make_rules(qualification=[{"division_id": "men", "method": "time",
                                             "threshold": 1200}])
        self.assertTrue(rules.qualification_status(
            "men", None, 1150, None, rs_time)["qualified"])

    def test_invalid_rules_rejected(self):
        with self.assertRaises(ValueError):
            make_rules(rank_points=[])
        with self.assertRaises(ValueError):
            make_rules(exempt_policy="bogus")
        with self.assertRaises(ValueError):
            make_rules(qualification=[{"division_id": "ghost", "method": "rank",
                                       "threshold": 1}])


if __name__ == "__main__":
    unittest.main()
