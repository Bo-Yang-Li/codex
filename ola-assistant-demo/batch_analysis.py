#!/usr/bin/env python3
import csv
import io
import json
import math
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional


TABLE_HEADER_MARKER = "Case序号\tCaseID\t"
BATCH_CASE_THRESHOLD = 25
CHUNK_TARGET_CHARS = 14000
CHUNK_MIN_CASES = 8
CHUNK_MAX_CASES = 18
MAX_PARALLEL_WORKERS = 4


def maybe_parse_case_export(raw_text: str) -> Optional[dict[str, Any]]:
    header_index = raw_text.find(TABLE_HEADER_MARKER)
    if header_index < 0:
        return None

    instruction = raw_text[:header_index].strip()
    table_text = raw_text[header_index:].strip()
    reader = csv.DictReader(
        io.StringIO(table_text),
        delimiter="\t",
        quotechar='"',
    )

    cases: list[dict[str, str]] = []
    for raw_row in reader:
        row = {(key or "").strip(): (value or "").strip() for key, value in raw_row.items()}
        if not row.get("CaseID"):
            continue
        cases.append(row)

    if not cases:
        return None

    return {
        "instruction": instruction,
        "cases": cases,
    }


def should_use_batch_mode(parsed: Optional[dict[str, Any]], raw_text: str) -> bool:
    if parsed is None:
        return False
    case_count = len(parsed["cases"])
    return case_count >= BATCH_CASE_THRESHOLD or len(raw_text) >= 20000


