> 连接与任务模型已更新：所有账号统一使用 IP 池（含可选直连），任务只提交账号和脚本。现行操作见 [IP 池说明](docs/ip-pool.md)；下文涉及固定出口绑定或旧任务编排的内容已停用。


独立 Python 后台统一管理账号、游戏会话和任务，CLI 与 Vue 浏览器前端通过同一套 `/api/v1` 接口操作。支持 Linux 部署；关闭网页或退出 CLI 后，已提交任务继续由后台执行。

## 启动

需要 Python 3.12、Node.js 22.12 或更高版本和 npm。克隆仓库后，在项目根目录创建虚拟环境。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```


安装后台依赖：

```bash
python -m pip install -r requirements.txt
```

使用 Node.js 22.12 或更高版本构建浏览器前端：

```bash
cd frontend
npm ci
npm run build
cd ..
```

启动后台，浏览器打开 `http://127.0.0.1:8765/`：

```bash
python cli_main.py serve
```


默认监听 `127.0.0.1:8765`，运行数据保存在 `data/service`。账号、出口分组和任务自行设置。

## 源码与构建

- `src/`：账号登录、游戏通信、协议、消息处理、后台服务和接口。
- `scripts/`：随程序提供的专用脚本。
- `frontend/`：Vue 网页源码、依赖锁文件和构建配置；`npm run build` 输出到 `frontend/dist`。
- `tools/`：通用接口导出和服务器探测工具。
- `csharp/AolaWorkbench/`：WebView2 工作台，显示现有网页，与浏览器使用同一套 HTTP 服务。




