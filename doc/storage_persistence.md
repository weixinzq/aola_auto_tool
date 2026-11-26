# 数据持久化与迁移 (`storage_persistence.md`)

> **隶属主文档**：[main.md](./main.md)  
> **覆盖路径**：`src/service/store.py`、`src/service/log_writer.py`、`src/service/migrations/`

---

## 1. 模块职责

管理控制面所有持久化状态，基于本地 SQLite + SQLAlchemy Core + Alembic 迁移框架实现；**运行态实时会话与锁不存数据库**，仅存配置、任务记录、步骤明细与运行日志。

---

## 2. 数据库配置与迁移链

- **数据库路径**：`<data_dir>/service.sqlite3`（默认 `data/service/service.sqlite3`），非 Windows 平台目录权限 `0o700`、库文件权限 `0o600`。
- **连接配置（`Store.__init__`）**：
  - `check_same_thread=False`
  - 连接钩子设置 `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`temp_store=MEMORY` 和 `cache_size=-32000`。
- **Alembic 迁移脚本（`src/service/migrations/versions/`）**：
  1. `0001_initial.py`：初始化基础表结构
  2. `0002_egress_groups.py`：引入出口分组配置表
  3. `0003_ip_pool.py`：IP 池模式相关结构演进
  4. `0004_task_step_batches.py`：将海量任务步骤结果拆出至 `task_step_batches` 流式追加表

---

## 3. 核心表结构（`src/service/store.py`）

| 表名 | 主键 / 索引 | 核心字段 | 说明 |
| :--- | :--- | :--- | :--- |
| `accounts` | `account_id` (PK) | `config` (JSON) | 存储账号配置（含密码）；对外查询 `private=False` 时剥离 `password`。 |
| `egress_groups` | `group_id` (PK) | `config` (JSON) | 存储出口分组名称、`clash_policy`、`enabled`、`routing_confirmed` 等配置。 |
| `tasks` | `task_id` (PK) | `record` (JSON) | 存储任务全量快照（状态、时间戳、参数快照、`account_results`、`server_results`）。 |
| `task_step_batches` | `id` (PK) | `task_id`, `account_id`, `records` (JSON) | 流式追加保存各账号执行步骤结果；`get_task()` 时按 `id` 顺序拼回 `step_results`。 |
| `logs` | `id` (PK 自增), `idx(task_id)` | `task_id`, `account_id`, `timestamp`, `message` | 按任务和账号记录运行日志，支持 `after` 游标分页拉取（单次上限 1000 条）。 |

---

## 4. 凭据脱敏与查询设计

- `Store` 注册账号、密码及其 MD5 和 URL 编码值。父进程生成的日志在写线程脱敏，经 Worker 脱敏的日志可直接批量落库。
- `save_task()` 和 `append_task_steps_batch()` 对结构化数据脱敏。任务记录处理保留系统生成的 ID 和状态字段。
- `get_accounts_by_ids()` 用一次 `SELECT … IN` 读取任务账号，按请求顺序返回，缺失账号返回 404。出口配置在服务启动时读入内存。
- **任务摘要优化**：`list_task_summaries()` / `get_task_summary()` 直接通过 SQLite `json_extract` / `json_each` / `json_array_length` 在库内聚合摘要字段，避免列表页加载庞大 `step_results`。

---

## 5. 批量写入与留存

- `BatchLogWriter` 的单写线程优先处理 FIFO 控制队列中的步骤、任务快照、刷盘与关停指令。日志进入独立的 30,000 条队列，每 200 ms 或累计 500 条提交一批。
- 日志队列满时丢弃当前日志并累计 `dropped_count`，运行诊断每 5 秒汇总一次。步骤使用无界控制队列，入队即返回。
- SQLite 瞬态锁错误按 50、150、300 ms 退避重试。最终失败累计 `failed_count`，对受影响的保存或刷盘请求单独返回错误，后续写入继续处理。
- 日志批量提交后分配持久化 `id` 并触发 SSE 推送。运行中分页读取已提交数据，终态和显式刷盘等待请求前接收的日志，单次最多读取 1,000 条。
- 清理已完成任务前先刷完已入队日志，随后每笔事务删除最多 10,000 条日志，再删除任务快照和步骤。
- 环境变量 `AOLA_MAX_LOGS_PER_TASK` 默认 50,000，设置为 `0` 可关闭留存限制。每 50 个日志批次及关停时裁剪近期写入的任务，保留最新记录。

表结构变更继续通过 Alembic 迁移管理。
