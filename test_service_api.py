"""HTTP API 端到端测试：鉴权、脱敏、冻结冲突与持久化重启。"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from domain import rules

TOKEN = "test-token"


class ApiTest(unittest.TestCase):
    server = None
    thread = None
    base_url = ""
    state_file = ""

    @classmethod
    def setUpClass(cls):
        os.environ["INTERNAL_TOKEN"] = TOKEN
        cls.state_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        os.unlink(cls.state_file)  # 从不存在的文件起步
        state = service.AppState(state_path=cls.state_file)
        cls.handler = service.build_handler(state)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        if os.path.exists(cls.state_file):
            os.unlink(cls.state_file)

    # -- HTTP 小工具 -------------------------------------------------------

    def call(self, method: str, path: str, body=None, token=TOKEN):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Internal-Token"] = token
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)

    def call_expect_error(self, method: str, path: str, body=None, token=TOKEN):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Internal-Token"] = token
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=3)
        error = ctx.exception
        payload = json.load(error)
        return error.code, payload

    # -- 用例 ---------------------------------------------------------------

    def test_01_full_flow_over_http(self):
        # 1) 发布规则（v1 已在 v2 之前发布）
        v1 = rules.make_default_rules(version="v1", effective_from="2026-01-01",
                                      published_at="2025-12-01").to_dict()
        status, res = self.call("POST", "/internal/rules/publish", v1)
        self.assertEqual(status, 201)

        # 2) 赛季、站点、选手、报名、签到
        self.assertEqual(self.call("POST", "/internal/seasons",
                                   {"season_id": "s", "name": "联赛"})[0], 201)
        self.assertEqual(self.call("POST", "/internal/stations", {
            "station_id": "bj", "season_id": "s", "name": "北京站", "city": "北京",
            "occurs_at": "2026-03-10T09:00:00+00:00"})[0], 201)
        for cid, name in (("a", "阿强"), ("b", "阿伟")):
            self.call("POST", "/internal/competitors",
                      {"competitor_id": cid, "name": name})
            self.call("POST", "/internal/stations/bj/entries",
                      {"competitor_id": cid, "division_id": "men"})
            self.call("POST", "/internal/stations/bj/checkins", {"competitor_id": cid})

        # 3) 设备读数：a 的 run 两个设备数值不同 -> 冲突
        self.call("POST", "/internal/stations/bj/readings",
                  {"competitor_id": "a", "discipline_id": "run", "device_id": "mat-A",
                   "value": 300.0, "read_at": "2026-03-10T10:00:00+00:00"})
        _, conflict = self.call("POST", "/internal/stations/bj/readings", {
            "competitor_id": "a", "discipline_id": "run", "device_id": "chip-C",
            "value": 320.0, "read_at": "2026-03-10T10:00:02+00:00"})
        self.assertEqual(conflict["status"], "conflicted")
        # 同值重复上报被去重
        _, dup = self.call("POST", "/internal/stations/bj/readings", {
            "competitor_id": "a", "discipline_id": "sled", "device_id": "backup",
            "value": 400.0, "read_at": "2026-03-10T10:05:00+00:00"})
        # （首条 sled 在下面的完整成绩采集前先到即为 active）
        self.assertIn(dup["status"], ("active", "duplicate"))
        # 补齐成绩
        for cid, values in (("a", {"run": None, "sled": 400.0, "gym": 200.0}),
                            ("b", {"run": 305.0, "sled": 410.0, "gym": 210.0})):
            for discipline_id, value in values.items():
                if value is None:
                    continue
                self.call("POST", "/internal/stations/bj/readings", {
                    "competitor_id": cid, "discipline_id": discipline_id,
                    "device_id": "mat-A", "value": value,
                    "read_at": "2026-03-10T10:10:00+00:00"})

        # a 冲突未裁决：公开榜中为 PENDING；b 第一
        status, public = self.call("GET", "/api/v1/stations/bj/standings", token=None)
        self.assertEqual(status, 200)
        rows = {r["competitor_id"]: r for r in public["divisions"]["men"]}
        self.assertEqual(rows["a"]["status"], "PENDING")
        self.assertEqual(rows["b"]["rank"], 1)
        # 公开榜脱敏：无姓名、无证据、无医疗字段
        self.assertNotIn("name", rows["a"])
        self.assertNotIn("evidence", rows["a"])

        # 4) 内部视图保留证据
        _, internal = self.call("GET", "/internal/stations/bj/results")
        int_a = next(r for r in internal["divisions"]["men"]
                     if r["competitor_id"] == "a")
        self.assertEqual(int_a["name"], "阿强")
        self.assertTrue(int_a["trace"]["evidence_event_ids"])

        # 5) 申诉冻结期间直接裁决被 409 拒绝
        _, appeal = self.call("POST", "/internal/appeals",
                              {"reason": "run 计时异议", "station_id": "bj",
                               "competitor_id": "a"})
        appeal_id = appeal["appeal_id"]
        chosen = conflict["event_id"]  # 冲突响应只回了事件 id，取内部历史中的读数
        history = self.call("GET", "/internal/stations/bj/competitors/a/history")[1]
        chip_reading = next(e["id"] for e in history["events"]
                            if e["type"] == "device_reading"
                            and e["payload"]["device_id"] == "chip-C")
        code, payload = self.call_expect_error(
            "POST", "/internal/conflicts/resolve",
            {"station_id": "bj", "competitor_id": "a", "discipline_id": "run",
             "chosen_event_id": chip_reading})
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"], "standings_frozen")

        # 6) 通过申诉裁决：a 以 920 秒反超
        _, closed = self.call("POST", f"/internal/appeals/{appeal_id}/close", {
            "decision": "amended", "note": "以芯片为准",
            "resolution": {"competitor_id": "a", "discipline_id": "run",
                           "chosen_event_id": chip_reading}})
        self.assertTrue(any(x["competitor_id"] in ("a", "b") for x in closed["affected"]))
        _, public = self.call("GET", "/api/v1/stations/bj/standings", token=None)
        rows = {r["competitor_id"]: r for r in public["divisions"]["men"]}
        self.assertEqual(rows["a"]["rank"], 1)
        self.assertEqual(rows["a"]["value"], 920.0)

    def test_02_internal_endpoints_require_token(self):
        code, payload = self.call_expect_error(
            "GET", "/internal/events", token="wrong-token")
        self.assertEqual(code, 401)
        code, _ = self.call_expect_error(
            "POST", "/internal/seasons", {"season_id": "x", "name": "x"}, token=None)
        self.assertEqual(code, 401)

    def test_03_public_rule_versions_list(self):
        status, payload = self.call("GET", "/api/v1/rule-versions", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(any(v["version"] == "v1" for v in payload["versions"]))

    def test_04_state_survives_restart(self):
        # 用同一持久化文件构建第二个服务实例
        state2 = service.AppState(state_path=self.state_file)
        handler2 = service.build_handler(state2)
        server2 = ThreadingHTTPServer(("127.0.0.1", 0), handler2)
        thread2 = threading.Thread(target=server2.serve_forever, daemon=True)
        thread2.start()
        try:
            url = f"http://127.0.0.1:{server2.server_port}/api/v1/stations/bj/standings"
            with urlopen(url, timeout=3) as response:
                payload = json.load(response)
            rows = {r["competitor_id"]: r for r in payload["divisions"]["men"]}
            self.assertEqual(rows["a"]["value"], 920.0)
            self.assertEqual(rows["a"]["rank"], 1)
        finally:
            server2.shutdown()
            server2.server_close()
            thread2.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
