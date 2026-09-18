# 综合体能赛资格与计分

服务于 HYROX 式综合体能赛事（跑步 / 负重 / 体操三大分项、多城分站）的规则版本、
成绩采集、跨站计分、申诉与奖金服务。核心原则：**事件不可变、规则按比赛发生时
绑定、更正只影响真正受影响的人**。

## 业务语义如何落地

| 需求 | 实现 |
| --- | --- |
| 缺席不能直接算零分 | 缺席分为 DNF（伤退/未完赛，保底分）、MED（医疗豁免，保底分）、DNS（未签到，零分）、DQ（取消资格，零分）；已批准补赛未到成绩前挂起 `PENDING_MAKEUP`，不进榜也不记零 |
| 异地补赛 | `MakeupApproved` 后可在任意地点提交 `MakeupResultRecorded`，成绩归属原站，按**原站比赛日绑定的规则版本**计分 |
| 规则带生效日期、禁止新规则重排旧站 | 规则以不可变版本发布（`published_at` + `effective_date`）；某站绑定版本要求发布日与生效日都不晚于比赛日。赛后补发"生效日写得很早"的版本也不能回溯任何已完成站；显式钉选晚生效版本直接报错 |
| 多设备同一动作去重 | 同一选手/动作、采集间隔 ≤ `dedupe_window_s` 的读数聚类：差值 ≤ `conflict_tolerance_s` 按设备优先级自动去重（保留全部原值，仅标记）；超阈值挂起 `ConflictFlagged`，**系统不静默选值**，裁决强制引用证据 |
| 采集资格/签到/判罚/医疗豁免 | 资格与签到决定参赛事实；裁判判罚产生罚时/取消资格（可被申诉撤销）；医疗豁免必须附医疗证明，公开榜单隐藏其全部细节 |
| 申诉冻结名次 | 申诉一提出，同站同组立即冻结：可预览但禁止发布正式快照、禁止官方更正；裁决（维持/成立）必须引用原始证据，成立时撤销判罚或认定成绩 |
| 更正只重算受影响者 | 每次发布生成不可变快照（指向上一版）。奖金以红冲（负数、回链原始分录）+ 补发表达；金额未变的选手**不产生任何分录** |
| 按当时规则复现 | 全部计算是事件投影的纯函数；快照记录 `rule_version_id`，跨站积分逐站取其绑定版本，赛季榜还暴露每站版本映射 |
| 公开/内部双视图 | 公开榜只有显示名/号码布/保底分事实（`MED` 对外显示 `EXEMPT`），内部视图含法定姓名、医疗原因与证据、设备读数、判罚与申诉编号、快照历史与奖金账本；`/events/<id>/diff` 从名次变化追到补赛/裁决/申诉/更正事件 |

## 架构

```
domain_models.py  不可变领域事件（唯一事实来源，含 EvidenceRef 证据引用）
store.py          仅追加事件日志 + 内存投影（可落盘 JSONL，重启重放）
rulebook.py       规则版本注册表与"比赛日 → 版本"绑定
admin.py          赛季/分站/规则/组别/选手/资格/签到
ingest.py         设备计时（去重/冲突）、判罚、豁免、伤退、补赛
appeals.py        申诉提出/裁决、官方更正（证据强制、冻结校验）
scoring.py        成绩解析、缺席分类、跑步/负重/体操分项名次、跨站积分、
                  晋级线、分站/赛季快照发布、红冲补发奖金账本
views.py          公开脱敏视图、内部追溯视图、快照版本差异归因
app.py            门面装配
service.py        HTTP 入口（/health 契约 + JSON API）
```

事件日志可通过 `--store data/events.jsonl` 落盘；不传则纯内存。

## 运行与测试

```bash
python3 service.py --check            # 基础配置检查
python3 service.py --port 8000        # 启动后访问 /health
npm test                              # 契约测试 + 14 个端到端场景测试
```

## 主要 HTTP 接口

- `POST /admin/seasons|rules|events|divisions|athletes|eligibility|check-in`
- `POST /ingest/readings|calls|exemptions|withdrawals|makeup-approvals|makeup-results|adjudications`
- `POST /appeals`、`POST /appeals/ruling`、`POST /corrections`
- `GET  /events/<id>/standings?division_id=&view=public|internal`
- `POST /events/<id>/standings/publish`（冻结 → 409；成绩挂起 → 409）
- `GET  /seasons/<id>/standings`、`POST /seasons/<id>/standings/publish`
- `GET  /events/<id>/diff`（内部：两版快照间名次变化与原因事件）
- `GET  /awards?scope=&owner_id=`（奖金账本：POSTED/REVERSAL/ADJUSTMENT 与净额）
- `GET  /conflicts`、`GET  /audit`（全量事件轨迹）

> `view=internal` 在真实部署中应由网关按角色鉴权；服务层只负责保证两套视图的数据边界。

`test_scenarios.py` 用"上海 → 北京 → 深圳 + v1/v2 两版规则"的完整叙事覆盖：
自动去重、冲突挂起与证据裁决、伤退保底分、申诉冻结与翻盘、仅 A1/A2 红冲补发、
医疗豁免脱敏、异地补赛、赛后版本禁止回溯、丢最差站的跨站积分与晋级线、
以及事件日志落盘重放结果一致。