def chunk_cases(cases: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    chunks: list[list[dict[str, str]]] = []
    current_chunk: list[dict[str, str]] = []
    current_chars = 0

    for case in cases:
        payload = build_case_payload(case)
        case_chars = len(payload)
        should_split = (
            current_chunk
            and (
                len(current_chunk) >= CHUNK_MAX_CASES
                or (
                    current_chars + case_chars > CHUNK_TARGET_CHARS
                    and len(current_chunk) >= CHUNK_MIN_CASES
                )
            )
        )
        if should_split:
            chunks.append(current_chunk)
            current_chunk = []
            current_chars = 0

        current_chunk.append(case)
        current_chars += case_chars

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def build_batch_decision(case_count: int, chunks: list[list[dict[str, str]]]) -> dict[str, Any]:
    chunk_sizes = [len(chunk) for chunk in chunks] or [0]
    worker_count = min(MAX_PARALLEL_WORKERS, len(chunks)) or 1
    average_chunk_size = math.ceil(case_count / len(chunks)) if chunks else case_count
    return {
        "caseCount": case_count,
        "chunkCount": len(chunks),
        "workerCount": worker_count,
        "minCasesPerChunk": min(chunk_sizes),
        "maxCasesPerChunk": max(chunk_sizes),
        "avgCasesPerChunk": average_chunk_size,
    }


def build_case_payload(case: dict[str, str]) -> str:
    content = compact_text(case.get("会话内容", ""), 900)
    summary = compact_text(case.get("会话总结", ""), 400)
    flow = compact_text(case.get("对话流", ""), 180)
    return "\n".join(
        [
            f"CaseID: {case.get('CaseID', '')}",
            f"会话总结: {summary}",
            f"Issue标签: {case.get('Issue标签', '')}",
            f"服务阶段: {case.get('服务阶段', '')}",
            f"转人工: {case.get('转人工', '')}",
            f"是否解决: {case.get('是否解决', '')}",
            f"满意度: {case.get('满意度', '')}",
            f"是否邀评: {case.get('是否邀评', '')}",
            f"人工是否已解决: {case.get('人工是否已解决', '')}",
            f"是否被风控: {case.get('是否被风控', '')}",
            f"对话流: {flow}",
            f"会话内容摘要: {content}",
        ]
    )


def compact_text(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def compute_case_metrics(cases: list[dict[str, str]]) -> dict[str, Any]:
    def count_field(name: str) -> dict[str, int]:
        counter = Counter(case.get(name, "未知") or "未知" for case in cases)
        return dict(counter.most_common())

    issue_counter: Counter[str] = Counter()
    for case in cases:
        labels = case.get("Issue标签", "")
        parts = [part.strip() for part in re.split(r"[、,，;/]", labels) if part.strip()]
        if not parts and labels.strip():
            parts = [labels.strip()]
        issue_counter.update(parts)

    return {
        "transfer": count_field("转人工"),
        "resolved": count_field("是否解决"),
        "satisfaction": count_field("满意度"),
        "inviteReview": count_field("是否邀评"),
        "serviceStage": count_field("服务阶段"),
        "manualResolved": count_field("人工是否已解决"),
        "riskControl": count_field("是否被风控"),
        "olaConversation": count_field("是否Ola会话"),
        "topIssueTags": dict(issue_counter.most_common(12)),
    }


def build_chunk_prompt(
    goal: str,
    cases: list[dict[str, str]],
    chunk_index: int,
    total_chunks: int,
) -> str:
    case_text = "\n\n".join(build_case_payload(case) for case in cases)
    return f"""
你是一名资深客服质检与运营分析专家。现在你会拿到一批客服会话 Case，请只分析这一个分片。

用户希望你完成的总目标：
{goal}

当前分片：
- 分片编号：{chunk_index}/{total_chunks}
- 分片内 Case 数：{len(cases)}

请基于下面这些 Case，提炼这个分片里的高频诉求、问题模式、人工转接原因、未解决风险点和有代表性的案例。
请务必只返回 JSON，不要输出 Markdown，不要输出解释文字。

JSON 结构必须是：
{{
  "chunkSummary": "一段 80-150 字的中文总结",
  "topThemes": [
    {{
      "name": "主题名称",
      "evidenceCaseIds": ["caseId1", "caseId2"],
      "summary": "这个主题的简述"
    }}
  ],
  "transferReasons": ["转人工常见原因1", "转人工常见原因2"],
  "unresolvedRisks": ["未解决风险1", "未解决风险2"],
  "representativeCases": [
    {{
      "caseId": "3840554897",
      "reason": "为什么有代表性",
      "summary": "一句话概述"
    }}
  ],
  "notableFindings": ["值得关注的洞察1", "值得关注的洞察2"]
}}

以下是分片 Case：
{case_text}
""".strip()


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return json.loads(stripped[start : end + 1])

    raise ValueError("No JSON object found in worker reply")


def build_final_prompt(
    goal: str,
    metrics: dict[str, Any],
    decision: dict[str, Any],
    chunk_results: list[dict[str, Any]],
) -> str:
    result_payload = json.dumps(chunk_results, ensure_ascii=False, indent=2)
    metrics_payload = json.dumps(metrics, ensure_ascii=False, indent=2)
    decision_payload = json.dumps(decision, ensure_ascii=False, indent=2)

    return f"""
你是一名客服运营分析负责人。现在已经有多个子 Agent 分别完成了 Case 分片分析，请你汇总成一份中文总报告。

用户原始目标：
{goal}

系统切分与并行策略：
{decision_payload}

全量结构化统计：
{metrics_payload}

各分片子 Agent 的分析结果：
{result_payload}

请输出一份清晰、可执行、适合业务复盘阅读的中文报告，至少包含以下部分：
1. 总体结论
2. 主要用户诉求 Top 5
3. 主要 Issue 标签 / 问题类型
4. 转人工与未解决问题分析
5. 典型 Case 观察
6. 可执行优化建议
7. 本次并行分析策略说明

要求：
- 明确写出本次识别的 Case 总量、分片数量、并行 worker 数
- 说明为什么不是“一个 Agent 50 个 Case 固定处理”，而是按上下文长度动态切分
- 结论尽量量化，优先引用结构化统计
- 中文输出，不要 JSON
""".strip()


class BatchCaseAnalyzer:
    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def analyze(
        self,
        conversation_id: str,
        raw_message: str,
        progress: Any,
        tasks: Any,
    ) -> dict[str, Any]:
        parsed = maybe_parse_case_export(raw_message)
        if parsed is None:
            raise ValueError("没有识别到可批量分析的 Case 表格。")

        cases = parsed["cases"]
        instruction = parsed["instruction"]
        goal = instruction or "请对这批 Case 做整体分析，提炼高频诉求、问题模式、人工转接原因、未解决风险和优化建议。"
        chunks = chunk_cases(cases)
        decision = build_batch_decision(len(cases), chunks)
        metrics = compute_case_metrics(cases)

        progress.start_batch(conversation_id, decision["chunkCount"], decision["workerCount"])
        tasks.set_thinking(
            conversation_id,
            "已识别到超长 Case 表格，正在自动切分任务。"
            f"本次共识别 {decision['caseCount']} 个 Case，"
            f"切成 {decision['chunkCount']} 个分片，"
            f"每个子 Agent 约处理 {decision['minCasesPerChunk']}~{decision['maxCasesPerChunk']} 个 Case，"
            f"并行度 {decision['workerCount']}。",
        )

        chunk_results = self._run_workers(conversation_id, goal, chunks, decision, progress, tasks)

        progress.mark_batch_merging(conversation_id)
        tasks.set_thinking(
            conversation_id,
            f"子 Agent 已全部完成，正在汇总 {decision['chunkCount']} 个分片的结果...",
        )

        final_session = self._session_factory()
        try:
            final_result = final_session.run_turn(
                None,
                build_final_prompt(goal, metrics, decision, chunk_results),
                on_delta=lambda delta: tasks.append(conversation_id, delta),
                on_event=lambda method, params: self._handle_final_event(
                    conversation_id,
                    method,
                    params,
                    progress,
                    tasks,
                ),
            )
        finally:
            final_session.close()

        progress.finish(conversation_id)
        return {
            "reply": final_result["reply"],
            "threadId": final_result["threadId"],
            "turnId": final_result["turnId"],
            "conversationId": conversation_id,
            "batchMeta": decision,
        }

    def _run_workers(
        self,
        conversation_id: str,
        goal: str,
        chunks: list[list[dict[str, str]]],
        decision: dict[str, Any],
        progress: Any,
        tasks: Any,
    ) -> list[dict[str, Any]]:
        thread_local = threading.local()
        created_sessions: list[Any] = []
        sessions_lock = threading.Lock()
        results: list[Optional[dict[str, Any]]] = [None] * len(chunks)
        completed = 0

        def get_session() -> Any:
            session = getattr(thread_local, "session", None)
            if session is None:
                session = self._session_factory()
                thread_local.session = session
                with sessions_lock:
                    created_sessions.append(session)
            return session

        def run_chunk(index: int, chunk_cases_list: list[dict[str, str]]) -> dict[str, Any]:
            session = get_session()
            prompt = build_chunk_prompt(goal, chunk_cases_list, index + 1, len(chunks))
            result = session.run_turn(None, prompt)
            try:
                payload = parse_json_object(result["reply"])
            except Exception:
                payload = {
                    "chunkSummary": compact_text(result["reply"], 240),
                    "topThemes": [],
                    "transferReasons": [],
                    "unresolvedRisks": [],
                    "representativeCases": [],
                    "notableFindings": [compact_text(result["reply"], 300)],
                    "rawReply": result["reply"],
                }
            payload["chunkIndex"] = index + 1
            payload["caseCount"] = len(chunk_cases_list)
            return payload

        try:
            with ThreadPoolExecutor(max_workers=decision["workerCount"]) as executor:
                future_map = {
                    executor.submit(run_chunk, index, chunk): index
                    for index, chunk in enumerate(chunks)
                }
                for future in as_completed(future_map):
                    index = future_map[future]
                    results[index] = future.result()
                    completed += 1
                    progress.update_batch_progress(conversation_id, completed, len(chunks))
                    tasks.set_thinking(
                        conversation_id,
                        f"正在并行分析超长 Case，已完成 {completed}/{len(chunks)} 个分片。"
                        f"当前并行 worker 数：{decision['workerCount']}。",
                    )
        finally:
            for session in created_sessions:
                session.close()

        return [result for result in results if result is not None]

    @staticmethod
    def _handle_final_event(
        conversation_id: str,
        method: str,
        params: dict[str, Any],
        progress: Any,
        tasks: Any,
    ) -> None:
        if method == "item/started":
            item_type = (params.get("item") or {}).get("type")
            if item_type in {"plan", "reasoning"}:
                tasks.set_thinking(conversation_id, "正在汇总所有子 Agent 的发现...")
            elif item_type in {
                "commandExecution",
                "fileChange",
                "mcpToolCall",
                "dynamicToolCall",
                "webSearch",
                "imageView",
                "imageGeneration",
                "collabAgentToolCall",
            }:
                tasks.set_thinking(conversation_id, "正在补充上下文并整理最终结论...")
        elif method in {"item/plan/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta"}:
            delta = params.get("delta", "")
            if delta:
                tasks.append_thinking(conversation_id, delta)
        elif method == "item/agentMessage/delta":
            progress.mark_batch_writing(conversation_id)

