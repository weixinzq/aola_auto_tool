# 后台服务与任务编排 (`service_orchestration.md`)

> **隶属主文档**：[main.md](./main.md)  
> **覆盖路径**：`src/service/application.py`, `accounts.py`, `contracts.py`, `events.py`, `execution.py`, `execution_runner.py`, `instance_lock.py`, `migration.py`, `runtime.py`, `tasks.py`

---

## 1. 模块职责

构成控制面的核心业务层，与具体传输协议（HTTP/CLI）解耦，负责：
- 服务装配与优雅启停（`Application` + `InstanceLock`）
- 账号配置管理（`AccountService`）
- 实时会话状态机、账号独占权与凭据脱敏（`SessionService` + `Redactor`）
- 异步后台任务编排、并发控制、取消清理与状态广播（`TaskService` + `EventHub`）

---

## 2. 核心文件与类清单

| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `src/service/application.py` | `Application` | 装配 `Store`、`EventHub`、`BatchLogWriter`、消息与脚本注册表、代理与 Worker、账号、会话和任务服务，并管理进程锁。 |
| `src/service/contracts.py` | `ServiceError`, `TaskSpec`, `ScriptTaskInput`, `AccountInput`, `EgressGroupInput` 等 | 控制面统一输入契约与错误模型（携带 `code`/`message`/`status`）。 |
| `src/service/accounts.py` | `AccountService` | 账号配置的增删改查、批量导入与出口绑定；删除/修改前检查 `SessionService.is_busy()`。 |
| `src/service/runtime.py` | `SessionService`, `_Session`, `Redactor`, `account_identity` | 管理在线会话映射 `_sessions`、账号独占声明 `_claims` / `_claim_accounts`、出口容量排队 `_waiting` 与登录并发信号量 `_login_slots`（默认 20）。 |
| `src/service/tasks.py` | `TaskService`, `_Run` | 任务预检 `validate()`、提交 `submit()`、并发调度 `_run()`、取消 `cancel()`、清理 `_finish()` 与 `0.2s` 状态防抖刷盘 `_publish()`。 |
| `src/service/events.py` | `EventHub`, `utc_now` | 为每个订阅者维护默认 1,024 项的队列，在入队前按任务和日志订阅范围筛选。日志可按批入队，队满时先拒绝新日志。 |
| `src/service/execution.py` | `MessageService`, `ScriptRegistry`, `ScriptContext`, `ScriptSocket`, `ScriptMessages` | `#send` 报文纯预检与已注册脚本能力封装，为脚本提供隔离的安全上下文。 |
| `src/service/execution_runner.py` | `run_execution()` | 统一的执行与网络写入审计路径，兼容进程内会话与隔离 Worker 会话。 |
| `src/service/instance_lock.py` | `InstanceLock` | 基于 `data/service/service.lock` 的进程互斥锁，防止双实例同时读写同一目录。 |
| `src/service/migration.py` | `convert_legacy_job()` | 一次性转换旧版任务配置文件（不修改源文件）。 |

---

## 3. 核心状态机与协作流程

### 3.1 账号会话状态流转 (`SessionService`)
`offline` → `waiting_egress`（等待出口 IP 池名额）→ `connecting`（等待并取得 `_login_slots` 执行连接握手）→ `initializing`（释放登录名额后等待玩家初始化）→ `ready` → `disconnecting` → `offline`

- **账号互斥机制**：按归一化登录账号 `account_identity(account)` 维护 `_claims`，在 `SessionService.reserve()`（由 `self._lock` 保护）中原子校验并抢占，确保同一账号不会被两个任务同时操作。
- **容量唤醒机制**：`SessionService.wake()` 在事件循环上同步执行，从 `_waiting` FIFO 队列中按“当前在线数最少 + 最久未使用（LRU）”原则分配活跃出口分组（每公网 IP 上限 25 在线）。

### 3.2 任务生命周期 (`TaskService`)
`queued` → `running` → (`cancelling`) → `succeeded` / `failed` / `cancelled` / `interrupted`

- **支持任务类型 (`TaskSpec.kind`)**：`connect`、`reconnect`、`disconnect`、`execute`、`server_probe`。
- **会话策略 (`session_policy`)**：`existing`（仅复用已就绪会话，未就绪账号标为 `skipped`）、`temporary`（任务结束后自动断开）、`pooled`（连接后留在池中复用）。
- **执行模式 (`mode`)**：`bounded`（边连边跑，受任务内 `Semaphore(concurrency)` 限制）与 `all_online`（全部账号先上线就绪再统一并发执行）。
- `Application.close()` 依次关闭任务、会话、执行进程和 Mihomo，等待写线程清空已接收数据，再关闭 SQLite 并释放实例锁。

---

## 4. 维护注意点

- `TaskService._log()` 把运行日志交给 `BatchLogWriter`。子进程日志携带内部脱敏标志，父进程日志由写线程使用全库凭据脱敏。
- 活动任务详情从内存快照读取。已完成账号的流式步骤在拼接前等待已入队批次提交。中间任务快照以 200 ms 防抖频率落盘，任务提交与终态等待各自快照写入完成。
- **服务重启恢复**：`Application.start()` 会调用 `store.interrupt_pending()`，将上次异常退出遗留的 `queued`/`running`/`cancelling` 任务统一标记为 `interrupted`（不自动重放）。
