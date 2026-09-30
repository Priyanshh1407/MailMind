Recording notes (2026-09-30):

- The evaluated Gemini routes are the latest models available to the key: `gemini-3.8-flash` (primary) and `gemini-3.5-flash-lite` (fallback). Both are sent `thinking_level: 'low'`, exactly as production sends to Gemini 3+ models.
- Transient provider trouble that survives all retries (for example `503 UNAVAILABLE`, "high demand") is not scored as a wrong answer. Those rows are left unrecorded and count as "no answer" here; rerunning `--record` retries them.
- `gemini-3.8-flash` (primary) could not be recorded that day: every attempt returned `503 UNAVAILABLE` ("high demand"), and the recorder's breaker stopped the route after 5 consecutive failures. Rerun `python -m scripts.evaluate_cloud --record --routes gemini_primary` to fill it in, then `--replay`.
- Action validation before and after the LLM-04 deadline fix, on these same recordings: Gemini fallback 25/60 → 60/60; Groq 29/63 → 63/63. Every earlier rejection was a deadline-format rule: a date-only deadline given as a plain date, or an unresolved deadline given as `null`.
- Earlier models, not scored:
  - `gemini-2.5-flash` rejects `thinking_level` (`400 INVALID_ARGUMENT`), and its free tier allows 20 requests/day per model (`gemini-2.5-flash.partial.jsonl`, 10 emails).
  - `gemini-1.5-flash` is retired (`404 NOT_FOUND`).
  - A partial run of `gemini-3.5-flash-lite` without `thinking_level` is kept as `gemini-3.5-flash-lite.no-thinking.partial.jsonl`; it is not production-identical, so it is not scored.
