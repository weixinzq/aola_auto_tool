# 账号认证、网络传输与二进制协议 (`network_protocol.md`)

> **隶属主文档**：[main.md](./main.md)  
> **覆盖路径**：`src/accounts/`, `src/network/`, `src/protocol/`, `src/messaging/`, `src/config.py`

---

## 1. 模块职责

封装奥拉星客户端从 HTTP 登录鉴权、选服建连、二进制协议加解密/AMF3 编解码，到长连接消息路由、通用 `#send` 报文分发与服务器可用性探测的全链路底层能力。

---

## 2. 子模块与文件清单

### 2.1 账号认证与会话建连 (`src/accounts/`)
| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `src/accounts/login.py` | `http_login()`, `parse_server_list()`, `select_role()`, `validate_game_login()`, `LoginResult`, `LoginBlockedError`, `ZoneData` | 异步 HTTP 登录、角色解析、服务器列表解析与风控拦截识别（429/频率限制/人机验证）。 |
| `src/accounts/browser_login.py` | `is_captcha_page()` | 在不启动浏览器的前提下识别登录验证页/人机验证响应。 |
| `src/accounts/session.py` | `authenticate()`, `select_zone()`, `open_context()`, `initialize_player_context()`, `initialize_session_clock()` | 串联认证、区服选择、TCP 建连、服务端时钟同步与玩家初始化握手。 |
| `src/accounts/manager.py` | `AccountSpec`, `AccountConnection`, `AccountManager`, `load_account_specs()` | 单账号独立连接生命周期管理（`connect`/`disconnect`/`probe_server`）与重试封装。 |

### 2.2 网络传输与消息路由 (`src/network/`)
| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `src/network/socket_client.py` | `GameSocket`, `login_and_connect()` | 异步 TCP 长连接与二进制协议帧收发（上行：`[4B长度][4B序号][XOR加密包体]`；下行：`[4B长度][1B类型][消息体]`），使用 `_send_lock` 保证发包有序。 |
| `src/network/context.py` | `AppContext`, `MessageRouter`, `MessageSubscription`, `SessionClock` | 单读者（Single-Reader）消息分发中心，将下行包按命令/条件路由至多个异步订阅者，并维护断线观察者与战斗收包日志开关。 |
| `src/network/server_probe.py` | `probe_server()`, `transport_result()` | 一次性 Flash/H5 服务器可用性与错峰登录探测，仅在选定出口的 Worker 内执行。 |
| `src/network/proxy.py` | `process_proxy()`, `proxy_connector()`, `proxy_socket()` | 显式 SOCKS5 代理路由，供登录 HTTP 与游戏 TCP 共用。 |

### 2.3 协议编解码与加密 (`src/protocol/`, `src/config.py`)
| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `src/config.py` | `UpCmd`, `DownCmd`, `Amf3Type`, `load_user_config()` | 协议命令号、AMF3 类型常量与默认配置（精确对照 H5 源码）。 |
| `src/protocol/amf3.py` & `_amf3_native.cpp` | `Amf3Writer`, `Amf3Reader`, `TraitsInfo` | AMF3 二进制序列化/反序列化，优先走 C++ 原生扩展加速（由 `setup.py` 编译）。 |
| `src/protocol/encrypt.py` | `encrypt_byte_array()`, `outer_xor_encrypt()` | 双层协议加密与外层 XOR 加密实现。 |
| `src/protocol/msgseq.py` | `MsgSeq`, `InnerSeq` | 上行外层包序号与内层消息序号生成器。 |
| `src/protocol/as3_compat.py` | `to_as3_int32()`, `as3_remainder()` | 严格模拟 ActionScript 3 有符号 32 位整数与取余语义。 |
| `src/protocol/message_codec.py` | `build_login_message()`, `build_xt_request()`, `build_message()` | 登录包与扩展命令（`extId + cvt + AMF3`）打包/解析纯函数。 |
| `src/protocol/operations.py` | `init_game()`, `send_activity_cmd()` | 高层游戏协议指令封装。 |

### 2.4 通用 `#send` 报文解析与分发 (`src/messaging/`)
| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `src/messaging/parser.py` | `SendMessage`, `parse_send_message()`, `parse_pipe_separated_messages()` | 解析抓包导出的 `#send=<JSON>|` 格式报文。 |
| `src/messaging/dispatcher.py` | `validate_messages()`, `execute_messages()`, `dispatch_to_accounts()`, `_BattleExecutor` | 报文预检、账号隔离分发、房间切换与内嵌战斗报文执行。 |
| `src/messaging/audit_log.py` | `append_send_results()` | 无凭据的 JSONL 发送结果审计日志。 |

---

## 3. 维护注意点

- **协议严格一致性**：`src/protocol/` 与 `src/config.py` 精确对照游戏 H5 源码语义（特别是 `as3_compat`、`MsgSeq`、`encrypt`），任何改动必须验证二进制包字节级一致。
- **C++ 扩展编译**：修改 `src/protocol/_amf3_native.cpp` 后需重新运行 `setup.py` 构建扩展，并保持与 `amf3.py` 纯 Python 回退实现行为一致。
- **日志与敏感信息**：网络层异常和登录失败诊断（`diagnostic_dir`）可能包含 `sid`/`loginkey`，向外部暴露前必须经过 `Redactor` 脱敏。
