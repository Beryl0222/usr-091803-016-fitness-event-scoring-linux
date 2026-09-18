# 综合体能赛资格与计分

服务于 HYROX 类综合体能赛事（跑步 / 负重 / 体操三大分项）快速跨城扩展后的
**规则版本化、成绩采集与申诉裁决**。核心原则：

- **规则预先发布、按生效日期解析**：组别、分项、积分、晋级线、奖金与医疗豁免
  政策组成不可变的规则版本；每一站永远按**比赛发生时已生效且已发布**的版本
  复现，赛后发布的新版本不会重排旧站奖金与名次。
- **事实只追加、更正留痕**：报名、签到、设备计时、裁判判罚、医疗豁免、补赛、
  申诉、裁决与更正都是事件账本中的事件。更正必须引用原始证据事件号
  （`causation_id`），原值不被覆盖，可随时重放复现任意时刻状态。
- **多设备先去重、冲突保留原值**：同一动作（同选手/分项/试次）多设备同值读数
  自动去重；数值不一致且无法自动判断时，双方原值都保留为 `conflicted`，该
  分项进入 `PENDING` 不进榜，等待裁判长裁决。
- **申诉即冻结**：申诉开始时对相关名次留快照并冻结；冻结期间任何绕过裁决通道
  的直接改动返回 `409 standings_frozen`。裁决可附带读数选择与更正，系统只报告
  **真正受影响的人**（名次/状态/成绩/积分发生变化者）。
- **缺席不再被简单算零**：未签到且无豁免 = `DNS`（零分）；伤退未完赛 = `DNF`
  （规则版本规定的 DNF 分）；整站医疗豁免按版本政策计分（零分 / DNF 分 /
  最后完赛者积分 / 跨站均值）；分项豁免用同组该分项中位数替代；异地补赛成绩
  计入缺席的源站，举办地仅作证据。
- **公开 / 内部双视图**：公开榜单隐藏姓名、医疗证件号与原因、设备证据与修订号；
  内部视图保留证据事件号、规则版本、修订次数，可从一次排名变化沿因果链追到
  补赛、判罚、裁决和当时发布的规则版本。

## 运行

```bash
python3 service.py --check                    # 基础配置检查
python3 service.py --port 8000 \
    --state ./data/events.json --seed-default-rules
INTERNAL_TOKEN=xxxx python3 service.py ...    # 内部接口令牌（默认 dev-internal-token）
npm test                                      # 运行全部测试
```

启动后 `GET /health` 返回稳定服务身份；事件账本以 JSON 原子写入 `--state`
指定的文件，重启后规则注册表、申诉快照与全部成绩从账本重建。

## 模块

| 文件 | 职责 |
| --- | --- |
| `domain/events.py` | 只追加事件账本（线程安全、可序列化、可按时间重放） |
| `domain/rules.py` | 不可变规则版本 `RuleSet`、生效版本解析、名次/积分/奖金/晋级纯函数 |
| `domain/competition.py` | 比赛投影引擎：采集、去重、豁免、补赛、冻结、裁决、更正、增量重算 |
| `domain/views.py` | 公开脱敏视图与内部证据视图 |
| `service.py` | HTTP 入口与路由（内部接口需 `X-Internal-Token`） |

## HTTP 接口

公开（无令牌）：

- `GET /health`
- `GET /api/v1/rule-versions` — 已发布版本与生效日期
- `GET /api/v1/stations/<id>/standings` — 公开分站榜（脱敏）
- `GET /api/v1/seasons/<id>/standings?as_of=...` — 公开跨站总榜（脱敏）

内部（请求头 `X-Internal-Token`）：

- `POST /internal/rules/publish`
- `POST /internal/seasons`、`POST /internal/stations`、`POST /internal/competitors`
- `POST /internal/stations/<id>/entries`、`.../checkins`
- `POST /internal/stations/<id>/readings`（设备计时，自动去重/冲突标记）
- `POST /internal/stations/<id>/penalties`（裁判判罚）
- `POST /internal/stations/<id>/exemptions`（医疗豁免：`scope=station|discipline`）
- `POST /internal/makeups`（异地补赛：`source_station_id` 计分、`host_station_id` 举办）
- `POST /internal/conflicts/resolve`（读数冲突裁决；冻结期须随申诉决定提交）
- `POST /internal/appeals`、`POST /internal/appeals/<id>/close`
  （裁决可带 `resolution` 与 `corrections`，响应给出 `affected` 名单）
- `POST /internal/corrections`（`void_reading|amend_reading|void_penalty|amend_penalty`，
  必须带 `target_event_id` 引用原始证据）
- `GET /internal/stations/<id>/results`、`GET /internal/seasons/<id>/standings`
- `GET /internal/stations/<id>/competitors/<cid>/history`
- `GET /internal/evidence/<event_id>/chain`（沿因果链追溯到原始证据）
- `GET /internal/appeals/<id>/snapshot`、`GET /internal/events`

## 名次与计分口径

- 完赛者按总成绩（time 取小、reps 取多，含罚时/扣次）排序，并列采用标准竞赛
  排名（1,1,3）；DNF 排在完赛者之后（按完成分项数）；DNS/EXEMPT/PENDING 不占位。
- 单站积分、DNF/DNS 分、豁免政策、可丢弃最差站数、晋级线（名次/累计积分/
  达标成绩）与按名次发放的奖金全部来自**该站规则版本**；跨站汇总采用查询时刻
  生效版本的汇总策略，每站积分仍按当站版本发放（响应中带 `station_rule_versions`）。
- 每名选手带确定性的 `revision`（经补赛/裁决/更正改变的次数），重放事件流即可
  复现，不依赖内存状态。

## 测试

- `test_rules.py` — 版本生效解析、不可变性、并列名次、积分/奖金/晋级、豁免与丢站
- `test_competition.py` — 端到端：去重、冲突保留与裁决、DNS/豁免/中位数替代、
  异地补赛、申诉冻结快照、更正只动受影响者、旧站按 v1 复现而新站按 v2、
  事件流重放结果一致、公开视图脱敏
- `test_service_api.py` — HTTP 全流程：令牌鉴权、401/409、脱敏榜单、重启持久化
- `service_contract.py` — 原有健康检查契约
