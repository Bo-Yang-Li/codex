#!/usr/bin/env python3
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from batch_analysis import build_native_batch_prompt, maybe_parse_case_export, should_use_batch_mode

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_RS_DIR = REPO_ROOT / "codex-rs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
OLA_DEMO_DIR = Path(__file__).resolve().parent
LOGO_PATH = Path("/Users/bytedance/Downloads/ola-logo-new.png")
OLA_CODEX_HOME = Path.home() / ".ola-codex"
CONVERSATIONS_PATH = OLA_CODEX_HOME / "ola_conversations.json"
OLA_LOG_PATH = OLA_CODEX_HOME / "log" / "ola-assistant.log"
BATCH_INPUT_DIR = Path("/tmp/ola-codex-batch")
HOST = "127.0.0.1"
PORT = 8765
PROGRESS_STEPS = [
    ("connect", "连接本地 OLA 引擎"),
    ("analyze", "分析你的请求"),
    ("tool", "检索上下文 / 执行任务"),
    ("compose", "整理并生成回复"),
    ("done", "完成"),
]


def load_ola_config_text() -> str:
    config_path = OLA_CODEX_HOME / "config.toml"
    if not config_path.exists():
        return ""
    return config_path.read_text(encoding="utf-8", errors="ignore")


def log_event(message: str) -> None:
    ensure_directory(OLA_LOG_PATH.parent)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        with OLA_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def load_local_env() -> None:
    for env_file in [OLA_DEMO_DIR / ".env.local", OLA_DEMO_DIR / ".env"]:
        if not env_file.exists():
            continue
        for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def active_model_provider_id() -> Optional[str]:
    config_text = load_ola_config_text()
    match = re.search(r'^model_provider\s*=\s*"([^"]+)"', config_text, flags=re.MULTILINE)
    return match.group(1) if match else None


def active_provider_env_key() -> Optional[str]:
    provider_id = active_model_provider_id()
    if provider_id is None:
        return None
    config_text = load_ola_config_text()
    pattern = (
        r'^\[model_providers\.'
        + re.escape(provider_id)
        + r'\]\n(?:(?!^\[).*\n)*?env_key\s*=\s*"([^"]+)"'
    )
    match = re.search(pattern, config_text, flags=re.MULTILINE)
    return match.group(1) if match else None


def validate_provider_env() -> None:
    provider_id = active_model_provider_id()
    env_key = active_provider_env_key()
    if provider_id in {None, "", "openai"} or env_key is None:
        return
    if os.environ.get(env_key):
        return
    raise RuntimeError(
        f"当前 OLA 使用自定义 provider `{provider_id}`，但缺少环境变量 `{env_key}`。"
        f"请先 `export {env_key}=...` 后再启动服务。"
    )


def discover_cargo() -> Optional[str]:
    cargo = shutil.which("cargo")
    if cargo:
        return cargo

    fallback = Path.home() / ".cargo" / "bin" / "cargo"
    if fallback.exists():
        return str(fallback)

    return None


def prepare_user_message(message: str) -> str:
    if len(message) < 5000:
        return message
    return (
        "请尽量简洁输出，优先给结论和最关键依据，控制在 220 字以内；"
        "如果输入材料很长，不要复述原文，只提炼最重要的判断。\n\n"
        + message
    )


class CodexRpcError(RuntimeError):
    pass


class ConversationStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def list_conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            conversations = list(self._data["conversations"].values())

        conversations.sort(key=lambda item: item["updatedAt"], reverse=True)
        return [self._summary(item) for item in conversations]

    def get_conversation(self, conversation_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            conversation = self._data["conversations"].get(conversation_id)
            if conversation is None:
                return None
            return json.loads(json.dumps(conversation))

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            if conversation_id not in self._data["conversations"]:
                return False
            del self._data["conversations"][conversation_id]
            self._persist_locked()
            return True

    def ensure_conversation(self, conversation_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            if conversation_id:
                existing = self._data["conversations"].get(conversation_id)
                if existing is not None:
                    return json.loads(json.dumps(existing))

            now = int(time.time())
            new_id = conversation_id or f"conv_{uuid4().hex}"
            conversation = {
                "id": new_id,
                "title": "新对话",
                "threadId": None,
                "createdAt": now,
                "updatedAt": now,
                "messages": [],
            }
            self._data["conversations"][new_id] = conversation
            self._persist_locked()
            return json.loads(json.dumps(conversation))

    def set_thread_id(self, conversation_id: str, thread_id: str) -> None:
        with self._lock:
            conversation = self._data["conversations"][conversation_id]
            conversation["threadId"] = thread_id
            conversation["updatedAt"] = int(time.time())
            self._persist_locked()

    def append_exchange(
        self,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._lock:
            conversation = self._data["conversations"][conversation_id]
            conversation["messages"].append(
                {"role": "user", "text": user_text, "createdAt": now}
            )
            conversation["messages"].append(
                {"role": "assistant", "text": assistant_text, "createdAt": now}
            )
            if conversation["title"] == "新对话" and user_text.strip():
                conversation["title"] = self._make_title(user_text)
            conversation["updatedAt"] = now
            self._persist_locked()
            return json.loads(json.dumps(conversation))

    def _load(self) -> dict[str, Any]:
        ensure_directory(self._path.parent)
        if not self._path.exists():
            return {"conversations": {}}

        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"conversations": {}}

    def _persist_locked(self) -> None:
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _make_title(text: str) -> str:
        single_line = " ".join(text.strip().split())
        return single_line[:24] + ("..." if len(single_line) > 24 else "")

    @staticmethod
    def _summary(conversation: dict[str, Any]) -> dict[str, Any]:
        messages = conversation.get("messages", [])
        last_message = messages[-1]["text"] if messages else ""
        return {
            "id": conversation["id"],
            "title": conversation["title"],
            "updatedAt": conversation["updatedAt"],
            "createdAt": conversation["createdAt"],
            "messageCount": len(messages),
            "preview": last_message[:40] + ("..." if len(last_message) > 40 else ""),
        }


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def persist_batch_input(conversation_id: str, message: str) -> Path:
    ensure_directory(BATCH_INPUT_DIR)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    path = BATCH_INPUT_DIR / f"{conversation_id}-{timestamp}.txt"
    path.write_text(message, encoding="utf-8")
    return path


class ProgressTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}

    def start(self, conversation_id: str) -> None:
        with self._lock:
            self._states[conversation_id] = {
                "visible": True,
                "completed": False,
                "error": None,
                "steps": [
                    {"key": key, "label": label, "status": "pending"}
                    for key, label in PROGRESS_STEPS
                ],
            }
            self._set_active_locked(conversation_id, "connect")

    def start_batch(self, conversation_id: str, total_chunks: int, worker_count: int) -> None:
        with self._lock:
            self._states[conversation_id] = {
                "visible": True,
                "completed": False,
                "error": None,
                "steps": [
                    {"key": "parse", "label": "解析并切分超长 Case", "status": "done"},
                    {
                        "key": "dispatch",
                        "label": f"启动并行子 Agent（{worker_count} 个）",
                        "status": "done",
                    },
                    {
                        "key": "chunks",
                        "label": f"并行分析分片（0/{total_chunks}）",
                        "status": "active",
                    },
                    {"key": "merge", "label": "汇总所有子 Agent 结果", "status": "pending"},
                    {"key": "done", "label": "完成", "status": "pending"},
                ],
            }

    def update_batch_progress(
        self,
        conversation_id: str,
        completed_chunks: int,
        total_chunks: int,
    ) -> None:
        with self._lock:
            state = self._states.get(conversation_id)
            if state is None:
                return
            self._set_step_label_locked(
                conversation_id,
                "chunks",
                f"并行分析分片（{completed_chunks}/{total_chunks}）",
            )
            if completed_chunks >= total_chunks:
                self._mark_done_locked(conversation_id, "chunks")

    def mark_batch_merging(self, conversation_id: str) -> None:
        with self._lock:
            self._mark_done_locked(conversation_id, "chunks")
            self._set_active_locked(conversation_id, "merge")

    def mark_batch_writing(self, conversation_id: str) -> None:
        with self._lock:
            self._mark_done_locked(conversation_id, "chunks")
            self._mark_done_locked(conversation_id, "merge")
            self._set_step_label_locked(conversation_id, "merge", "汇总完成，正在输出最终结论")
            self._set_active_locked(conversation_id, "done")

    def mark_analyzing(self, conversation_id: str) -> None:
        with self._lock:
            self._mark_done_locked(conversation_id, "connect")
            self._set_active_locked(conversation_id, "analyze")

    def mark_tooling(self, conversation_id: str) -> None:
        with self._lock:
            self._mark_done_locked(conversation_id, "connect")
            self._mark_done_locked(conversation_id, "analyze")
            self._set_active_locked(conversation_id, "tool")

    def mark_composing(self, conversation_id: str) -> None:
        with self._lock:
            self._mark_done_locked(conversation_id, "connect")
            self._mark_done_locked(conversation_id, "analyze")
            self._mark_done_locked(conversation_id, "tool")
            self._set_active_locked(conversation_id, "compose")

    def finish(self, conversation_id: str) -> None:
        with self._lock:
            state = self._states.get(conversation_id)
            if state is None:
                return
            for step in state["steps"]:
                if step["key"] != "done":
                    step["status"] = "done"
            self._set_active_locked(conversation_id, "done")
            self._mark_done_locked(conversation_id, "done")
            state["completed"] = True

    def fail(self, conversation_id: str, message: str) -> None:
        with self._lock:
            if conversation_id not in self._states:
                self.start(conversation_id)
            self._states[conversation_id]["error"] = message
            self._states[conversation_id]["completed"] = True

    def snapshot(self, conversation_id: Optional[str]) -> dict[str, Any]:
        if not conversation_id:
            return {"visible": False, "completed": False, "error": None, "steps": []}

        with self._lock:
            state = self._states.get(conversation_id)
            if state is None:
                return {"visible": False, "completed": False, "error": None, "steps": []}
            return json.loads(json.dumps(state))

    def _set_active_locked(self, conversation_id: str, key: str) -> None:
        state = self._states.get(conversation_id)
        if state is None:
            return
        for step in state["steps"]:
            if step["status"] == "active":
                step["status"] = "done"
        for step in state["steps"]:
            if step["key"] == key and step["status"] == "pending":
                step["status"] = "active"
                return

    def _mark_done_locked(self, conversation_id: str, key: str) -> None:
        state = self._states.get(conversation_id)
        if state is None:
            return
        for step in state["steps"]:
            if step["key"] == key:
                step["status"] = "done"
            return

    def _set_step_label_locked(self, conversation_id: str, key: str, label: str) -> None:
        state = self._states.get(conversation_id)
        if state is None:
            return
        for step in state["steps"]:
            if step["key"] == key:
                step["label"] = label
                return


class ChatTaskTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def start(self, conversation_id: str) -> None:
        with self._lock:
            self._tasks[conversation_id] = {
                "running": True,
                "done": False,
                "error": None,
                "reply": "",
                "thinking": "正在思考你的请求...",
                "updatedAt": int(time.time()),
            }

    def append(self, conversation_id: str, delta: str) -> None:
        with self._lock:
            task = self._tasks.get(conversation_id)
            if task is None:
                return
            task["reply"] += delta
            task["updatedAt"] = int(time.time())

    def replace(self, conversation_id: str, text: str) -> None:
        with self._lock:
            task = self._tasks.get(conversation_id)
            if task is None:
                return
            task["reply"] = text
            task["updatedAt"] = int(time.time())

    def set_thinking(self, conversation_id: str, text: str) -> None:
        with self._lock:
            task = self._tasks.get(conversation_id)
            if task is None:
                return
            task["thinking"] = text
            task["updatedAt"] = int(time.time())

    def append_thinking(self, conversation_id: str, delta: str) -> None:
        with self._lock:
            task = self._tasks.get(conversation_id)
            if task is None:
                return
            current = task.get("thinking") or ""
            task["thinking"] = current + delta
            task["updatedAt"] = int(time.time())

    def finish(self, conversation_id: str, text: str) -> None:
        with self._lock:
            task = self._tasks.get(conversation_id)
            if task is None:
                task = {}
                self._tasks[conversation_id] = task
            task.update(
                {
                    "running": False,
                    "done": True,
                    "error": None,
                    "reply": text,
                    "thinking": "",
                    "updatedAt": int(time.time()),
                }
            )

    def fail(self, conversation_id: str, message: str) -> None:
        with self._lock:
            task = self._tasks.get(conversation_id)
            if task is None:
                task = {}
                self._tasks[conversation_id] = task
            task.update(
                {
                    "running": False,
                    "done": True,
                    "error": message,
                    "thinking": "",
                    "updatedAt": int(time.time()),
                }
            )

    def snapshot(self, conversation_id: Optional[str]) -> dict[str, Any]:
        if not conversation_id:
            return {"running": False, "done": False, "error": None, "reply": ""}
        with self._lock:
            task = self._tasks.get(conversation_id)
            if task is None:
                return {"running": False, "done": False, "error": None, "reply": ""}
            return json.loads(json.dumps(task))


class CodexSession:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen[str]] = None
        self._request_id = 0
        self._pending: Dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._notifications: Deque[dict[str, Any]] = deque()
        self._server_requests: Deque[dict[str, Any]] = deque()
        self._condition = threading.Condition()
        self._interaction_lock = threading.RLock()
        self._stderr_lines: Deque[str] = deque(maxlen=50)
        self._initialized = False

    def start(self) -> None:
        cargo = discover_cargo()
        if cargo is None:
            raise RuntimeError(
                "找不到 cargo。请先运行 `source \"$HOME/.cargo/env\"`，"
                "或者确认 Rust 已正确安装。"
            )

        if self._process is not None:
            return

        validate_provider_env()
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("CODEX_HOME", str(OLA_CODEX_HOME))
        log_event(
            "starting codex app-server"
            f" provider={active_model_provider_id()}"
            f" env_key={active_provider_env_key()}"
            f" env_present={'yes' if active_provider_env_key() and env.get(active_provider_env_key() or '') else 'no'}"
        )
        cmd = [cargo, "run", "--bin", "codex", "--", "app-server"]
        self._process = subprocess.Popen(
            cmd,
            cwd=str(CODEX_RS_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

        threading.Thread(target=self._stdout_reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()
        self._ensure_initialized()

    def close(self) -> None:
        process = self._process
        if process is None:
            return

        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self._process = None

    def chat(
        self,
        conversation_id: str,
        thread_id: Optional[str],
        message: str,
        on_delta: Optional[Callable[[str], None]] = None,
        apply_briefing: bool = True,
    ) -> dict[str, Any]:
        with self._interaction_lock:
            PROGRESS.start(conversation_id)
            TASKS.set_thinking(conversation_id, "正在连接本地 OLA 引擎...")
            self.start()
            thread_id = self._ensure_thread(thread_id)
            PROGRESS.mark_analyzing(conversation_id)
            TASKS.set_thinking(conversation_id, "正在分析你的请求...")
            return self.run_turn(
                thread_id,
                message,
                on_delta=on_delta,
                on_event=lambda method, params: self._handle_chat_event(
                    conversation_id,
                    method,
                    params,
                ),
                conversation_id=conversation_id,
                apply_briefing=apply_briefing,
            )

    def run_turn(
        self,
        thread_id: Optional[str],
        message: str,
        on_delta: Optional[Callable[[str], None]] = None,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
        conversation_id: Optional[str] = None,
        apply_briefing: bool = True,
    ) -> dict[str, Any]:
        with self._interaction_lock:
            self.start()
            thread_id = self._ensure_thread(thread_id)
            prepared_message = prepare_user_message(message) if apply_briefing else message
            turn = self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": prepared_message,
                            "text_elements": [],
                        }
                    ],
                    "approvalPolicy": "never",
                },
            )
            turn_id = turn["turn"]["id"]
            parts: list[str] = []
            fallback_text: Optional[str] = None
            truncated_by_max_tokens = False

            while True:
                self._drain_server_requests()
                notification = self._next_notification()
                method = notification.get("method")
                params = notification.get("params") or {}

                if params.get("turnId") not in {None, turn_id}:
                    continue

                if on_event is not None:
                    on_event(method, params)

                if method == "item/agentMessage/delta":
                    delta = params.get("delta", "")
                    if delta:
                        parts.append(delta)
                        if on_delta is not None:
                            on_delta(delta)
                elif method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        fallback_text = item.get("text") or fallback_text
                elif method == "turn/completed":
                    turn_payload = params.get("turn") or {}
                    if turn_payload.get("id") != turn_id:
                        continue
                    status = turn_payload.get("status")
                    if status == "failed":
                        error = (turn_payload.get("error") or {}).get("message")
                        raise CodexRpcError(error or "Codex turn failed.")
                    if status == "interrupted":
                        raise CodexRpcError("Codex turn was interrupted.")
                    break
                elif method == "error":
                    if params.get("willRetry"):
                        continue
                    error = (params.get("error") or {}).get("message")
                    if error:
                        if "max_output_tokens" in error and (parts or fallback_text):
                            truncated_by_max_tokens = True
                            log_event(
                                "turn hit max_output_tokens but partial output exists; "
                                f"turn_id={turn_id} partial_len={len(''.join(parts) or (fallback_text or ''))}"
                            )
                            break
                        raise CodexRpcError(error)

            reply = "".join(parts).strip() or (fallback_text or "").strip()
            if not reply:
                reply = "Codex 已完成本轮，但没有返回可展示的文本。"
            elif truncated_by_max_tokens:
                reply += "\n\n[输出因 provider 的 max_output_tokens 限制被截断]"

            return {
                "reply": reply,
                "threadId": thread_id,
                "turnId": turn_id,
                "conversationId": conversation_id,
            }

    def _handle_chat_event(
        self,
        conversation_id: str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if method == "item/agentMessage/delta":
            PROGRESS.mark_composing(conversation_id)
            TASKS.set_thinking(conversation_id, "正在整理最终回复...")
            return

        if method == "thread/compacted":
            PROGRESS.mark_tooling(conversation_id)
            TASKS.set_thinking(conversation_id, "上下文已自动压缩，正在继续分析...")
            return

        if method == "item/completed":
            item = params.get("item") or {}
            item_type = item.get("type")
            if item_type in {
                "commandExecution",
                "fileChange",
                "mcpToolCall",
                "dynamicToolCall",
                "webSearch",
                "imageView",
                "imageGeneration",
                "collabAgentToolCall",
                "contextCompaction",
            }:
                PROGRESS.mark_tooling(conversation_id)
                if item_type == "collabAgentToolCall":
                    TASKS.set_thinking(conversation_id, "正在调用 Codex 原生子 Agent 协同分析...")
                elif item_type == "contextCompaction":
                    TASKS.set_thinking(conversation_id, "正在压缩上下文并继续处理超长输入...")
                else:
                    TASKS.set_thinking(conversation_id, "正在调用工具和整理上下文...")
            if item_type == "agentMessage":
                PROGRESS.mark_composing(conversation_id)
                TASKS.set_thinking(conversation_id, "正在整理最终回复...")
            return

        if method == "turn/completed":
            turn_payload = params.get("turn") or {}
            status = turn_payload.get("status")
            if status == "failed":
                error = (turn_payload.get("error") or {}).get("message")
                PROGRESS.fail(conversation_id, error or "Codex turn failed.")
            elif status == "interrupted":
                PROGRESS.fail(conversation_id, "Codex turn was interrupted.")
            else:
                PROGRESS.finish(conversation_id)
            return

        if method == "error":
            if params.get("willRetry"):
                TASKS.set_thinking(
                    conversation_id,
                    f"连接暂时中断，正在重试... {(params.get('error') or {}).get('message', '')}".strip(),
                )
                return
            error = (params.get("error") or {}).get("message")
            if error:
                PROGRESS.fail(conversation_id, error)
            return

        if method == "item/started":
            item_type = (params.get("item") or {}).get("type")
            if item_type in {"plan", "reasoning"}:
                PROGRESS.mark_analyzing(conversation_id)
                TASKS.set_thinking(conversation_id, "正在拆解任务步骤...")
            elif item_type in {
                "commandExecution",
                "fileChange",
                "mcpToolCall",
                "dynamicToolCall",
                "webSearch",
                "imageView",
                "imageGeneration",
                "collabAgentToolCall",
                "contextCompaction",
            }:
                PROGRESS.mark_tooling(conversation_id)
                if item_type == "collabAgentToolCall":
                    TASKS.set_thinking(conversation_id, "正在分派 Codex 原生子 Agent...")
                elif item_type == "contextCompaction":
                    TASKS.set_thinking(conversation_id, "正在触发上下文压缩...")
                else:
                    TASKS.set_thinking(conversation_id, "正在执行任务步骤...")
            return

        if method in {"item/plan/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta"}:
            delta = params.get("delta", "")
            if delta:
                TASKS.append_thinking(conversation_id, delta)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        self._rpc(
            "initialize",
            {
                "clientInfo": {
                    "name": "ola_assistant_demo",
                    "title": "OLA Assistant Demo",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": False,
                    "optOutNotificationMethods": None,
                },
            },
        )
        self._notify("initialized", None)
        self._initialized = True

    def _ensure_thread(self, thread_id: Optional[str]) -> str:
        if thread_id is not None:
            return thread_id

        response = self._rpc(
            "thread/start",
            {
                "cwd": str(REPO_ROOT),
                "serviceName": "ola-assistant-demo",
                "personality": "friendly",
                "approvalPolicy": "never",
                "experimentalRawEvents": False,
                "persistExtendedHistory": False,
            },
        )
        return response["thread"]["id"]

    def _stdout_reader(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            with self._condition:
                if "id" in message and ("result" in message or "error" in message):
                    response_id = int(message["id"])
                    pending = self._pending.get(response_id)
                    if pending is not None:
                        pending.put(message)
                elif "id" in message and "method" in message:
                    self._server_requests.append(message)
                else:
                    self._notifications.append(message)
                self._condition.notify_all()

    def _stderr_reader(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return

        for raw_line in process.stderr:
            line = raw_line.rstrip()
            if line:
                self._stderr_lines.append(line)
                log_event(f"app-server stderr: {line}")

    def _rpc(self, method: str, params: Optional[dict[str, Any]]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        response_queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=1)
        with self._condition:
            self._pending[request_id] = response_queue
        self._write_message(
            {
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        log_event(f"rpc request method={method} id={request_id}")

        try:
            response = response_queue.get(timeout=180)
        except queue.Empty as exc:
            log_event(
                f"rpc timeout method={method} id={request_id} recent_stderr={list(self._stderr_lines)[-5:]}"
            )
            raise RuntimeError(
                f"等待 Codex 响应 `{method}` 超时。最近日志：{list(self._stderr_lines)[-5:]}"
            ) from exc
        finally:
            with self._condition:
                self._pending.pop(request_id, None)

        if "error" in response:
            message = response["error"].get("message", "Unknown error")
            log_event(f"rpc error method={method} id={request_id} message={message}")
            raise CodexRpcError(message)

        log_event(f"rpc success method={method} id={request_id}")
        return response["result"]

    def _notify(self, method: str, params: Optional[dict[str, Any]]) -> None:
        self._write_message({"method": method, "params": params})

    def _write_message(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("Codex app-server is not running.")
        if process.poll() is not None:
            raise RuntimeError(
                f"Codex app-server 已退出，退出码 {process.returncode}。"
                f"最近日志：{list(self._stderr_lines)[-10:]}"
            )

        serialized = json.dumps(payload, ensure_ascii=False)
        process.stdin.write(serialized + "\n")
        process.stdin.flush()

    def _next_notification(self) -> dict[str, Any]:
        deadline = time.time() + 180
        while True:
            self._drain_server_requests()
            with self._condition:
                if self._notifications:
                    return self._notifications.popleft()

                process = self._process
                if process is not None and process.poll() is not None:
                    log_event(
                        f"app-server exited returncode={process.returncode} recent_stderr={list(self._stderr_lines)[-10:]}"
                    )
                    raise RuntimeError(
                        f"Codex app-server 已退出，退出码 {process.returncode}。"
                        f"最近日志：{list(self._stderr_lines)[-10:]}"
                    )

                remaining = deadline - time.time()
                if remaining <= 0:
                    log_event(f"notification timeout recent_stderr={list(self._stderr_lines)[-10:]}")
                    raise RuntimeError(
                        f"等待 Codex 通知超时。最近日志：{list(self._stderr_lines)[-10:]}"
                    )
                self._condition.wait(timeout=remaining)

    def _drain_server_requests(self) -> None:
        while True:
            with self._condition:
                if not self._server_requests:
                    return
                request = self._server_requests.popleft()

            self._handle_server_request(request)

    def _handle_server_request(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        method = request.get("method")

        if request_id is None or method is None:
            return

        if method == "item/commandExecution/requestApproval":
            self._write_message({"id": request_id, "result": {"decision": "accept"}})
            return

        if method == "item/fileChange/requestApproval":
            self._write_message({"id": request_id, "result": {"decision": "accept"}})
            return

        if method == "item/tool/requestUserInput":
            self._write_message({"id": request_id, "result": {"answers": []}})
            return

        self._write_message(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Unsupported server request: {method}",
                },
            }
        )


SESSION = CodexSession()
STORE = ConversationStore(CONVERSATIONS_PATH)
PROGRESS = ProgressTracker()
TASKS = ChatTaskTracker()


def build_summary(conversation: dict[str, Any]) -> dict[str, Any]:
    messages = conversation["messages"]
    last_text = messages[-1]["text"] if messages else ""
    return {
        "id": conversation["id"],
        "title": conversation["title"],
        "updatedAt": conversation["updatedAt"],
        "createdAt": conversation["createdAt"],
        "messageCount": len(messages),
        "preview": last_text[:40] + ("..." if len(last_text) > 40 else ""),
    }


def run_chat_task(conversation_id: str, message: str) -> None:
    try:
        log_event(
            f"chat task start conversation_id={conversation_id} message_len={len(message)}"
        )
        conversation = STORE.ensure_conversation(conversation_id)
        parsed = maybe_parse_case_export(message)
        if should_use_batch_mode(parsed, message):
            artifact_path = persist_batch_input(conversation["id"], message)
            log_event(
                f"batch input persisted conversation_id={conversation_id} path={artifact_path}"
            )
            TASKS.set_thinking(
                conversation["id"],
                "已识别到超长 Case 表格，正在切换到 Codex 原生批量分析模式...",
            )
            result = SESSION.chat(
                conversation["id"],
                conversation.get("threadId"),
                build_native_batch_prompt(message, artifact_path),
                on_delta=lambda delta: TASKS.append(conversation["id"], delta),
                apply_briefing=False,
            )
        else:
            result = SESSION.chat(
                conversation["id"],
                conversation.get("threadId"),
                message,
                on_delta=lambda delta: TASKS.append(conversation["id"], delta),
            )
        if conversation.get("threadId") != result["threadId"]:
            STORE.set_thread_id(conversation["id"], result["threadId"])
        saved = STORE.append_exchange(conversation["id"], message, result["reply"])
        log_event(
            f"chat task success conversation_id={conversation_id} "
            f"thread_id={result['threadId']} reply_len={len(result['reply'])}"
        )
        TASKS.finish(conversation["id"], result["reply"])
        summaries = STORE.list_conversations()
        latest_summary = next(
            (summary for summary in summaries if summary["id"] == conversation["id"]),
            build_summary(saved),
        )
        with TASKS._lock:
            TASKS._tasks[conversation["id"]]["conversation"] = saved
            TASKS._tasks[conversation["id"]]["summary"] = latest_summary
    except Exception as exc:
        log_event(f"chat task failure conversation_id={conversation_id} error={exc}")
        TASKS.fail(conversation_id, str(exc))


class OLARequestHandler(BaseHTTPRequestHandler):
    server_version = "OLAAssistant/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if parsed.path == "/api/conversations":
            self._send_json(HTTPStatus.OK, {"data": STORE.list_conversations()})
            return

        if parsed.path == "/api/progress":
            conversation_id = parse_qs(parsed.query).get("conversationId", [None])[0]
            self._send_json(HTTPStatus.OK, PROGRESS.snapshot(conversation_id))
            return

        if parsed.path == "/api/chat/status":
            conversation_id = parse_qs(parsed.query).get("conversationId", [None])[0]
            self._send_json(HTTPStatus.OK, TASKS.snapshot(conversation_id))
            return

        if parsed.path.startswith("/api/conversations/"):
            conversation_id = parsed.path.split("/", 3)[-1]
            conversation = STORE.get_conversation(conversation_id)
            if conversation is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "会话不存在"})
                return
            self._send_json(HTTPStatus.OK, {"conversation": conversation})
            return

        if parsed.path == "/assets/logo.png":
            self._send_file(LOGO_PATH, "image/png")
            return

        relative_path = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        file_path = (STATIC_DIR / relative_path).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        content_type = self._guess_content_type(file_path)
        self._send_file(file_path, content_type)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/conversations":
            conversation = STORE.ensure_conversation()
            self._send_json(
                HTTPStatus.OK,
                {
                    "conversation": conversation,
                    "summary": {
                        "id": conversation["id"],
                        "title": conversation["title"],
                        "updatedAt": conversation["updatedAt"],
                        "createdAt": conversation["createdAt"],
                        "messageCount": 0,
                        "preview": "",
                    },
                },
            )
            return

        if parsed.path != "/api/chat":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return

        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Body must be valid JSON"})
            return

        message = (payload.get("message") or "").strip()
        conversation_id = payload.get("conversationId")
        if not message:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message 不能为空"})
            return

        try:
            conversation = STORE.ensure_conversation(conversation_id)
            TASKS.start(conversation["id"])
            threading.Thread(
                target=run_chat_task,
                args=(conversation["id"], message),
                daemon=True,
            ).start()
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": str(exc),
                    "details": list(SESSION._stderr_lines)[-10:],
                },
            )
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "conversationId": conversation["id"],
            },
        )

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/conversations/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        conversation_id = parsed.path.split("/", 3)[-1]
        deleted = STORE.delete_conversation(conversation_id)
        if not deleted:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "会话不存在"})
            return

        self._send_json(HTTPStatus.OK, {"ok": True, "conversationId": conversation_id})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        data = path.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _guess_content_type(path: Path) -> str:
        if path.suffix == ".html":
            return "text/html; charset=utf-8"
        if path.suffix == ".css":
            return "text/css; charset=utf-8"
        if path.suffix == ".js":
            return "application/javascript; charset=utf-8"
        if path.suffix == ".png":
            return "image/png"
        return "application/octet-stream"


def main() -> None:
    load_local_env()
    log_event("ola server boot")
    print(f"Using isolated CODEX_HOME at {OLA_CODEX_HOME}")
    provider_id = active_model_provider_id()
    env_key = active_provider_env_key()
    if provider_id:
        print(f"Active model provider: {provider_id}")
        log_event(f"active model provider: {provider_id}")
    if env_key:
        print(f"Provider env key present: {'yes' if os.environ.get(env_key) else 'no'} ({env_key})")
        log_event(
            f"provider env key present: {'yes' if os.environ.get(env_key) else 'no'} ({env_key})"
        )
    print(f"OLA demo server starting at http://{HOST}:{PORT}")
    print("首次请求时会自动拉起 codex app-server，第一次可能会稍慢。")
    httpd = ThreadingHTTPServer((HOST, PORT), OLARequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.close()
        httpd.server_close()


if __name__ == "__main__":
    main()
