# OLA Assistant Demo

这是一个最小可运行版本：

- 前端：纯静态 HTML/CSS/JS
- 中间层：Python 标准库 HTTP 服务
- Codex 后端：由中间层自动拉起 `codex app-server`

## 目录说明

- `server.py`：中间层入口，同时负责启动 `codex app-server`
- `static/index.html`：页面结构
- `static/styles.css`：页面样式
- `static/app.js`：前端交互逻辑

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

- 第一次发送消息时，中间层会自动启动 `codex app-server`
- 第一次可能会慢一些，因为 Rust 可能需要编译或初始化
- 这版是最小闭环，先保证页面、接口、Codex 调用都能通

## 停止服务

在运行 `server.py` 的终端里按：

```bash
Ctrl + C
```

这会一起结束中间层和它拉起的 `codex app-server`
