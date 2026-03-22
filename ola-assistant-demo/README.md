# OLA Assistant Demo

这是一个最小可运行版本：

- 前端：纯静态 HTML/CSS/JS
- 中间层：Python 标准库 HTTP 服务
- Codex 后端：由中间层自动拉起 `codex app-server`

## 目录说明

- `server.py`：OLA HTTP 服务入口，只保留产品路由、会话存储和前端状态聚合
- `codex_adapter.py`：薄的 Codex 适配层，当前已开始切到 Rust native bridge，后续继续向原生 `in_process/app-server-client` 对齐
- `static/index.html`：页面结构
- `static/styles.css`：页面样式
- `static/app.js`：前端交互逻辑
- `NATIVE_MIGRATION.md`：中间层瘦身与原生能力迁移清单

## 运行前确认

1. 你已经安装好 Rust，并且 `cargo` 能用
2. 你已经至少成功运行过一次：

```bash
cd /Users/bytedance/Documents/GitHub/codex/codex-rs
cargo run --bin codex
```

3. 本地图标存在：

```bash
/Users/bytedance/Downloads/ola-logo-new.png
```

4. 初始化 OLA 的独立状态目录：

```bash
cd /Users/bytedance/Documents/GitHub/codex
python3 ola-assistant-demo/setup_ola_home.py
```

5. 确认官方 Codex 已登录 ChatGPT 账号。

OLA 默认沿用：

```text
/Users/bytedance/.codex/auth.json
```

也就是继续走你当前本机的 ChatGPT 账号额度，而不是自定义 API provider。

## 启动方式

在仓库根目录执行：

```bash
cd /Users/bytedance/Documents/GitHub/codex
python3 ola-assistant-demo/server.py
```

启动后，打开浏览器访问：

```text
http://127.0.0.1:8765
```

## 说明

- OLA 现在默认使用独立的 `CODEX_HOME`：
  `~/.ola-codex`
- 第一次发送消息时，中间层会自动启动 `codex app-server`
- 第一次可能会慢一些，因为 Rust 可能需要编译或初始化
- 这版是最小闭环，先保证页面、接口、Codex 调用都能通
- `server.py` 启动时会自动加载 `ola-assistant-demo/.env.local` 或 `ola-assistant-demo/.env`
- OLA 会隔离自己的配置、日志、会话和 memory
- 当前默认沿用你本机已有的 ChatGPT 登录态：
  `~/.codex/auth.json`
- OLA 自己的运行配置仍然写在：
  `~/.ola-codex/config.toml`
- 如果你直接粘贴超长的 Case 导出表格，OLA 会自动切换到“Codex 原生批量分析”模式：
  - 自动解析 TSV 导出内容，并补充轻量结构化统计
  - 不再由 Python 中间层自己做线程池并发或多 session 编排
  - 由当前这一个 Codex 线程按需直接调用原生 `spawn_agent` / `wait` / `close_agent`
  - 上下文过长时，优先依赖 Codex 原生压缩能力继续分析
  - 最终仍由主线程统一汇总成一份业务可读的中文报告
- 当前正在推进“薄适配层”改造：
  - OLA 前端和产品接口仍保留
  - 但会逐步删除 Python 中间层里重复 Codex 原生语义的逻辑
  - 第二阶段已开始引入 Rust native bridge，让 OLA 底层不再直接依赖 `codex app-server` 的 stdio 进程语义
  - 详细路线见：
    [`NATIVE_MIGRATION.md`](/Users/bytedance/Documents/GitHub/codex/ola-assistant-demo/NATIVE_MIGRATION.md)

## 停止服务

在运行 `server.py` 的终端里按：

```bash
Ctrl + C
```

这会一起结束中间层和它拉起的 `codex app-server`
