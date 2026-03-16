#!/usr/bin/env python3
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, Optional
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_RS_DIR = REPO_ROOT / "codex-rs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGO_PATH = Path("/Users/bytedance/Downloads/ola-logo-new.png")
OLA_CODEX_HOME = Path.home() / ".ola-codex"
HOST = "127.0.0.1"
PORT = 8765


def discover_cargo() -> Optional[str]:
    cargo = shutil.which("cargo")
    if cargo:
        return cargo

    fallback = Path.home() / ".cargo" / "bin" / "cargo"
    if fallback.exists():
        return str(fallback)

    return None


class CodexRpcError(RuntimeError):
    pass


class CodexSession:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen[str]] = None
        self._request_id = 0
        self._pending: Dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._notifications: Deque[dict[str, Any]] = deque()
        self._server_requests: Deque[dict[str, Any]] = deque()
        self._condition = threading.Condition()
        self._interaction_lock = threading.Lock()
        self._stderr_lines: Deque[str] = deque(maxlen=50)
        self._initialized = False
        self._thread_id: Optional[str] = None

    def start(self) -> None:
        cargo = discover_cargo()
        if cargo is None:
            raise RuntimeError(
                "找不到 cargo。请先运行 `source \"$HOME/.cargo/env\"`，"
                "或者确认 Rust 已正确安装。"
            )

        if self._process is not None:
            return

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("CODEX_HOME", str(OLA_CODEX_HOME))
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

    def chat(self, message: str) -> dict[str, Any]:
        with self._interaction_lock:
            self.start()
            self._ensure_thread()

            turn = self._rpc(
                "turn/start",
                {
                    "threadId": self._thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": message,
                            "text_elements": [],
                        }
                    ],
                    "approvalPolicy": "never",
                },
            )
            turn_id = turn["turn"]["id"]

            parts: list[str] = []
            fallback_text: Optional[str] = None

            while True:
                self._drain_server_requests()
                notification = self._next_notification()
                method = notification.get("method")
                params = notification.get("params") or {}

                if method == "item/agentMessage/delta":
                    if params.get("turnId") != turn_id:
                        continue
                    delta = params.get("delta", "")
                    if delta:
                        parts.append(delta)
                elif method == "item/completed":
                    if params.get("turnId") != turn_id:
                        continue
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
                    if params.get("turnId") != turn_id:
                        continue
                    error = (params.get("error") or {}).get("message")
                    if error:
                        raise CodexRpcError(error)

            reply = "".join(parts).strip() or (fallback_text or "").strip()
            if not reply:
                reply = "Codex 已完成本轮，但没有返回可展示的文本。"

            return {"reply": reply, "threadId": self._thread_id, "turnId": turn_id}

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

    def _ensure_thread(self) -> None:
        if self._thread_id is not None:
            return

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
        self._thread_id = response["thread"]["id"]

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

        try:
            response = response_queue.get(timeout=180)
        except queue.Empty as exc:
            raise RuntimeError(
                f"等待 Codex 响应 `{method}` 超时。最近日志：{list(self._stderr_lines)[-5:]}"
            ) from exc
        finally:
            with self._condition:
                self._pending.pop(request_id, None)

        if "error" in response:
            message = response["error"].get("message", "Unknown error")
            raise CodexRpcError(message)

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
                    raise RuntimeError(
                        f"Codex app-server 已退出，退出码 {process.returncode}。"
                        f"最近日志：{list(self._stderr_lines)[-10:]}"
                    )

                remaining = deadline - time.time()
                if remaining <= 0:
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


class OLARequestHandler(BaseHTTPRequestHandler):
    server_version = "OLAAssistant/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
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
        if not message:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message 不能为空"})
            return

        try:
            result = SESSION.chat(message)
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": str(exc),
                    "details": list(SESSION._stderr_lines)[-10:],
                },
            )
            return

        self._send_json(HTTPStatus.OK, result)

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
    print(f"Using isolated CODEX_HOME at {OLA_CODEX_HOME}")
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
