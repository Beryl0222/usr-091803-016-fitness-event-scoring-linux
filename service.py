"""综合体能赛资格与计分的运行入口与 HTTP 接口。

- GET  /health 稳定健康检查（契约）
- POST /admin/...               赛季、规则版本、分站、组别、选手、资格、签到
- POST /ingest/...              设备计时、判罚、豁免、伤退、补赛、冲突裁决
- POST /appeals, /appeals/rule, /corrections
- GET  /events/<id>/standings   榜单（?division_id=&view=public|internal）
- POST /events/<id>/standings/publish
- GET  /seasons/<id>/standings  跨站积分与晋级线
- POST /seasons/<id>/standings/publish
- GET  /events/<id>/diff        内部：名次版本差异追溯
- GET  /awards                  奖金账本
- GET  /audit                   内部：事件轨迹

内部视图在真实部署中须由网关做角色鉴权；此处用 view=internal 显式区分。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app import Application
from scoring import ResultsPending, StandingsFrozen

SERVICE_ID = "fitness-event-scoring"
SERVICE_NAME = "综合体能赛资格与计分"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_application(store_path: str | None = None) -> Application:
    return Application(store_path)


class Handler(BaseHTTPRequestHandler):
    app = build_application()

    # ---- 基础收发 ------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path
        app = self.app
        try:
            if path == "/health":
                self._send_json(200, health_payload())
            elif path.startswith("/events/") and path.endswith("/standings"):
                event_id = path.split("/")[2]
                self._require(query, "division_id")
                self._send_json(200, app.event_standings(
                    event_id, query["division_id"], query.get("view") == "internal"))
            elif path.startswith("/events/") and path.endswith("/diff"):
                self._send_json(200, app.event_diff(path.split("/")[2]))
            elif path.startswith("/seasons/") and path.endswith("/standings"):
                season_id = path.split("/")[2]
                self._require(query, "division_id")
                self._send_json(200, app.season_standings(
                    season_id, query["division_id"], query.get("view") == "internal"))
            elif path == "/conflicts":
                self._send_json(200, app.list_open_conflicts(query.get("event_id")))
            elif path == "/awards":
                self._send_json(200, app.award_ledger(query.get("scope"), query.get("owner_id")))
            elif path == "/audit":
                self._send_json(200, app.audit_trail())
            else:
                self.send_error(404)
        except (KeyError, LookupError) as error:
            self._send_json(404, {"error": "NOT_FOUND", "message": str(error)})
        except Exception as error:  # noqa: BLE001 - 兜底转 500，避免连接挂死
            self._send_json(500, {"error": "INTERNAL", "message": str(error)})

    def do_POST(self):
        path = urlparse(self.path).path
        app = self.app
        try:
            payload = self._read_json()
            routes = {
                "/admin/seasons": app.create_season,
                "/admin/rules": app.publish_rules,
                "/admin/events": app.schedule_event,
                "/admin/divisions": app.register_division,
                "/admin/athletes": app.register_athlete,
                "/admin/eligibility": app.grant_eligibility,
                "/admin/check-in": app.check_in,
                "/ingest/readings": app.record_reading,
                "/ingest/calls": app.record_call,
                "/ingest/exemptions": app.grant_exemption,
                "/ingest/withdrawals": app.record_withdrawal,
                "/ingest/makeup-approvals": app.approve_makeup,
                "/ingest/makeup-results": app.record_makeup_result,
                "/ingest/adjudications": app.adjudicate,
                "/appeals": app.file_appeal,
                "/appeals/ruling": app.rule_appeal,
                "/corrections": app.apply_correction,
            }
            if path in routes:
                self._send_json(200, routes[path](payload))
            elif path.startswith("/events/") and path.endswith("/standings/publish"):
                event_id = path.split("/")[2]
                self._require(payload, "division_id")
                self._send_json(200, app.publish_event_standings(event_id, payload["division_id"]))
            elif path.startswith("/seasons/") and path.endswith("/standings/publish"):
                season_id = path.split("/")[2]
                self._require(payload, "division_id")
                self._send_json(200, app.publish_season_standings(season_id, payload["division_id"]))
            else:
                self.send_error(404)
        except (StandingsFrozen, ResultsPending) as error:
            self._send_json(409, {"error": type(error).__name__, "message": str(error)})
        except (ValueError, TypeError) as error:
            self._send_json(422, {"error": "VALIDATION_ERROR", "message": str(error)})
        except (KeyError, LookupError) as error:
            self._send_json(404, {"error": "NOT_FOUND", "message": str(error)})
        except Exception as error:  # noqa: BLE001
            self._send_json(500, {"error": "INTERNAL", "message": str(error)})

    @staticmethod
    def _require(data: dict, key: str) -> None:
        if not data.get(key):
            raise ValueError(f"缺少必填参数: {key}")

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=None, help="事件日志 JSONL 路径；不填则仅内存")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        build_application(args.store)  # 确保装配（含事件重放）成功
        print("基础检查通过")
        return
    if args.store:
        Handler.app = build_application(args.store)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
