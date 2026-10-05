# 整改措施闭环

本项目维护整改措施闭环的领域约定、角色边界与样例数据，并提供可运行的 Python 后端（仅标准库，零第三方依赖）。当前契约覆盖机构合规员、执业人员、监管人员、复核专家，并明确整改措施依赖、证据版本核验、结案完整性门槛、期限提醒幂等等关键约束。

## 后端能力

检查决定下达后，案件登记时把整改要求拆成带**依赖关系和期限**的措施（人员停岗 / 设备整改 / 制度修订）：

- **证据版本核验**：机构分批提交证据，每次提交生成递增版本；核验人逐条复查，结论（通过 / 部分驳回 / 驳回）与驳回项留痕。
- **结案完整性门槛**：只有全部必需措施通过才能结案；可选措施不阻塞。结案被拦截时返回每项阻塞原因。
- **决定链**：延期批准/拒绝、替代措施采纳/否决、部分驳回、案件与措施复开、结案归档全部按序留痕。
- **期限提醒幂等**：提醒以 `(措施, 类型, 期限)` 为键去重，任务可安全重跑；延期后新期限自动生成新键。
- **进度 API**：每项整改要求给出完成度（必需措施通过比例）与具体阻塞原因（未启动、待核验、被驳回、依赖未过、已逾期、延期待审批）。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/remediation/`：整改闭环后端（`models` 领域模型、`service` 核心业务规则、`repository` 内存仓储、`api` 标准库 HTTP 层）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动后端服务（`--demo` 写入演示案件）。
- `tests/`：契约回归、领域规则与 HTTP 端到端测试。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 检查决定下达：登记案件并拆分要求与措施 |
| GET | `/cases` / `/cases/{id}` | 案件列表 / 详情（含证据版本与核验记录） |
| GET | `/cases/{id}/progress` | 每项要求的完成度与阻塞原因 |
| GET | `/cases/{id}/decisions` | 决定链 |
| POST | `/cases/{id}/close` | 结案（门槛不满足返回 409 与阻塞清单） |
| POST | `/cases/{id}/archive` / `/cases/{id}/reopen` | 归档 / 复开 |
| POST | `/measures/{id}/evidence` | 分批提交证据（生成新版本） |
| POST | `/measures/{id}/reviews` | 核验人逐条复查（通过 / 部分驳回 / 驳回） |
| POST | `/measures/{id}/extensions` → `/extensions/{id}/decision` | 延期申请与审批 |
| POST | `/measures/{id}/alternatives` → `/alternatives/{id}/decision` | 替代措施申请与审批 |
| POST | `/measures/{id}/reopen` | 复开已通过措施（连带复开案件） |
| POST | `/jobs/reminders` | 期限提醒扫描（幂等，可安全重跑） |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

启动服务：`python3 tools/run_server.py --port 8080 --demo`
