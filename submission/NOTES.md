# Notes

Skeleton; completed in Step 10.

## Keys needed
* `ANTHROPIC_API_KEY` (environment or `.env`): extraction at ingest and the slow path.
  Not needed for the stub. Behaviour without a key is described here in Step 10.

## How to run
* `pip install -r submission/requirements.txt`, then
  `python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl --out results/run`.
* Working files go under `KK_WORK_DIR` (default: a fresh temporary directory).

## Known limits
* Step 2 stub: every question is refused; `conflicts()` returns `[]`.
