"""综合体能赛资格与计分服务入口。

提供：
- GET  /health                              健康检查（稳定身份）
- GET  /api/v1/rule-versions                已发布规则版本（公开元数据）
- GET  /api/v1/stations/<id>/standings      公开分站榜（脱敏）
- GET  /api/v1/seasons/<id>/standings       公开跨站总榜（脱敏）

内部接口（需请求头 X-Internal-Token）覆盖规则发布、报名签到、设备读数、
判罚、医疗豁免、异地补赛、冲突裁决、申诉与更正，见 service.py 内路由表。
状态以只追加事件账本持久化到 --state 指定的 JSON 文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from domain import events as events_mod
from domain.competition import (
    CompetitionService, DomainError, NotFound, FrozenStandingsError)
from domain import rules as rules_mod
from domain import views

SERVICE_ID = "fitness-event-scoring"
SERVICE_NAME = "综合体能赛资格与计分"
DEFAULT_INTERNAL_TOKEN = "dev-internal-token"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class AppState:
    """持有领域服务、写锁与持久化路径。"""

    def __init__(self, state_path: str | None = None):
        self.state_path = state_path
        self.lock = threading.RLock()
        store = events_mod.EventStore()
        if state_path and os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            store = events_mod.EventStore.from_list(data.get("events", []))
        self.service = CompetitionService(store=store)
        self.persist()

    def persist(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"events": self.service.store.to_list()}, fh,
                      ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)


def _publish_default_rules(state: AppState) -> None:
    """空账本时注入默认规则版本，便于本地联调。"""
    with state.lock:
        if not state.service.registry.all():
            state.service.registry.publish(rules_mod.make_default_rules())
            state.service.store.append(
                "rules_published",
                {"rule_set": rules_mod.make_default_rules().to_dict()},
                occurred_at=rules_mod.make_default_rules().published_at,
                actor="rules-committee")
            state.persist()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    state: AppState = None  # type: ignore[assignment]  # 由 main 注入到类属性

    @property
    def internal_token(self) -> str:
        return os.environ.get("INTERNAL_TOKEN", DEFAULT_INTERNAL_TOKEN)

    # -- 基础 HTTP 工具 ----------------------------------------------------

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    def _require_internal(self) -> None:
        token = self.headers.get("X-Internal-Token")
        if token != self.internal_token:
            raise _Unauthorized("内部接口需要正确的 X-Internal-Token 请求头")

    # -- GET ---------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/health":
                self._send_json(health_payload())
                return
            if path == "/api/v1/rule-versions":
                self._send_json({"versions": self.state.service.list_rule_versions()})
                return

            match = re.fullmatch(r"/api/v1/stations/([^/]+)/standings", path)
            if match:
                result = self.state.service.station_results(match.group(1))
                self._send_json(views.public_station(result))
                return
            match = re.fullmatch(r"/api/v1/seasons/([^/]+)/standings", path)
            if match:
                as_of = query.get("as_of", [None])[0]
                result = self.state.service.season_standings(match.group(1), as_of=as_of)
                self._send_json(views.public_season(result))
                return

            if self._try_internal_get(path):
                return
            self.send_error(404)
        except _Unauthorized as exc:
            self._send_json({"error": "unauthorized", "message": str(exc)}, 401)
        except NotFound as exc:
            self._send_json({"error": "not_found", "message": str(exc)}, 404)
        except DomainError as exc:
            self._send_json({"error": "domain_error", "message": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - 防御性
            self._send_json({"error": "internal_error", "message": str(exc)}, 500)

    def _try_internal_get(self, path: str) -> bool:
        internal = path.startswith("/internal/")
        if not internal:
            return False
        self._require_internal()
        service = self.state.service

        match = re.fullmatch(r"/internal/stations/([^/]+)/results", path)
        if match:
            self._send_json(views.internal_station(service.station_results(match.group(1))))
            return True
        match = re.fullmatch(r"/internal/seasons/([^/]+)/standings", path)
        if match:
            self._send_json(views.internal_season(service.season_standings(match.group(1))))
            return True
        match = re.fullmatch(r"/internal/appeals/([^/]+)/snapshot", path)
        if match:
            self._send_json(service.get_appeal_snapshot(match.group(1)))
            return True
        match = re.fullmatch(
            r"/internal/stations/([^/]+)/competitors/([^/]+)/history", path)
        if match:
            self._send_json(service.history(match.group(1), match.group(2)))
            return True
        match = re.fullmatch(r"/internal/evidence/([^/]+)/chain", path)
        if match:
            self._send_json({"chain": service.evidence_chain(match.group(1))})
            return True
        if path == "/internal/events":
            self._send_json({"events": service.store.to_list()})
            return True
        raise NotFound(f"未知内部路由: {path}")

    # -- POST --------------------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            self._require_internal()
            body = self._read_body()
            result, status = self._route_post(path, body)
            self.state.persist()
            self._send_json(result, status)
        except _Unauthorized as exc:
            self._send_json({"error": "unauthorized", "message": str(exc)}, 401)
        except NotFound as exc:
            self._send_json({"error": "not_found", "message": str(exc)}, 404)
        except FrozenStandingsError as exc:
            self._send_json({"error": "standings_frozen", "message": str(exc)}, 409)
        except DomainError as exc:
            self._send_json({"error": "domain_error", "message": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - 防御性
            self._send_json({"error": "internal_error", "message": str(exc)}, 500)

    def _route_post(self, path: str, body: dict):
        service = self.state.service

        if path == "/internal/rules/publish":
            return service.publish_rules(body), 201
        if path == "/internal/seasons":
            return service.create_season(body["season_id"], body["name"]), 201
        if path == "/internal/stations":
            return service.schedule_station(
                body["station_id"], body["season_id"], body["name"], body["city"],
                body["occurs_at"], body.get("rule_version")), 201
        if path == "/internal/competitors":
            return service.register_competitor(
                body["competitor_id"], body["name"], body.get("season_id")), 201

        match = re.fullmatch(r"/internal/stations/([^/]+)/entries", path)
        if match:
            return service.enter_station(
                match.group(1), body["competitor_id"], body["division_id"]), 201
        match = re.fullmatch(r"/internal/stations/([^/]+)/checkins", path)
        if match:
            return service.check_in(
                match.group(1), body["competitor_id"], body.get("at")), 201
        match = re.fullmatch(r"/internal/stations/([^/]+)/readings", path)
        if match:
            return service.record_reading(
                match.group(1), body["competitor_id"], body["discipline_id"],
                body["device_id"], float(body["value"]),
                read_at=body.get("read_at"), attempt_id=body.get("attempt_id")), 201
        match = re.fullmatch(r"/internal/stations/([^/]+)/penalties", path)
        if match:
            return service.record_penalty(
                match.group(1), body["competitor_id"], body["discipline_id"],
                body["judge_id"], body.get("reason", ""),
                seconds=float(body.get("seconds", 0)),
                reps=float(body.get("reps", 0))), 201
        match = re.fullmatch(r"/internal/stations/([^/]+)/exemptions", path)
        if match:
            return service.grant_medical_exemption(
                match.group(1), body["competitor_id"], body["medical_ref"],
                reason=body.get("reason", ""), scope=body.get("scope", "station"),
                discipline_id=body.get("discipline_id"), at=body.get("at")), 201

        if path == "/internal/makeups":
            return service.record_makeup(
                body["source_station_id"], body["host_station_id"],
                body["competitor_id"], body["discipline_values"],
                judge_id=body.get("judge_id", "makeup"),
                recorded_at=body.get("recorded_at")), 201
        if path == "/internal/conflicts/resolve":
            return service.resolve_reading_conflict(
                body["station_id"], body["competitor_id"], body["discipline_id"],
                body["chosen_event_id"], note=body.get("note", ""),
                appeal_id=body.get("appeal_id")), 200
        if path == "/internal/appeals":
            return service.open_appeal(
                body["reason"], station_id=body.get("station_id"),
                competitor_id=body.get("competitor_id")), 201
        match = re.fullmatch(r"/internal/appeals/([^/]+)/close", path)
        if match:
            return service.close_appeal(
                match.group(1), body["decision"], note=body.get("note", ""),
                resolution=body.get("resolution"),
                corrections=body.get("corrections")), 200
        if path == "/internal/corrections":
            return service.apply_correction(
                body["station_id"], body["competitor_id"], body["kind"],
                body["target_event_id"], body["reason"],
                new_value=(float(body["new_value"]) if body.get("new_value") is not None else None),
                appeal_id=body.get("appeal_id")), 200

        raise NotFound(f"未知路由: {path}")

    def log_message(self, *_args):
        return


class _Unauthorized(Exception):
    pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def build_handler(state: AppState) -> type[Handler]:
    """把共享状态绑定到每个请求使用的 Handler 类。"""
    Handler.state = state
    return Handler


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--state", default=os.environ.get("STATE_FILE"),
                        help="事件账本持久化文件（JSON）")
    parser.add_argument("--seed-default-rules", action="store_true",
                        help="空账本时写入默认规则版本，便于联调")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        rules_mod.make_default_rules()  # 校验默认规则可构造
        print("基础检查通过")
        return

    state = AppState(state_path=args.state)
    if args.seed_default_rules:
        _publish_default_rules(state)
    handler_cls = build_handler(state)
    print(f"{SERVICE_NAME} 监听 0.0.0.0:{args.port}"
          f"（内部令牌{'已配置' if args.state else '默认'}）")
    ThreadingHTTPServer(("0.0.0.0", args.port), handler_cls).serve_forever()


if __name__ == "__main__":
    main()
