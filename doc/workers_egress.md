# 出口分组、Mihomo 代理与多进程 Worker (`workers_egress.md`)

> **隶属主文档**：[main.md](./main.md)  
> **覆盖路径**：`src/service/egress.py`, `src/service/mihomo.py`, `src/service/mihomo_host.py`, `src/service/workers.py`, `src/service/worker_host.py`, `src/service/worker_process.py`, `src/service/worker_transport.py`, `src/network/proxy.py`

---

## 1. 模块职责

负责**网络出口隔离与数据面多进程托管**：
- 维护经过公网 IP 探测验证的出口分组池（防止同 IP 超限或重复启用）。
- 托管私有 Mihomo 代理进程，为每个代理节点分配固定且带鉴权的本地代理路由。
- 为每个出口分组拉起独立的 `WorkerHost` 子进程，通过私有 Loopback 控制通道进行 RPC 通信与流式事件回传。

---

## 2. 核心文件与类清单

| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `src/service/egress.py` | `EgressService` | 出口分组 CRUD、节点导入 `import_nodes()`、公网 IP 探测 `probe()`、重复 IP 拦截 `_enable_check()`、Clash 进程分流规则生成 `rules()`。 |
| `src/service/mihomo.py` | `Mihomo` | 托管独立 Mihomo 内核进程，解析 `mihomo/nodes.yaml`，为各 `clash_policy` 提供认证 SOCKS5 `proxy_url()` 并监听进程掉线（触发 `on_lost`）。 |
| `src/service/mihomo_host.py` | — | Linux 下将 Mihomo 子进程绑定到父进程生命周期（父退子杀）。 |
| `src/service/workers.py` | `WorkerPool`, `Worker`, `RemoteAccountConnection`, `RemoteContext`, `RemoteMessages` | 父进程侧进程池管理、RPC 请求/取消/关闭、将子进程连接适配为 `SessionService` 可消费的统一连接接口。 |
| `src/service/worker_host.py` | `WorkerHost`, `detect_exit_ip()`, `main()` | 子进程主循环：接收父进程指令并发派发 `handle()`，管理本进程内账号连接 `connections`、脚本执行与日志/断线/步骤事件推送。 |
| `src/service/worker_process.py` | `WorkerJob`, `prepare_worker_executable()`, `bind_linux_parent()`, `actual_executable()` | 为每个出口分组复制独立路径的 Python 可执行文件（用于外部 Clash `PROCESS-PATH` 严格分流），并用 Windows Job Object / Linux `prctl(PR_SET_PDEATHSIG, SIGKILL)` 防止孤儿进程。 |
| `src/service/worker_transport.py` | `WorkerListener`, `open_worker_connection()` | 独立于标准输入输出（stdin/stdout）的本地 Loopback TCP 控制通道，使用随机端口与环境变量 Token 双向认证。 |
| `src/network/proxy.py` | `process_proxy()`, `proxy_connector()`, `proxy_socket()` | 读取子进程环境变量 `AOLA_PROXY_URL`，为 `aiohttp` 和游戏 TCP Socket 提供统一 SOCKS5 代理封装。 |

---

## 3. 父子进程控制协议（Loopback JSON-RPC）

通信载体为本地 TCP 长连接，按换行符（`\n`）分隔 UTF-8 JSON 帧（单帧上限 `MESSAGE_LIMIT`）：

- **父 → 子请求格式**：`{"id": "<uuid>", "op": "<op>", "account_id": "...", "data": {...}}`
  - 支持操作：`hello`（身份与真实可执行路径校验）、`probe`（探测公网出口 IP）、`connect`、`initialize`、`execute`、`disconnect`、`server_probe`、`cancel`（取消指定 `request_id` 任务）、`shutdown`。
- **子 → 父响应格式**：`{"id": "<uuid>", "ok": true|false, "data": {...}, "error": {...}}`
- **子 → 父异步事件**：
  - `{"event": "log_batch", "items": [{"account_id": "...", "message": "...", "timestamp": "..."}]}`
  - 父进程也接收旧版单条 `log` 事件。
  - `{"event": "steps", "request_id": "...", "steps": [...]}`
  - `{"event": "disconnected", "account_id": "...", "reason": "..."}`

---

## 4. 关键机制与维护注意点

- **严格出口唯一性**：`EgressService._enable_check()` 禁止两个同时启用或仍有在线连接的出口分组拥有相同的公网 `exit_ip`；若某出口的 Worker 崩溃或 Mihomo 退出，会自动触发 `egress.failed()` 下线该组并唤醒等待队列。
- **取消语义强一致**：父进程侧 `Worker._request()` 收到 `CancelledError` 时，会先向子进程发 `cancel` 并等待子进程真正停止操作，再释放父进程账号独占权，防止“父以为已取消、子仍在发包”。
- `WorkerHost.send()` 使用同一个写锁发送响应、步骤批次、日志批次及断线通知。日志在子进程按本组已知凭据脱敏，每 100 ms 或达到 50 条发送一批，最多缓冲 5,000 条。
- 初始化、执行、探测、断开、取消和关停返回前发送待处理日志，保证父进程能在释放账号归属之前接收日志。父进程将步骤批次交给持久化写线程。
- 创建和启动分组时通过线程池运行 `prepare_worker_executable()`。规则生成与进程启动共享文件准备锁，避免同时写同一路径。
