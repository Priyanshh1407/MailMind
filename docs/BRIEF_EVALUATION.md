# Daily brief evaluation

**Status: recording pending.** Both providers had used their daily quotas when this feature was built (19 October 2026). Record with `python -m scripts.evaluate_brief --record` (about 8 minutes); this file is then regenerated from the recording and replays byte for byte.

The benchmark: the Ask-your-inbox mailbox seen on 4 days, each with hand-written items a good brief must mention and the two planted lies it must never repeat. It compares the three-agent pipeline (triage, per-item research with verified quotes, writer citing verified facts) with a single-agent ablation that does everything in one call, under the same verification.

Verified without a provider (`tests/test_brief.py`): candidates are gathered locally per day, the fallback brief needs no model, sentences without verified facts are removed, and proposals are kept only for items of the right kind that the writer was given.
