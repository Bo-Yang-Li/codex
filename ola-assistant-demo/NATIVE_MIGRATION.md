# OLA Native Migration

这个文件记录 OLA 从“厚中间层”迁移到“薄适配层”的路线，目标是尽量复用 Codex 原生能力，而不是在 Python 里继续重复实现协议语义。

## 当前结论

OLA 中间层不应该删除到只剩前端直连，但应该继续削薄。

保留的职责：

- OLA 自己的会话列表与本地存档
- OLA 前端当前依赖的 REST API
- OLA 批量 Case 的产品入口与提示词改写
- 面向用户的 progress / thinking 展示聚合

持续移除的职责：

- 子进程 / JSON-RPC / notification 细节散落在业务层
- 对 `phase`、`turn/completed`、`willRetry`、`incomplete` 的重复语义判断
- 工具输出缺失后的私有兜底规则
- 任何已经由 Codex 原生协议明确定义的状态机

## 第一阶段

已完成：

- 将 Codex app-server 交互抽到 [`codex_adapter.py`](/Users/bytedance/Documents/GitHub/codex/ola-assistant-demo/codex_adapter.py)
- `server.py` 仅保留 OLA 产品逻辑与 REST 路由
- 将“Codex 会话执行”与“OLA 展示/存储逻辑”分层，后续替换传输实现时不需要重写前端 API

## 第二阶段

已开始：

- 新增 Rust bridge 二进制，目标是让 OLA 不再直接拉起 `codex app-server`
- 这个 bridge 对 Python 继续暴露相同的 JSON 行协议
- 但内部改为直接使用 Codex 原生 `InProcessAppServerClient`
- 这样可以在不重写 OLA 前端和 REST API 的前提下，把最关键的运行时语义切回 Codex 原生

## 下一阶段

优先级从高到低：

1. 用 Codex 原生 `app-server-client` / `in_process` 语义重新实现适配层
2. 继续删除 Python 里重复的协议恢复逻辑，尽量下沉到 Codex 原生实现
3. 评估是否值得把前端从 REST 轮询迁移到更接近原生 app-server 的事件模型

## 风险评估

直接让前端绕过中间层、直连 Codex app-server 的风险仍然偏高，因为当前前端依赖：

- 会话列表接口
- 本地 conversation 存储模型
- progress 轮询接口
- OLA 批量分析专用入口

因此更稳的做法是保留一个很薄的 OLA 适配层，但让它不再定义自己的 Codex 运行时语义。
