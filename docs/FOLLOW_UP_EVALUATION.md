# Follow-up draft evaluation

**Status: recording pending.** Both providers had used their daily quotas when this feature was built (19 October 2026), so no live drafts are recorded yet. Record them with `python -m scripts.evaluate_follow_ups --record` (about 5 minutes); this file is then regenerated from the recording and replays byte for byte.

What is already verified without a provider, on all 18 benchmark threads (`tests/test_follow_ups.py`): every request is free of names, email addresses and amounts; the greeting follows how the user addressed the person ("Dear Ms. Menon", "Dear Sir/Madam", "Hi Neha", or no name); and the checker catches stock AI phrases, exclamation marks, dashes, lists, wrong length, a greeting or sign-off that is not the user's, unverifiable references, and numbers, dates or weekdays that the thread never mentions.

Human blind test: **pending** (`python -m scripts.follow_up_blind_test`).
