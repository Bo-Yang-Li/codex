#!/usr/bin/env python3
"""Thin Codex app-server adapter for OLA.

This module is the boundary between OLA product logic and Codex transport
semantics. OLA should keep product-specific behavior in `server.py` and route
all app-server protocol handling through this adapter so we can later swap the
implementation to Codex native in-process/app-server-client primitives with
minimal surface churn.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable, Deque, Optional


def load_ola_config_text(ola_codex_home: Path) -> str:
    config_path = ola_codex_home / "config.toml"
    if not config_path.exists():
        return ""
    return config_path.read_text(encoding="utf-8", errors="ignore")


def active_model_provider_id(ola_codex_home: Path) -> Optional[str]:
    config_text = load_ola_config_text(ola_codex_home)
    match = re.search(r'^model_provider\s*=\s*"([^"]+)"', config_text, flags=re.MULTILINE)
    return match.group(1) if match else None


def active_provider_env_key(ola_codex_home: Path) -> Optional[str]:
    provider_id = active_model_provider_id(ola_codex_home)
    if provider_id is None:
        return None
    config_text = load_ola_config_text(ola_codex_home)
    pattern = (
        r"^\[model_providers\."
        + re.escape(provider_id)
        + r'\]\n(?:(?!^\[).*\n)*?env_key\s*=\s*"([^"]+)"'
    )
    match = re.search(pattern, config_text, flags=re.MULTILINE)
    return match.group(1) if match else None


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


def provider_streaming_override(
    ola_codex_home: Path,
    provider_id: Optional[str],
    enabled: bool,
) -> list[str]:
    if provider_id in {None, ""}:
        return []

    config_text = load_ola_config_text(ola_codex_home)
    pattern = (
        r"^\[model_providers\."
        + re.escape(provider_id)
        + r"\]\n(?P<body>(?:(?!^\[).*\n)*)"
    )
    match = re.search(pattern, config_text, flags=re.MULTILINE)
    if match is None:
        return []

    body = match.group("body")
    body = re.sub(
        r"(?m)^responses_streaming\s*=.*\n?",
        "",
        body,
    ).rstrip()
    provider_inline = "{ " + ", ".join(
        line.strip() for line in body.splitlines() if line.strip()
    )
    if provider_inline != "{ ":
        provider_inline += ", "
    provider_inline += f"responses_streaming = {str(enabled).lower()} }}"
    return [
        "--config",
        f"model_providers.{provider_id}={provider_inline}",
    ]


def normalize_message_phase(phase: Any) -> Optional[str]:
    if not isinstance(phase, str):
        return None
    normalized = phase.strip().lower()
    if normalized in {"commentary", "final_answer"}:
        return normalized
    return None


class CodexRpcError(RuntimeError):
    pass


class CodexAppServerAdapter:
    """OLA-facing adapter for Codex app-server over stdio.

    This intentionally keeps the current subprocess transport behind one class.
    A later migration can replace the internals with Codex native in-process
    primitives without forcing OLA product routes, storage, or frontend state
    to learn app-server protocol details.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        codex_rs_dir: Path,
        ola_codex_home: Path,
        notification_timeout_seconds: int,
        log_event: Callable[[str], None],
        config_overrides: Optional[list[str]] = None,
    ) -> None:
        self._repo_root = repo_root
        self._codex_rs_dir = codex_rs_dir
        self._ola_codex_home = ola_codex_home
        self._notification_timeout_seconds = notification_timeout_seconds
        self._log_event = log_event
        self._process: Optional[subprocess.Popen[str]] = None
        self._request_id = 0
        self._pending: dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._notifications: Deque[dict[str, Any]] = deque()
        self._server_requests: Deque[dict[str, Any]] = deque()
        self._condition = threading.Condition()
        self._interaction_lock = threading.RLock()
        self._stderr_lines: Deque[str] = deque(maxlen=50)
        self._initialized = False
        self._config_overrides = list(config_overrides or [])

    @property
    def stderr_lines(self) -> list[str]:
        return list(self._stderr_lines)

    def active_model_provider_id(self) -> Optional[str]:
        return active_model_provider_id(self._ola_codex_home)

    def active_provider_env_key(self) -> Optional[str]:
        return active_provider_env_key(self._ola_codex_home)

    def start(self) -> None:
        start_time = time.time()
        cargo = discover_cargo()
        if cargo is None:
            raise RuntimeError(
                '找不到 cargo。请先运行 `source "$HOME/.cargo/env"`，'
                "或者确认 Rust 已正确安装。"
            )

        if self._process is not None:
            return

        self._validate_provider_env()
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("CODEX_HOME", str(self._ola_codex_home))
        env_key = self.active_provider_env_key()
        self._log_event(
            "starting codex native bridge"
            f" provider={self.active_model_provider_id()}"
            f" env_key={env_key}"
            f" env_present={'yes' if env_key and env.get(env_key) else 'no'}"
        )
        self._process = subprocess.Popen(
            [
                cargo,
                "run",
                "-p",
                "codex-exec",
                "--bin",
                "codex-exec-in-process-bridge",
                "--",
                *self._config_overrides,
            ],
            cwd=str(self._codex_rs_dir),
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
        self._log_event(f"app-server ready startup_ms={int((time.time() - start_time) * 1000)}")

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
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
        apply_briefing: bool = True,
        notification_timeout_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        with self._interaction_lock:
            self.start()
            thread_id = self._ensure_thread(thread_id)
            return self.run_turn(
                thread_id=thread_id,
                message=message,
                on_delta=on_delta,
                on_event=on_event,
                conversation_id=conversation_id,
                apply_briefing=apply_briefing,
                notification_timeout_seconds=notification_timeout_seconds
                or self._notification_timeout_seconds,
            )

    def run_turn(
        self,
        thread_id: Optional[str],
        message: str,
        on_delta: Optional[Callable[[str], None]] = None,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
        conversation_id: Optional[str] = None,
        apply_briefing: bool = True,
        notification_timeout_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        with self._interaction_lock:
            self.start()
            thread_id = self._ensure_thread(thread_id)
            run_turn_started_at = time.time()
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
            timeout_seconds = notification_timeout_seconds or self._notification_timeout_seconds
            item_deltas: dict[str, str] = {}
            final_messages: list[str] = []
            unknown_phase_messages: list[str] = []
            truncated_by_max_tokens = False
            notification_counts: Counter[str] = Counter()
            item_started_counts: Counter[str] = Counter()
            item_completed_counts: Counter[str] = Counter()
            item_completed_phase_counts: Counter[str] = Counter()
            retry_count = 0
            first_notification_at: Optional[float] = None
            first_output_at: Optional[float] = None
            last_notification_at: Optional[float] = None
            total_streamed_chars = 0

            self._log_event(
                "turn started "
                + json.dumps(
                    {
                        "conversationId": conversation_id,
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "messageLen": len(message),
                        "preparedMessageLen": len(prepared_message),
                        "applyBriefing": apply_briefing,
                        "notificationTimeoutSeconds": timeout_seconds,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

            while True:
                self._drain_server_requests()
                try:
                    notification = self._next_notification(timeout_seconds=timeout_seconds)
                except RuntimeError as exc:
                    partial_reply = "\n\n".join(final_messages).strip() or "\n\n".join(
                        unknown_phase_messages
                    ).strip()
                    if "等待 Codex 通知超时" in str(exc) and partial_reply:
                        truncated_by_max_tokens = True
                        self._log_event(
                            "notification timeout but partial output exists; "
                            f"turn_id={turn_id} partial_len={len(partial_reply)}"
                        )
                        break
                    raise
                method = notification.get("method")
                params = notification.get("params") or {}
                now = time.time()

                notification_counts[method] += 1
                last_notification_at = now
                if first_notification_at is None:
                    first_notification_at = now
                    self._log_event(
                        "turn first notification "
                        + json.dumps(
                            {
                                "conversationId": conversation_id,
                                "turnId": turn_id,
                                "method": method,
                                "elapsedMs": int((now - run_turn_started_at) * 1000),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )

                if params.get("turnId") not in {None, turn_id}:
                    continue

                if on_event is not None:
                    on_event(method, params)

                if method == "item/agentMessage/delta":
                    item_id = params.get("itemId") or "unknown"
                    delta = params.get("delta", "")
                    if delta:
                        if first_output_at is None:
                            first_output_at = now
                            self._log_event(
                                "turn first output "
                                + json.dumps(
                                    {
                                        "conversationId": conversation_id,
                                        "turnId": turn_id,
                                        "method": method,
                                        "elapsedMs": int((now - run_turn_started_at) * 1000),
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                            )
                        total_streamed_chars += len(delta)
                        item_deltas[item_id] = item_deltas.get(item_id, "") + delta
                        if on_delta is not None:
                            on_delta(delta)
                elif method == "item/started":
                    item_type = ((params.get("item") or {}).get("type")) or "unknown"
                    item_started_counts[item_type] += 1
                elif method == "item/completed":
                    item = params.get("item") or {}
                    item_type = item.get("type") or "unknown"
                    item_completed_counts[item_type] += 1
                    if item_type == "agentMessage":
                        if first_output_at is None:
                            first_output_at = now
                            self._log_event(
                                "turn first output "
                                + json.dumps(
                                    {
                                        "conversationId": conversation_id,
                                        "turnId": turn_id,
                                        "method": method,
                                        "elapsedMs": int((now - run_turn_started_at) * 1000),
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                            )
                        phase = normalize_message_phase(item.get("phase"))
                        item_completed_phase_counts[phase or "unknown"] += 1
                        item_id = item.get("id") or params.get("itemId") or "unknown"
                        message_text = (item.get("text") or item_deltas.get(item_id, "")).strip()
                        if not message_text:
                            continue
                        if phase == "final_answer":
                            final_messages.append(message_text)
                        elif phase is None:
                            unknown_phase_messages.append(message_text)
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
                        retry_count += 1
                        self._log_event(
                            "turn retry notification "
                            + json.dumps(
                                {
                                    "conversationId": conversation_id,
                                    "turnId": turn_id,
                                    "retryCount": retry_count,
                                    "elapsedMs": int((now - run_turn_started_at) * 1000),
                                    "message": (params.get("error") or {}).get("message", ""),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        )
                        continue
                    error = (params.get("error") or {}).get("message")
                    if error:
                        visible_reply = "\n\n".join(final_messages).strip() or "\n\n".join(
                            unknown_phase_messages
                        ).strip()
                        if "max_output_tokens" in error and visible_reply:
                            truncated_by_max_tokens = True
                            self._log_event(
                                "turn hit max_output_tokens but partial output exists; "
                                f"turn_id={turn_id} partial_len={len(visible_reply)}"
                            )
                            break
                        raise CodexRpcError(error)

            reply = "\n\n".join(final_messages).strip() or "\n\n".join(
                unknown_phase_messages
            ).strip()
            if not reply:
                reply = "本轮处理在生成最终答案前中断，未返回可安全展示的最终文本。"
            elif truncated_by_max_tokens:
                reply += "\n\n[已返回当前可用的部分输出，后续响应未完整结束]"

            self._log_event(
                "turn metrics "
                + json.dumps(
                    {
                        "conversationId": conversation_id,
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "durationMs": int((time.time() - run_turn_started_at) * 1000),
                        "firstNotificationMs": (
                            int((first_notification_at - run_turn_started_at) * 1000)
                            if first_notification_at is not None
                            else None
                        ),
                        "firstOutputMs": (
                            int((first_output_at - run_turn_started_at) * 1000)
                            if first_output_at is not None
                            else None
                        ),
                        "lastNotificationMs": (
                            int((last_notification_at - run_turn_started_at) * 1000)
                            if last_notification_at is not None
                            else None
                        ),
                        "notificationCounts": dict(notification_counts),
                        "itemStartedCounts": dict(item_started_counts),
                        "itemCompletedCounts": dict(item_completed_counts),
                        "itemCompletedPhaseCounts": dict(item_completed_phase_counts),
                        "retryCount": retry_count,
                        "totalStreamedChars": total_streamed_chars,
                        "replyLen": len(reply),
                        "truncatedByMaxTokens": truncated_by_max_tokens,
                        "finalMessageCount": len(final_messages),
                        "unknownPhaseMessageCount": len(unknown_phase_messages),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

            return {
                "reply": reply,
                "threadId": thread_id,
                "turnId": turn_id,
                "conversationId": conversation_id,
            }

    def _validate_provider_env(self) -> None:
        provider_id = self.active_model_provider_id()
        env_key = self.active_provider_env_key()
        if provider_id in {None, "", "openai"} or env_key is None:
            return
        if os.environ.get(env_key):
            return
        raise RuntimeError(
            f"当前 OLA 使用自定义 provider `{provider_id}`，但缺少环境变量 `{env_key}`。"
            f"请先 `export {env_key}=...` 后再启动服务。"
        )

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
                "cwd": str(self._repo_root),
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
                self._log_event(f"app-server stderr: {line}")

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
        request_started_at = time.time()
        self._log_event(f"rpc request method={method} id={request_id}")

        try:
            response = response_queue.get(timeout=180)
        except queue.Empty as exc:
            self._log_event(
                f"rpc timeout method={method} id={request_id} "
                f"duration_ms={int((time.time() - request_started_at) * 1000)} "
                f"recent_stderr={list(self._stderr_lines)[-5:]}"
            )
            raise RuntimeError(
                f"等待 Codex 响应 `{method}` 超时。最近日志：{list(self._stderr_lines)[-5:]}"
            ) from exc
        finally:
            with self._condition:
                self._pending.pop(request_id, None)

        if "error" in response:
            message = response["error"].get("message", "Unknown error")
            self._log_event(
                f"rpc error method={method} id={request_id} "
                f"duration_ms={int((time.time() - request_started_at) * 1000)} message={message}"
            )
            raise CodexRpcError(message)

        self._log_event(
            f"rpc success method={method} id={request_id} "
            f"duration_ms={int((time.time() - request_started_at) * 1000)}"
        )
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

    def _next_notification(self, timeout_seconds: int) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while True:
            self._drain_server_requests()
            with self._condition:
                if self._notifications:
                    return self._notifications.popleft()

                process = self._process
                if process is not None and process.poll() is not None:
                    self._log_event(
                        f"app-server exited returncode={process.returncode} recent_stderr={list(self._stderr_lines)[-10:]}"
                    )
                    raise RuntimeError(
                        f"Codex app-server 已退出，退出码 {process.returncode}。"
                        f"最近日志：{list(self._stderr_lines)[-10:]}"
                    )

                remaining = deadline - time.time()
                if remaining <= 0:
                    self._log_event(f"notification timeout recent_stderr={list(self._stderr_lines)[-10:]}")
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
