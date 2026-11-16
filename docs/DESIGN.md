# connectivity-ai-triage: design

For every support case, this service writes a triage note: the likely root cause, the log lines that show it, the runbooks that apply, and what to do next. The interesting engineering is everything around the model call. The model is one function; the rest decides what the model sees, checks what it says, and keeps the cost and the failure modes bounded.

It owns the Postgres schema `triage` and reads cases only through the case service's API.

## Layout

| File | Responsibility |
|---|---|
| `__main__.py` | Entry point: `serve`, `eval` |
| `config.py` | Settings from environment variables |
| `service.py` | Builds the service from settings: the one place that knows how the parts fit |
| `worker.py` | The reconcile loop: which cases need a note, a few at a time, under leases |
| `cases.py` | Client for the case service's API |
| `objectstore.py` | Reads bundles from S3 |
| `bundle.py` | Reads a log bundle safely and chooses the lines worth showing the model |
| `runbooks.py`, `embed.py`, `retrieval.py` | Runbooks as chunks, embeddings, hybrid search |
| `prompt.py` | The prompt, with device text wrapped and neutralised |
| `schema.py` | What a note is, and the grounding checks |
| `llm/` | Providers (Claude, Ollama, fake) behind one interface; retries and the circuit breaker |
| `pipeline.py` | One case from start to finish |
| `store.py` | Postgres: migrations, notes, leases, the spend ledger, the runbook index |
| `api.py` | Notes, runbook search, health, metrics |
| `evals/` | Labelled cases, scoring, thresholds |

## The loop

```
every POLL_INTERVAL:
  list every open case (paged by ID)  ──▶  keep those whose logs are "attached" or "none"
  compare with the notes already stored  ──▶  the ones that need a note
  for each, up to LLM_CONCURRENCY at once:
      claim a lease in Postgres  ──▶  triage  ──▶  release
```

**Polling, not events.** The worker reads the case API instead of consuming a topic. That keeps it independent of the case service's tables and topics, and it makes the service self-healing: after a crash, a restart or a missed event, the next pass simply finds the case again. The cost is a delay of up to one poll and a listing call every poll.

**Reading every case.** The case API returns at most 500 cases a call, so `list_open` pages by case ID until a short page. Asking once would mean that beyond 500 open cases some are never triaged, silently.

**When a case needs a note** (`needs_triage`): it has none; or its note failed and a retry is due and attempts remain; or its last alert time changed, which may mean new bundles. Cases whose logs are still `waiting` are left alone: a bundle is on its way, and waiting is cheaper than paying twice.

**Leases.** `triage.leases` has one row per case being worked on. `claim` is a single `INSERT ... ON CONFLICT DO UPDATE ... WHERE` the lease is expired or already ours, so of two replicas exactly one wins. A replica that dies mid-triage leaves a lease that expires after `LEASE` seconds; there is no heartbeat, so a shorter lease risks two replicas on one slow case and a longer one delays recovery.

## One case (`pipeline.py`)

1. **Fetch** the case and the newest arrived bundle for each device, up to `MAX_BUNDLES_PER_CASE`. A site case with fifty devices doesn't need fifty bundles to show a pattern.
2. **Check the breaker, then skip if nothing changed.** An open breaker is `skipped_breaker`. Otherwise the inputs (bundles, prompt version, model) are hashed; the same hash as the stored note means `unchanged` and no model call. New alerts on the same bundles don't change the hash, or a case collecting an alert a minute would be paid for every minute.
3. **Check the budget.** Over the daily budget is `skipped_budget`. Like `skipped_breaker`, it doesn't count as a failed attempt, because nothing was wrong with the case.
4. **Choose evidence** (`bundle.py`).
5. **Retrieve runbooks** (`retrieval.py`).
6. **Build the prompt** (`prompt.py`) and **call the model** with retries.
7. **Parse and check** the note (`schema.py`).
8. **Store** the note with its checks, cost and latency, whatever the outcome.

Every outcome is a row in `triage.notes`, including failures and skips, so "why has case 12 no note?" always has an answer.

## Evidence under a budget

A bundle is a gzipped tar from a device, which makes it untrusted input twice over: its size and its content.

