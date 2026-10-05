# 整改措施闭环

本项目维护整改措施闭环的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖机构合规员、执业人员、监管人员、复核专家，并明确整改措施依赖、证据版本核验、结案完整性门槛、期限提醒幂等等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/remediation/`：整改闭环后端（纯标准库，无第三方依赖）。
  - `models.py`：案件/要求/措施/证据/决定链模型，状态常量与契约对齐。
  - `service.py`：措施依赖校验、证据版本核验、结案完整性门槛、延期/替代/部分驳回/复开。
  - `reminders.py`：期限提醒任务，按确定键去重，可安全重跑。
  - `api.py`：HTTP JSON API；`store.py`：JSON 原子落盘存储。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/serve.py`：启动后端服务；`tools/run_reminders.py`：跑一轮期限提醒（适合 cron）。
- `tests/`：契约与后端回归测试。

## 后端流程

1. `POST /cases` 登记案件，把整改要求拆成带 `depends_on` 和 `deadline` 的措施（依赖成环、未知依赖会被拒绝）。
2. `POST /cases/{id}/issue` 下达检查决定（登记 → 待核验）。
3. `POST /cases/{id}/measures/{mid}/evidence` 分批提交证据，版本号递增，历史版本全保留。
4. `POST /cases/{id}/measures/{mid}/verify` 核验最新版本：通过 / 驳回 / 部分驳回（须给出驳回条目），记录核验人与复查结果；过期版本会被拒绝。
5. `POST .../extensions` 延期、`POST .../substitutions` 替代措施（下游依赖自动改指替代措施），全部写入决定链。
6. `POST /cases/{id}/close` 结案门槛：全部必需措施通过才放行，否则返回 409 与阻塞原因；`POST /cases/{id}/reopen` 可复开，决定链完整留痕。
7. `GET /cases/{id}` 展示每项要求的完成度与阻塞原因；`GET /cases/{id}/decisions` 查看决定链。
8. `POST /reminders/run` 或 `python3 tools/run_reminders.py` 生成临期/逾期提醒，重复运行不产生重复记录。

启动服务：`python3 tools/serve.py --port 8000 --data data/store.json`

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
