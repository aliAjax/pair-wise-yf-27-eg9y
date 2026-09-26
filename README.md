# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`reviewer2`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `POST /api/objects/{id}/handovers`：主张进入 `resolved_return` 后，工作人员登记交割单（受领人、交接日期、至少一份该藏品的内部证据），单据进入待确认。
- `POST /api/handovers/{id}/confirm`：由另一名审查员确认交割；确认后持有人一次性改为受领人并追加历史版本。重复或并发确认时，后到者直接拿到已有结果。
- `GET /api/objects/{id}/handovers`：工作人员和审查员查看交割单及关联证据。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。缺证据、交接日期早于主张提交日或藏品已返还时不能登记交割，登记失败不改变原持有人和主张状态；列表和公众详情只显示“已返还”，受领人、交割证据和审查说明仍不公开。