- It is read in memory with limits on its compressed and expanded size, and never extracted to disk. A 64 KB gzip can expand to gigabytes.
- Runs of lines that differ only in numbers collapse to the first and last, with a count.
- The rest are ranked: errors and warnings first, then lines within five minutes of an alert, then the most recent. They are packed into `EVIDENCE_TOKEN_BUDGET` using a deliberately high token estimate, since log lines tokenise worse than prose.

The result is that a 3 MB log storm costs the same as a 30-line log, and the model's context is never the limit that fails.

## Retrieval

Runbooks are markdown with front matter, split into one chunk per `##` section; each chunk carries its runbook's title so a section called "Fix" still says what it fixes. Chunks are embedded (fastembed's `bge-small-en-v1.5` on the CPU, baked into the image) and stored in pgvector alongside a full-text index.

For a case, retrieval returns:

- **every runbook tagged with the case's alert code**, always. These are the differential: the causes that alert can have. Retrieval quality must never decide whether the model sees the candidate causes.
- **the best of the rest** by hybrid search: vector similarity and keyword search, merged with reciprocal rank fusion. Vectors find paraphrases; keywords find exact part names and error strings that embeddings blur.

## The prompt and untrusted text

Some device text comes from fields a customer can edit, such as an asset tag, so every log line and alert message is treated as data that may try to give orders:

- Device text is wrapped in tags and labelled as untrusted in the system prompt.
- Angle brackets inside a line are neutralised, so a line can't close its own tag and escape.
- The system prompt says what to do when data contains instructions: report it, don't follow it.
- A filter flags instruction-like lines, and the note records `injection_detected`.
- Nothing acts on a note. Next steps are suggestions for an engineer.

The last point is the one that holds if the others fail.

## Structured output and grounding

The model is asked for a structured output constrained to the note's JSON schema, so the reply always parses. That makes it well-formed, not true. `schema.check` then verifies:

- every quoted evidence line appears, exactly, among the lines the model was shown;
- every device named belongs to the case;
- every runbook cited was one it was given.

The results are stored with the note. An ungrounded note is kept and marked, not hidden, and `TriageNotesUngrounded` alerts on it.

## Providers and failure

`llm/` has one interface and three providers: Claude through the Anthropic API, a local model through Ollama, and a deterministic fake. Providers raise `LLMError` with a `kind`, and the kind decides the response:

| Kind | Retried | Counts toward the breaker |
|---|---|---|
| rate limited, timeout, overloaded, server error, connection | yes, with backoff and jitter, honouring `Retry-After` | yes |
| auth | no: every request would fail the same way | yes |
| invalid output | yes | no |
| truncated, refusal, bad request | no | no |

The circuit breaker opens after a run of failures that mean the provider is unhealthy, fails fast while open so the worker stops paying for doomed requests, and lets one probe through after a cool-down.

The SDK's own retries are turned off: retries happen in one place, where every attempt is counted and recorded.

**The fake model** reads the prompt as the real one would and applies hand-written rules that know this system's log formats. It exists so the whole pipeline (evidence, retrieval, prompt, schema, checks, budgets, retries) is testable and free to run. It is priced like Claude Haiku so budgets and spend alerts behave. It says nothing about model quality, and it can be told to misbehave (`/debug/llm-faults`) to exercise the failure paths.

## Cost

Every model call is a row in a spend ledger. The daily budget is the sum over the last 24 hours across all replicas, read from Postgres before each call, so two replicas can't each spend a full budget. The check and the spend are not atomic: with `LLM_CONCURRENCY` calls in flight the budget can be overshot by that many notes.

## Evals

`cmd/bundlegen` in the edge agent generates labelled cases from the device simulator's own sensor, log and bundle code, so the eval data can't drift from what devices send. `evals/dataset.py` adds stress variants: the alert with no logs (the right answer is "insufficient evidence") and the evidence buried under a log storm.

Each case is run through the real pipeline and graded on category, grounding, retrieval rank of the right runbook, and injection handling. Thresholds per provider and embedder are in `evals/thresholds.yaml`; CI fails below them.

With the fake model the suite is a regression test for everything around the model. Only a run against a real model measures the model.

## Known limits

- A lease has no heartbeat.
- The budget check isn't atomic with the spend.
- Cases are triaged oldest first; when the budget is short, nothing ranks a site case above a device case.
- A case resolved before it is triaged never gets a note, because only open cases are listed.
- The image is large (about 750 MB) because the embedding model and its runtime are baked in.
