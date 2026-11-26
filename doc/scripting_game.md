# 脚本引擎与游戏业务能力 (`scripting_game.md`)

> **隶属主文档**：[main.md](./main.md)  
> **覆盖路径**：`src/scripting/`, `src/game/`, `scripts/`

---

## 1. 模块职责

提供可组合的自动化脚本框架、可复用的游戏领域能力（战斗、道具、宠物、本地 OCR），以及面向具体活动/日常玩法的专用脚本集合。

---

## 2. 脚本引擎 (`src/scripting/`)

| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `src/scripting/loader.py` | `InteractionScript`, `discover_scripts()`, `script_module_names()` | 自动发现并校验 `scripts/` 目录下的可选交互脚本及其元数据/参数定义。 |
| `src/scripting/composer.py` | `MessageBatchStep`, `ScriptStep`, `execute_steps()`, `execute_combination()` | 将通用 `#send` 报文批次与专用交互脚本按顺序编排执行，支持重复次数、间隔与跳过统计。 |
| `src/scripting/isolation.py` | `call_in_process()` | 在一次性子进程中限时调用同步第三方函数（如 OCR 推理），防止阻塞主事件循环或崩溃污染 Worker。 |
| `src/scripting/jobs.py` & `job_worker.py` | `load_job()`, `build_steps()`, `run_accounts()`, `SecretFilter` | 配置驱动型 CLI 作业的预检、账号并发调度与脱敏结果落盘。 |
| `src/scripting/good_ids.py` | `HotGoodIds` | 进程内缓存双人自动战斗脚本的可编辑目标角色 ID 列表（支持热更新）。 |

---

## 3. 游戏领域能力 (`src/game/`)

### 3.1 战斗自动化与状态管理 (`src/game/battle/`)
| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `auto_battle.py` | `AutoBattle` | 独立为每个可控出战位选择技能及阵亡替补亚比。 |
| `state.py` | `BattleLifecycle` | 单账号局部的战斗标识、进入就绪态与完整生命周期状态机。 |
| `control.py` | `RoundScope`, `BattleStateError`, `BattleRoundSkipped`, `begin_round()`, `abort_round()` | 单账号战斗超时截止控制与单轮放弃/跳过信号传递。 |
| `message_log.py` | `BattleMessageLog` | 账号级临时战斗报文取证缓存（断线即释放）。 |
| `key_logs.py` | `is_key_log()` | 过滤仅允许跨 Worker/Session 边界上报的关键生命周期与恢复日志。 |
| `diagnostics.py` | `emit()`, `capture()` | 单账号单次脚本调用的可选诊断采集。 |

### 3.2 道具、宠物与本地 OCR (`src/game/`)
| 文件路径 | 核心类/函数 | 职责说明 |
| :--- | :--- | :--- |
| `src/game/items.py` | `get_item_count()` | 复用当前账号会话实时查询道具库存（不购买、不缓存）。 |
| `src/game/pets/storage.py` | `find_storage_i()` | 宠物仓库查询与记录定位（复用已有会话）。 |
| `src/game/pets/backpack.py` & `cards.py` | — | 背包出战亚比调整与亚比魂卡更换的公共操作占位模块（待具体协议确认后补全）。 |
| `src/game/ocr/fast_ocr.py` & `ocr.py` | `Recognition`, `recognize()`, `recognize_bytes()` | 本地轻量数字/验证码特征提取与识别。 |

---

## 4. 内置专用脚本清单 (`scripts/`)

| 脚本文件 | 入口函数 | 功能说明 |
| :--- | :--- | :--- |
| `scripts/auto_arena.py` | `run()` | 竞技场自动匹配与校验，支持自动战斗或逃跑策略。 |
| `scripts/auto_hatch.py` | `run()` | 自动更新孵化状态、查询并收取孵化产物。 |
| `scripts/item_count.py` | `run()` | 根据前端传入参数查询指定道具当前数量。 |
| `scripts/mt250816.py` | `run()`, `keep_mt250816_best()` | 活动 `MT250816` 自动化：解析关卡分数并保留最高分结果。 |
| `scripts/rank_info.py` | `run()`, `parse_rank()` | 查询账号排位赛季信息并输出当前展示段位。 |

---

## 5. 维护注意点

- **会话复用原则**：`src/game/` 与 `scripts/` 中的业务操作严禁自行新建连接，必须复用调用方传入的 `ScriptContext` / 会话上下文。
- **日志克制原则**：高频战斗或轮询脚本应尽量通过 `key_logs.py` 控制跨进程日志频率，避免在高并发在线场景下产生日志风暴。
- **新增脚本规范**：在 `scripts/` 新增脚本后，确保遵循 `loader.py` 的元数据与 `run()` 签名约定，并同步更新本子文档第 4 节的「内置专用脚本清单」与 [main.md](./main.md) 变更日志。
