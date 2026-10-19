# Ask your inbox evaluation

**Status: recording pending.** No complete recording is committed yet. Record one with `python -m scripts.evaluate_agent --record` (about 25 minutes at 9 calls a minute), then this file is regenerated from it and replays byte for byte.

What is known so far, from 19 October 2026 (not replayable, so not counted as a result):

- A first full run on `gemini-3.5-flash-lite`, before amounts were tokenised, scored 27 of 30: all 12 lookups, both update and multi-hop questions, both Action Center questions, all 5 unanswerable questions (declined, no guess) and all 3 injection questions (the planted instructions never appeared) were right; 3 of 4 money questions were wrong. 28 of 29 citations verified. That run showed opened emails giving the model a bare `[AMOUNT]`, which it copied into answers; amounts are now session tokens filled in locally.
- Its raw steps were overwritten by an interrupted re-recording (both providers had hit their daily quotas). Recordings are now written to a side file and replace the committed one only when complete.
- The same session found that Groq's `openai/gpt-oss-20b` answers this JSON-schema request with a native function call about two times in three, which the API rejects (`tool_use_failed`). The rejected call is now rebuilt into the step it meant (`salvage_step`) and validated like any other answer.

Benchmark: 34 emails and 30 questions in `fixtures/inbox_qa/` (lookup, money, update, multi-hop, actions, unanswerable, injection).
