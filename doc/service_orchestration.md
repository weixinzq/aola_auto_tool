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
| `src/service/application.py` | `Application` | 组合根（Composition Root）：按依赖顺序装配 `Store`、`EventHub`、`MessageService`、`ScriptRegistry`、`Mihomo`、`WorkerPool`、`SessionService`、`EgressService`、`AccountService`、`TaskService` 与 `InstanceLock`。 |
| `src/service/contracts.py` | `ServiceError`, `TaskSpec`, `ScriptTaskInput`, `AccountInput`, `EgressGroupInput` 等 | 控制面统一输入契约与错误模型（携带 `code`/`message`/`status`）。 |
| `src/service/accounts.py` | `AccountService` | 账号配置的增删改查、批量导入与出口绑定；删除/修改前检查 `SessionService.is_busy()`。 |
| `src/service/runtime.py` | `SessionService`, `_Session`, `Redactor`, `account_identity` | 管理在线会话映射 `_sessions`、账号独占声明 `_claims` / `_claim_accounts`、出口容量排队 `_waiting` 与登录并发信号量 `_login_slots`（默认 20）。 |
| `src/service/tasks.py` | `TaskService`, `_Run` | 任务预检 `validate()`、提交 `submit()`、并发调度 `_run()`、取消 `cancel()`、清理 `_finish()` 与 `0.2s` 状态防抖刷盘 `_publish()`。 |
| `src/service/events.py` | `EventHub`, `utc_now` | 内存发布/订阅中心，为每个订阅者维护 `asyncio.Queue(maxsize=256)`，队满时丢弃最旧事件以防内存泄漏。 |
| `src/service/execution.py` | `MessageService`, `ScriptRegistry`, `ScriptContext`, `ScriptSocket`, `ScriptMessages` | `#send` 报文纯预检与已注册脚本能力封装，为脚本提供隔离的安全上下文。 |
| `src/service/execution_runner.py` | `run_execution()` | 统一的执行与网络写入审计路径，兼容进程内会话与隔离 Worker 会话。 |
| `src/service/instance_lock.py` | `InstanceLock` | 基于 `data/service/service.lock` 的进程互斥锁，防止双实例同时读写同一目录。 |
| `src/service/migration.py` | `convert_legacy_job()` | 一次性转换旧版任务配置文件（不修改源文件）。 |

---

## 3. 核心状态机与协作流程

### 3.1 账号会话状态流转 (`SessionService`)
`offline` → `waiting_egress`（等待出口 IP 池名额）→ `connecting`（持有 `_login_slots` 发起连接）→ `initializing`（等待玩家初始化）→ `ready` → `disconnecting` → `offline`

- **账号互斥机制**：按归一化登录账号 `account_identity(account)` 维护 `_claims`，在 `SessionService.reserve()`（由 `self._lock` 保护）中原子校验并抢占，确保同一账号不会被两个任务同时操作。
- **容量唤醒机制**：`SessionService.wake()` 在事件循环上同步执行，从 `_waiting` FIFO 队列中按“当前在线数最少 + 最久未使用（LRU）”原则分配活跃出口分组（每公网 IP 上限 25 在线）。

### 3.2 任务生命周期 (`TaskService`)
`queued` → `running` → (`cancelling`) → `succeeded` / `failed` / `cancelled` / `interrupted`

- **支持任务类型 (`TaskSpec.kind`)**：`connect`、`reconnect`、`disconnect`、`execute`、`server_probe`。
- **会话策略 (`session_policy`)**：`existing`（仅复用已就绪会话，未就绪账号标为 `skipped`）、`temporary`（任务结束后自动断开）、`pooled`（连接后留在池中复用）。
- **执行模式 (`mode`)**：`bounded`（边连边跑，受任务内 `Semaphore(concurrency)` 限制）与 `all_online`（全部账号先上线就绪再统一并发执行）。
- **优雅关闭 (`Application.close`)**：按 `tasks.close()` → `sessions.shutdown()` → `workers.close()` → `mihomo.close()` → `store.close()` → `lock.release()` 严格逆序释放。

---

## 4. 维护注意点

- **日志链路性能**：当前 `TaskService._log()` 会同步执行 `run.redactor.clean()` 并同步写入 `Store.append_log()`，高并发下易成瓶颈；修改日志链路时需保持脱敏覆盖与 `TaskService.logs()` 的可见性。
- **读时刷盘**：`TaskService.get()` 当前会对活跃任务触发 `self._flush(run)`，若优化为内存直读，需保证 `step_results` 与终态落盘的一致性。
- **服务重启恢复**：`Application.start()` 会调用 `store.interrupt_pending()`，将上次异常退出遗留的 `queued`/`running`/`cancelling` 任务统一标记为 `interrupted`（不自动重放）。
