# 接口层与前端界面 (`interfaces_frontend.md`)

> **隶属主文档**：[main.md](./main.md)  
> **覆盖路径**：`cli_main.py`, `backend_main.py`, `src/interfaces/`, `src/cli_jobs.py`, `src/app.py`, `src/ui/`, `frontend/`

---

## 1. 模块职责

负责对外暴露统一的控制面入口，将 HTTP 请求、命令行指令和浏览器交互转换为对 `Application` 应用服务的调用；**不包含核心业务状态**，所有状态归属 `src/service/`。

---

## 2. 入口与文件清单

| 文件路径 | 职责说明 |
| :--- | :--- |
| `cli_main.py` | 公共 HTTP CLI 与 Linux 服务启动入口，转发至 `src.interfaces.cli.main`。 |
| `backend_main.py` | 独立后台服务启动入口（自动注入 `serve` 子命令）。 |
| `src/interfaces/http.py` | FastAPI 应用工厂 `create_app()`：生命周期 `lifespan`、同源校验 `check_origin`、异常处理、全部 `/api/v1` 路由、SSE `/api/v1/events` 与前端静态文件托管。 |
| `src/interfaces/schemas.py` | 公开响应模型（OpenAPI 与前端 `api.generated.ts` 的类型来源），包含 `AccountRecord`、`SessionRecord`、`EgressGroupRecord`、`TaskRecord`、`TaskSummary`、`TaskResults`、`LogEntry` 等。 |
| `src/interfaces/cli.py` | 基于 Typer 的 CLI 客户端；`serve` 启动 `uvicorn(..., workers=1)`，其余命令（`accounts` 等）通过 HTTP 调用运行中的后台服务。 |
| `src/cli_jobs.py` | Linux CLI 侧配置化任务与私有 Mihomo 进程监管工具（`status`/`stop` 等）。 |
| `src/app.py` | 传统交互式控制台菜单编排（保留的控制台入口能力）。 |
| `src/ui/runtime_log.py` | 后端与 CLI 共用的持久化运行诊断日志（`configure_runtime_logging`、`LoggerStream`）。 |
| `frontend/src/` | Vue 3 + TypeScript + Vite 前端项目，构建产物输出至 `frontend/dist`，由 FastAPI 直接托管。 |

---

## 3. HTTP API 路由总览 (`/api/v1`)

| 分组 | 路由 | 方法 | 作用 |
| :--- | :--- | :--- | :--- |
| **健康检查** | `/health` | GET | 服务存活探针 |
| **账号管理** | `/accounts`, `/accounts/{id}` | GET/POST/PATCH/DELETE | 账号增删改查 |
| **账号批量** | `/accounts/import`, `/accounts/assign-egress` | POST | 配置导入、出口分配 |
| **出口分组** | `/egress-groups`, `/egress-groups/{id}` | GET/POST/PUT/DELETE | 出口分组管理 |
| **出口运行态** | `/egress-groups/runtime`, `/egress-groups/rules`, `/egress-groups/{id}/probe` | GET/POST | Mihomo 状态、Clash 规则导出、出口公网 IP 探测 |
| **会话控制** | `/sessions`, `/sessions/{connect,reconnect,disconnect}` | GET/POST | 查询在线会话、提交会话变更任务（202） |
| **报文与脚本** | `/messages/parse`, `/messages/validate`, `/scripts` | POST/GET | `#send` 报文解析预检、可用脚本清单 |
| **任务与探测** | `/tasks`, `/tasks/validate`, `/tasks/{id}`, `/tasks/{id}/cancel`, `/tasks/{id}/results`, `/tasks/{id}/logs` | GET/POST/DELETE | 任务预检、提交（202）、查询、取消、结果与日志拉取 |
| **服务器探测** | `/server-probes/validate`, `/server-probes` | POST | 服务器可用性与错峰登录探测 |
| **实时事件** | `/events` | GET (SSE) | 全局流接收状态事件，`task_id` 流接收匹配任务的状态和日志，15 秒心跳保活 |

---

## 4. 前端核心结构 (`frontend/src/`)

- **核心模块**：`App.vue`（主布局与状态驱动）、`api.ts` + `api.generated.ts`（后端 OpenAPI 生成的强类型客户端）、`types.ts`、`presentation.ts`、`scriptParams.ts`、`accountPagination.ts`。
- **关键组件 (`frontend/src/components/`)**：
  - `AccountPagination.vue`：账号列表分页与批量选择
  - `EgressGroups.vue`：出口分组管理、探测与启停
  - `TaskComposer.vue` & `ScriptParameters.vue`：组合步骤与脚本参数表单编排
  - `TaskDetail.vue`：任务详情、单账号结果与实时日志查看

---

## 5. 维护注意点

- **同源校验**：非 `GET/HEAD/OPTIONS` 请求携带 `Origin` 头时必须与服务同源（`_same_origin`），否则返回 `403 origin_mismatch`。
- **敏感字段保护**：`RequestValidationError` 处理器故意裁剪了提交值（避免密码随 422 错误回显）。
- 账号和任务列表、日志分页、报文解析和规则生成的同步工作通过线程池运行。活动任务详情在事件循环上获取快照，脱敏、存储查询与 JSON 序列化交给线程池。账号状态或凭据有变化时，重新生成活动任务的响应。
- 任务详情 SSE 将已提交的日志批次合并为一次传输，每条日志仍有独立的 `data:` 帧和 `event: "log"` 字段。客户端可继续使用原有事件解析与 `after` 分页游标。
- 会话连接、重连和断开请求的 `concurrency` 上限为 500，实际同时连接数仍受运行时登录名额与出口容量限制。
- **前后端契约同步**：修改 `src/interfaces/schemas.py` 或 `src/service/contracts.py` 后，需同步检查并更新 `frontend/src/api.generated.ts` 及对应组件。
