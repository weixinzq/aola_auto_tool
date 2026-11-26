# 数据持久化与迁移 (`storage_persistence.md`)

> **隶属主文档**：[main.md](./main.md)  
> **覆盖路径**：`src/service/store.py`, `src/service/migrations/`

---

## 1. 模块职责

管理控制面所有持久化状态，基于本地 SQLite + SQLAlchemy Core + Alembic 迁移框架实现；**运行态实时会话与锁不存数据库**，仅存配置、任务记录、步骤明细与运行日志。

---

## 2. 数据库配置与迁移链

- **数据库路径**：`<data_dir>/service.sqlite3`（默认 `data/service/service.sqlite3`），非 Windows 平台目录权限 `0o700`、库文件权限 `0o600`。
- **连接配置（`Store.__init__`）**：
  - `check_same_thread=False`
  - 连接钩子自动设置 `PRAGMA journal_mode=WAL` 与 `PRAGMA busy_timeout=5000`
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

- **启动加载敏感词**：`Store.__init__` 遍历所有账号的 `account` 与 `password` 及其 MD5 加入 `self._secrets`，懒编译正则 `self._secret_pattern`。
- **脱敏落库**：`save_task()`、`append_task_steps()`、`append_log()` 落盘前均调用 `Store.redact()`，同时保留 `_GENERATED_FIELDS`（如 `task_id`、`account_id`、`state` 等系统字段）避免被单字符密码误伤。
- **任务摘要优化**：`list_task_summaries()` / `get_task_summary()` 直接通过 SQLite `json_extract` / `json_each` / `json_array_length` 在库内聚合摘要字段，避免列表页加载庞大 `step_results`。

---

## 5. 维护注意点

- **Schema 变更规范**：任何新增表或列必须通过新增 Alembic 迁移文件（`0005_*.py`）实现，禁止仅改 `store.py` 里的 `Table(...)` 定义而不写迁移。
- **高并发性能风险**：
  - `append_log()` 当前每次调用独立提交事务且默认 `synchronous=FULL`，高并发日志下必须改造为批量事务写入并启用 `PRAGMA synchronous=NORMAL`。
  - `get_egress_group()` 当前实现先调用了 `list_egress_groups()` 全表扫描再过滤，应改为 `WHERE group_id = ?`。
  - `logs` 表目前无自动裁剪/TTL，长期高频运行需注意磁盘体积膨胀与 `delete_tasks()` 清理耗时。
