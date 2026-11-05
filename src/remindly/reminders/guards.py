from __future__ import annotations

import re
from dataclasses import replace

from remindly.reminders.models import ParseResult, ReminderDraft

# 這些是未由時間文法驗證的條件；不能因時間已確定就清除語意追問。
SEMANTIC_CONDITION_RE = re.compile(
    r"(?:^|[,，;；（(]|但|且|並且)\s*"
    r"(?:如果|假如|除非|只在|只限|僅在|限於|"
    r"(?:若|只要|等|待|有空)(?=[^,，;；）)]*(?:就|才|再))|有空時|"
    r"(?:排除|不含|避開)(?:國定假日|工作日|平日|假日|週末|農曆)(?!的)|"
    r"(?:每(?:個)?|逢)(?:工作日|平日|假日|週末|農曆)|"
    r"(?:之前|之後|以前|以後|左右)(?!的))"
)
# 時間界線需接動作，避免把方向、製作等複合詞的前綴當成界線。
BOUNDARY_ACTION_RE = re.compile(
    r"^[前後](?:再|才|就|要)?(?:交|繳|寄|送|領|買|還|完成|提交|支付|付款|出門|出發|到達|取件)(?!的)"
)

CORRECTION_RE = re.compile(
    r"^(?:請\s*)?(?:(?:把\s*)?(?:提醒)?(?:時間|日期)\s*)?"
    r"(?:改(?:成|到|為)|換(?:成|到)|調(?:到|整(?:成|到|為))|"
    r"延(?:到|至)|(?:延後|提早|提前)(?:到|至|(?=\s*[\d半零〇一二兩两三四五六七八九十]))|"
    r"不是|不對(?=$|[\s，,:：]))\s*"
)
VAGUE_TIME_RE = re.compile(
    r"(?:(?:上|下)(?:班|課)|起床|睡覺)(?:之?前|之?後)|"
    r"(?:月|年)(?:初|底|末)(?:之?前|之?後)?|"
    r"(?:有空|方便)(?:時|的時候)|(?:這個|下個|本)?週末|工作日|平日|假日"
)

CONDITION_QUESTION = "請確認額外提醒條件，並一次提供一筆明確提醒。"


def has_condition(title: str | None) -> bool:
    return bool(SEMANTIC_CONDITION_RE.search(title or "") or BOUNDARY_ACTION_RE.match(title or ""))


def preserve_conditions(
    result: ParseResult,
    context: ReminderDraft | None = None,
    source_question: str | None = None,
) -> ParseResult:
    existing_condition = bool(
        context
        and "clarification" in context.missing_fields
        and result.raw.get("intent") == "update"
    )
    if existing_condition or source_question is not None:
        prior_question = str(
            (
                context.parse_result.get("condition_question")
                or context.parse_result.get("question")
                or "請確認先前的條件。"
            )
            if existing_condition
            else source_question
        )
        previous_question = (
            context.parse_result.get("question") if existing_condition else source_question
        )
        question = result.raw.get("question")
        if not question or question == previous_question:
            question = prior_question
        elif not question.startswith(prior_question):
            question = f"{prior_question[:220]}\n{question}".strip()[:300]
        result = replace(
            result,
            raw={**result.raw, "question": question, "condition_question": prior_question},
            missing_fields=tuple(dict.fromkeys((*result.missing_fields, "clarification"))),
        )
    return result
