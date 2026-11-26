# aola_auto_tool 项目架构主文档

> **文档路径**：`./doc/main.md`  
> **维护基线**：300 会话并发改造
> **适用对象**：项目维护者、协作者与编码 Agent

---

## 1. 文档维护规范（每次完成任务必读）

为了保证架构文档与代码长期同步，**每次完成功能开发、重构、Bug 修复或配置变更后，必须执行以下两步更新**：

1. **更新主文档 (`doc/main.md`)**：
   - 若新增/删除/重命名顶层模块、改变核心数据流、调整并发/容量约束或新增外部依赖，更新对应章节与「模块导航索引」。
   - 在本文档末尾的「架构变更日志」中追加一条简明记录（日期、变更概要、受影响子文档）。
2. **更新对应子文档 (`doc/*.md`)**：
   - 根据下方「模块导航索引」定位本次改动涉及的所有子文档。
   - 同步更新子文档中的文件清单、核心类/函数职责、接口契约、状态机、数据库表结构或已知约束。

> **给编码 Agent 的约定**：在提交代码变更前，先读取 `doc/main.md` 的「模块导航索引」，定位并读取受影响的子文档；完成代码修改与测试后，先更新对应子文档，再更新 `doc/main.md` 的变更日志与受影响章节。

---

## 2. 项目定位与整体架构

`aola_auto_tool` 是一个面向奥拉星（H5/Flash 协议）的**独立 Python 后台自动化服务**，统一管理多账号会话、代理出口池、协议通信与脚本任务。CLI、Vue 浏览器前端与桌面工作台（WebView2）共用同一套 `/api/v1` HTTP 接口；关闭客户端后，任务继续在后台运行。

### 2.1 分层架构概览

```text
+-------------------------------------------------------------------+
|  客户端层：Vue 前端 (frontend/) | CLI (cli_main.py) | 桌面工作台     |
+-------------------------------------------------------------------+
                         |  HTTP /api/v1 + SSE /api/v1/events
                         v
+-------------------------------------------------------------------+
|  控制面（单进程 FastAPI + asyncio，uvicorn workers=1）              |
|  - HTTP/CLI 适配 (src/interfaces/)                                 |
|  - 应用装配根 Application (src/service/application.py)             |
|  - 账号/会话/任务/出口编排 (src/service/{accounts,runtime,tasks,egress}) |
|  - 持久化 Store、BatchLogWriter (SQLite WAL) 与 EventHub           |
|  - Mihomo 代理托管 (src/service/mihomo.py)                         |
+-------------------------------------------------------------------+
                         |  Loopback TCP (换行分隔 JSON-RPC)
                         v
+-------------------------------------------------------------------+
|  数据面（按出口分组隔离的多子进程 WorkerHost，每 IP 上限 25 在线）    |
|  - WorkerHost (src/service/worker_host.py)                         |
|  - 账号认证与会话 (src/accounts/)                                   |
|  - TCP 长连接与消息路由 (src/network/)                              |
|  - 二进制协议/加密/AMF3 C++扩展 (src/protocol/)                     |
|  - #send 报文解析与分发 (src/messaging/)                            |
|  - 脚本组合器 & 游戏能力 (src/scripting/, src/game/, scripts/)      |
+-------------------------------------------------------------------+
```

### 2.2 核心设计原则

- **控制面与数据面物理隔离**：每个出口分组（`egress_group`）运行独立 `WorkerHost` 子进程，避免单进程 GIL 争用，实现代理路由与故障域隔离。
- **单进程权威状态**：运行态会话（`_Session`）与账号独占锁（`_claims`）仅存于控制面内存，不落库；持久化配置、任务结果与日志存入本地 SQLite（`data/service/service.sqlite3`）。
- **纯异步网络 + 协议加速**：登录（`aiohttp` + `aiohttp_socks`）与游戏 Socket（`asyncio`）均为异步 I/O，AMF3 编解码内置 C++ 扩展（`_amf3_native.cpp`）。
- **凭据全程脱敏**：`Redactor` 与 `Store.redact` 在日志、错误与任务快照输出前屏蔽密码、账号、MD5、`sid`、`token` 等敏感信息。

---

## 3. 模块导航索引（代码路径 ↔ 子文档映射）

当修改下表左侧任意代码路径时，请同步查阅并更新右侧对应的子文档：

