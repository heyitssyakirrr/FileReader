from __future__ import annotations


def build_summary_prompt(text: str) -> str:
    """Prompt for bank statement — extract only date + balance rows."""
    return f"""\
You are a data extraction assistant for a Malaysian bank's internal system.
Your task is to read a bank statement and extract ONLY two columns from every transaction row.

=== RULES ===
0. You are in JSON-only mode. Your entire response must be a single JSON object. Stop immediately after the closing brace. No introduction, no explanation, no conclusion.
1. Extract EVERY transaction row — do not skip any and do not duplicate any.
2. "date" can be in any common format (e.g., "01/01/2023", "011224" etc.), but must be returned in YYYY-MM-DD format.
3. "balance" is the running account balance AFTER that transaction (the balance/running-balance column). Return it as a plain numeric string with 2 decimal places, e.g. "1234.56". Never include currency symbols or commas.
4. Return ONLY a raw JSON object same as the OUTPUT format below — no markdown, no code blocks, no explanation.
5. "DO NOT REPEAT" or explain the fields. Just output the JSON object and nothing else.

=== DOCUMENT ===
\"\"\"
{text}
\"\"\"

=== OUTPUT ===
Return ONLY and EXACTLY like this JSON object with no other text:
{{
    "rows": [
        {{"date": "YYYY-MM-DD", "balance": "0.00"}},
    ]
}}
"""
