#!/usr/bin/env python3
import csv
import io
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional


TABLE_HEADER_MARKER = "Case序号\tCaseID\t"
BATCH_CASE_THRESHOLD = 25
CHUNK_TARGET_CHARS = 30000
CHUNK_MIN_CASES = 16
CHUNK_MAX_CASES = 40
NATIVE_MAX_PARALLEL_AGENTS = 3


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
    average_chunk_size = math.ceil(case_count / len(chunks)) if chunks else case_count
    suggested_parallel_agents = min(NATIVE_MAX_PARALLEL_AGENTS, len(chunks)) or 1
    return {
        "caseCount": case_count,
        "chunkCount": len(chunks),
        "suggestedParallelAgents": suggested_parallel_agents,
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


def build_chunk_manifest(chunks: list[list[dict[str, str]]]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    start_index = 1
    for chunk_index, chunk in enumerate(chunks, start=1):
        case_ids = [case.get("CaseID", "") for case in chunk]
        end_index = start_index + len(chunk) - 1
        manifest.append(
            {
                "chunkIndex": chunk_index,
                "startCaseIndex": start_index,
                "endCaseIndex": end_index,
                "caseCount": len(chunk),
                "firstCaseId": case_ids[0] if case_ids else "",
                "lastCaseId": case_ids[-1] if case_ids else "",
            }
        )
        start_index = end_index + 1
    return manifest


def build_native_batch_prompt(raw_message: str, artifact_path: Path) -> str:
    parsed = maybe_parse_case_export(raw_message)
    if parsed is None:
        return raw_message

    cases = parsed["cases"]
    goal = parsed["instruction"] or "请对这批 Case 做整体分析，提炼高频诉求、问题模式、人工转接原因、未解决风险和优化建议。"
    chunks = chunk_cases(cases)
    decision = build_batch_decision(len(cases), chunks)
    metrics = compute_case_metrics(cases)
    chunk_manifest = build_chunk_manifest(chunks)

    metrics_payload = json.dumps(metrics, ensure_ascii=False, indent=2)
    decision_payload = json.dumps(decision, ensure_ascii=False, indent=2)
    chunk_manifest_payload = json.dumps(chunk_manifest, ensure_ascii=False, indent=2)

    return f"""
你现在正在 OLA 的批量会话分析场景中工作。

这不是让外部 Python 再做线程池并发的旧方案。用户已经明确要求：这一轮要直接使用 Codex 原生能力来完成，不要在回答里自称“我会让外部系统并发处理”，而是由你自己在当前线程里按需使用 Codex 原生 sub-agent / wait / close_agent 等协作能力；如果上下文变长，也优先依赖 Codex 原生的上下文压缩能力。

原始超长输入已经写入本地文件，避免单条消息超出长度限制：
- 批量输入文件：`{artifact_path}`
- 文件内容格式：前半段是用户指令，随后是 TSV 表格，表头从 `Case序号\tCaseID\t` 开始
- 你应优先通过工具读取这个文件，而不是假设我会继续把全文贴进消息里

请遵守以下执行原则：
1. 先快速判断是否真的需要调用原生子 agent；只有在对当前这批超长 Case 做分治明显更稳、更快时才调用。
2. 如果要调用，使用 Codex 原生 `spawn_agent`、`send_input`、`wait`、`close_agent`，不要自己假装并发，也不要要求外部包装层替你并行。
3. 子 agent 的任务必须是清晰、互不重叠的分片分析，例如按连续 Case 范围拆分；并行度控制在 2 到 {decision["suggestedParallelAgents"]} 个之间，不要无上限扩张。
4. 主线程在等待子 agent 期间，优先整理结构化统计、交叉归纳主题、准备最终汇总框架。
5. 最终输出必须是一份可直接给业务复盘阅读的中文报告，不要输出 JSON，不要暴露工具调用细节。
6. 禁止输出你的执行过程、接下来要做什么、已经读了什么、先判断什么、再补什么这类工作日志；只允许输出最终结论与建议。

为了帮助你判断是否要分治，系统已经基于原始 Case 表格做了轻量预分析：

建议切分信息：
{decision_payload}

全量结构化统计：
{metrics_payload}

建议分片清单：
{chunk_manifest_payload}

最终报告至少包含：
1. 总体结论
2. 主要用户诉求 Top 5
3. 主要 Issue 标签 / 问题类型
4. 转人工与未解决问题分析
5. 典型 Case 观察
6. 可执行优化建议
7. 本次分析策略说明

额外要求：
- 明确写出本次识别的 Case 总量。
- 如果你实际调用了原生子 agent，要在“本次分析策略说明”里简洁说明你为什么这样拆分。
- 如果你判断不需要调用子 agent，也要说明原因。
- 结论尽量量化，优先引用上面的结构化统计。
- 不要大段复述原始 Case 文本。
- 直接从“总体结论”开始写，不要出现“我先……”“接着……”“结论：”之前的过程性铺垫。
- 如果你引用抽样观察，只保留结论，不要解释你是如何抽样的。
""".strip()