| 架构域 | 覆盖的代码路径 | 对应子文档 | 核心职责摘要 |
| :--- | :--- | :--- | :--- |
| **接口层与前端** | `cli_main.py`, `backend_main.py`, `src/interfaces/`, `src/cli_jobs.py`, `src/app.py`, `src/ui/`, `frontend/` | [interfaces_frontend.md](./interfaces_frontend.md) | HTTP `/api/v1` 路由、SSE 推送、Pydantic Schema、CLI 命令、Vue 前端与日志诊断 |
| **后台服务与编排** | `src/service/application.py`, `accounts.py`, `contracts.py`, `events.py`, `execution.py`, `execution_runner.py`, `instance_lock.py`, `migration.py`, `runtime.py`, `tasks.py` | [service_orchestration.md](./service_orchestration.md) | `Application` 装配、会话生命周期与账号互斥锁、任务状态机、步骤执行与事件分发 |
| **出口池与多进程 Worker** | `src/service/egress.py`, `mihomo.py`, `mihomo_host.py`, `workers.py`, `worker_host.py`, `worker_process.py`, `worker_transport.py`, `src/network/proxy.py` | [workers_egress.md](./workers_egress.md) | IP 池名额调度（25/IP）、Mihomo 节点托管、子进程生命周期绑定与 Loopback RPC 协议 |
| **存储与数据迁移** | `src/service/store.py`, `src/service/log_writer.py`, `src/service/migrations/` | [storage_persistence.md](./storage_persistence.md) | SQLite WAL、批量写入与日志留存、5 张核心表结构、Alembic 迁移链（`0001`–`0004`）与脱敏落库 |
| **账号、网络与协议** | `src/accounts/`, `src/network/`, `src/protocol/`, `src/messaging/`, `src/config.py` | [network_protocol.md](./network_protocol.md) | HTTP 登录与风控识别、TCP `GameSocket`、`MessageRouter`、AMF3/XOR 协议、`#send` 分发与服务器探测 |
| **脚本引擎与游戏业务** | `src/scripting/`, `src/game/`, `scripts/` | [scripting_game.md](./scripting_game.md) | 脚本动态发现、步骤组合执行、战斗状态机、道具/宠物/OCR 能力及内置专用脚本 |

---

## 4. 关键运行约束与并发验收

- 控制面由 `cli_main.py serve` 以 `uvicorn(..., workers=1)` 启动，`InstanceLock` 保证同一数据目录只有一个服务实例。
- 每个出口 IP 最多承载 25 个在线会话。全局登录信号量默认为 20，连接握手完成便释放该名额。HTTP 单次会话操作允许指定最高 500 的并发度。
- SQLite 单写线程优先处理任务快照与步骤，日志走独立有界队列。控制指令在当前事务结束后取得优先权。单批锁错误按限次退避重试，最终失败只影响该批。
- 出口调度使用配置缓存，提交与预检批量读取账号。活动日志轮询读取已落盘的数据，任务详情 SSE 继续推送每批提交的日志。
- 在项目根目录运行 `python -m unittest tests.test_api_cli tests.test_300_concurrency -v` 验证控制队列、锁错误恢复、脱敏、关停、日志查询和 IPC。运行 `python -m tests.benchmark_300_concurrency` 执行含日志分页轮询的 30 秒本机回环压测。
- 本机回环压测使用 12 组、300 个模拟会话，独立客户端进程并发轮询健康检查、任务详情与日志分页，并保持两个 SSE 连接。30 秒内 45,000 条日志全部落盘并送达任务详情 SSE，全局 SSE 收到 0 条日志。健康检查 P99 为 42.04 ms，任务详情为 44.92 ms，日志分页为 54.03 ms。写入失败和日志丢弃均为 0，日志队列采样峰值为 600 条。

---

## 5. 架构变更日志（Changelog）

| 日期 | 变更概要 | 涉及子文档 |
| :--- | :--- | :--- |
| 2026-11-26 | 建立初始架构文档体系（`doc/main.md` 及 6 份模块子文档），完成全仓库模块盘点与高并发瓶颈基线记录。 | 全部子文档 |
| 2026-11-26 | 完成 300 会话并发改造，加入 SQLite 批量写入和留存、按来源脱敏、任务快照异步持久化、Worker IPC 与 SSE 合批，并调整会话操作并发上限。 | [服务编排](./service_orchestration.md)、[存储](./storage_persistence.md)、[Worker 与出口](./workers_egress.md)、[HTTP 与前端](./interfaces_frontend.md) |
| 2026-11-26 | 将旧 API 测试迁移到当前 HTTP 和 CLI 契约。14 项 API 用例及另外 58 项并发、存储和 Worker 回归通过。 | [HTTP 与前端](./interfaces_frontend.md) |
| 2026-11-26 | 拆分持久化控制与日志队列，增加锁错误重试和故障隔离，缓存出口配置，移出主循环的数据库调用，并解除日志轮询与 Worker 步骤的反压。80 项回归及 30 秒本机压测通过。 | [服务编排](./service_orchestration.md)、[存储](./storage_persistence.md)、[Worker 与出口](./workers_egress.md)、[HTTP 与前端](./interfaces_frontend.md) |
