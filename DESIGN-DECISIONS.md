# Design decisions

Each decision below follows the same pattern: **what** it is, **why** I chose it,
the **alternative** I turned down, and the **tradeoff**, meaning what the choice
costs. Where a number backs a claim, it comes from this repository (the tests, the
benchmark in `docs/query-performance.*.md`, the eval in `docs/eval-report.md`, or the
saved outputs in `docs/evidence/`).

Who the tool is for, the risks, and the cuts are revisited in
[section 16](#16-the-tradeoffs-revisited).

---

## 1. Data model

### 1.1 A normalized relational schema

**What.** Nine core tables (features, stories, tasks, acceptance_criteria,
requirements, commits, pull_requests, reviews, people), each holding one kind of
thing, with relationships expressed as foreign keys and link tables.

**Why.** Every question this tool answers is a walk along a chain: requirement →
criterion → task → commit → pull request → review. In a relational model that walk
is a join, and the database guarantees the chain can't point at something that
doesn't exist. It also means each fact lives in one place: a PR title is stored once,
not copied onto every commit row that belongs to it.

**Alternative rejected.** A document store (one JSON blob per story with everything
nested inside), or one wide denormalized table.

**Tradeoff.** Reads need joins, and the schema needs migrations when it changes. A
document model would make "show me one story" a single read. But the questions here
cut across documents ("every commit with no task, in any story"), and without joins
those turn into application code that scans everything. Integrity would also move
from the database into hope.

### 1.2 Surrogate primary keys plus a source key on every row

**What.** Each row has an internal `id` (an identity column) used by foreign keys,
plus `(source_system, source_id)` with a unique constraint. Source ids are
namespaced and deterministic, for example `pallets/click@<sha>` for a commit and
`pallets/click#pr-3877` for a PR. Every row also stores `raw` (the original record),
`raw_hash` and `synced_at`.

**Why.** Internal ids keep foreign keys small, fast and stable. The source key is
what makes loads idempotent (see 3.2): the same object fetched twice maps to the same
row. `raw` lets us re-derive columns later without calling the API again, and answer
"what exactly did GitHub send?" during an incident. `synced_at` tells you how fresh a
row is.

**Alternative rejected.** Using the source id itself as the primary key, or making
`source_id` alone unique, as the original brief suggested.

**Tradeoff.** One extra column and index per table. Two systems can use the same id
for different things, so uniqueness is on the pair `(source_system, source_id)`.
Because the ids are also namespaced, `source_id` is unique on its own in practice
too, so the brief's intent is met with an extra safety margin.

### 1.3 Link tables for many-to-many relationships

**What.** `task_criteria`, `commit_tasks`, `requirement_criteria`,
`pull_request_commits` and `criterion_tests`. Each has a composite primary key over
its two foreign keys, plus a `link_source` column on the links we infer (for example
`message_ref`, `pr_ref` or `backlog_map`).

**Why.** These relationships really are many-to-many. One commit can serve two tasks;
one criterion can be met by work in three tasks. A link table is the textbook,
queryable way to model that. The composite key makes a duplicate link impossible at
the database level, not just unlikely. `link_source` records *how* we know the link
exists, which matters when a link turns out to be wrong.

**Alternative rejected.** Arrays of ids on the parent row (`tasks.commit_ids
bigint[]`), or a JSON list.

**Tradeoff.** More tables, more joins. Arrays can't carry a foreign key, can't hold
per-link data like `link_source`, can't be indexed in both directions cheaply, and
make "who points at this commit?" a scan.

**A modelling choice inside this.** Acceptance criteria belong to the **story**, not
the task. A criterion says what "done" means for the user-facing story; tasks are how
the team chooses to split the work, and one task often serves several criteria. The
many-to-many `task_criteria` link connects the two views.

### 1.4 Constraints that encode business rules

**What.** Foreign keys everywhere, `NOT NULL` where a value is required, and `CHECK`
constraints for rules such as:
- a planned task must sit under a story (only raw GitHub issues may float);
- a merged PR must have `merged_at`;
- a commit sha is 40 characters;
- a story can't complete before it starts;
- a "fail" verdict can never be stored as `final`: it must be pending, approved or
  rejected.

**Why.** Rules enforced by the database hold for every writer: this code, a future
script, someone in a SQL console. Tests in `tests/test_schema.py` prove each rule
rejects bad data, on SQLite and on Postgres.

**Alternative rejected.** Validating only in Python.

**Tradeoff.** Bad data fails loudly at insert time, which is the point, but loaders
must be written to handle that (see 3.3, failure isolation). SQLite enforces foreign
keys only when `PRAGMA foreign_keys = ON`, so the backend sets it on every connection.

### 1.5 One person, many identities

**What.** `people` has a unique `login` and a unique `email_hash`. The normalizer
matches on login first, then on email hash, and fills in whichever key it learns later.

**Why.** Git history knows a name and an email; the API knows a login. Without
resolution the same engineer appears twice and their review turnaround is split
across two rows. The test
`test_git_seed_person_and_api_person_resolve_to_one_row` covers this.

**Alternative rejected.** One row per (source, identity), merged later in analytics.

**Tradeoff.** Matching rules can be wrong. Two people sharing an email would merge.
The rules are deliberately conservative: exact login or exact email hash, never a
fuzzy name match.

### 1.6 Personal data stays out

**What.** Emails are stored only as sha256 hashes. Emails written inside commit
messages (Co-authored-by and Signed-off-by trailers) and PR bodies are masked before
storage, and the seed files contain none. A test scans every stored `raw` payload
for email addresses.

**Why.** We never need an email to answer a delivery question, and data we don't
hold can't leak. That test found a real leak while I was building this: trailers in
public commit messages.

**Alternative rejected.** Storing emails and restricting access to them.

**Tradeoff.** We can't email people from this tool, and `raw` is no longer
byte-for-byte what the API returned. That is documented, and patch bodies are also
left out of `raw` because they live in the `diff` column.

---

## 2. Indexes and search

### 2.1 Which indexes, and why they live in a separate migration

**What.** `002_indexes.sql` indexes every foreign key column, adds the reverse
direction on every link table (`(task_id, commit_id)` next to the primary key's
`(commit_id, task_id)`), and adds status/date indexes used by filters. Two of those
are partial indexes: `merged_at WHERE merged_at IS NOT NULL`, and unprocessed staging
rows only.

**Why.**
- *Foreign keys:* Postgres does not index them automatically. Without the index,
  joining child to parent, and every `ON DELETE` check, scans the child table.
- *Reverse link indexes:* a composite primary key only helps when you look up by its
  first column. "Which commits belong to this task?" needs the opposite order.
- *Partial indexes:* they stay small and cover exactly the rows the query asks for.
- *Separate migration:* so `devtrack bench` can measure the same queries without and
  with these indexes, on the same data.

**Measured result (PostgreSQL 16, about 150k commits).** "Commits not tied to any
task" went from more than 60 seconds (it hit the timeout) to 452 ms. The full trace
for one requirement went from 51 ms to 2.2 ms. The whole-table aggregations (cycle
time, turnaround) didn't change, because Postgres keeps using hash joins when it reads
everything anyway.

**Alternative rejected.** Indexing nothing until it's slow, or indexing every column.

**Tradeoff.** Every index slows writes and takes disk. There was also a surprise
worth knowing: on SQLite, which has no hash join, two aggregations got **2.5x slower**
with the new indexes, because its planner switched to nested loops through them. In
production on Postgres that doesn't happen. If it did, the fix would be a materialized
view for those aggregations, not removing the index the selective queries need.

### 2.2 Full-text search in the database

**What.** Postgres: a generated `tsvector` column with a GIN index on criteria text
and commit messages, queried with `websearch_to_tsquery`. SQLite: FTS5 tables kept
in sync by triggers.

**Why.** Searching "zstd proxy" across 150k commits took 94 ms with ILIKE and 2.2 ms
with GIN. It needs no extra system to run. User input is never parsed as search
syntax: Postgres's `websearch_to_tsquery` accepts arbitrary text safely, and FTS5
terms are quoted.

**Alternative rejected.** Elasticsearch or OpenSearch.

**Tradeoff.** Ranking is simpler than a search engine's (no synonyms, no fuzzy
matching). At this size a second system would cost more to run than it gives back.

### 2.3 Lexical similarity vectors in their own table, with a model column

**What.** `embeddings(entity, entity_id, model, dim, vector_json)`. Similarity is
computed in Python. The only embedder is a 256-dimension feature-hashing bag of
words, which is lexical, not semantic, so the UI calls it "lexical similarity".

**Cut before release.** An earlier version added a pgvector column and HNSW index
when the extension was present. That path was never exercised (no pgvector in the
build environment), so it was removed from the migration before any deployment
rather than shipped untested. See section 15.

**Why.**
- *A separate table keyed by model:* switching embedding models is a new row set,
  not a schema change, and old and new vectors can be compared during a migration.
- *The hashing embedder:* it runs offline, is deterministic for tests, and needs no
  API key.
- *What it's used for:* suggesting candidate commits for criteria that have no linked
  code. The dashboard shows suggestions only above a similarity threshold, and never
  links anything automatically.

**Alternative rejected.** A vector column directly on `acceptance_criteria` and
`commits`, or a dedicated vector database.

**Tradeoff.** One more join. The hashing embedder is lexical, not semantic: it
matches `strip_ansi` to "strip ANSI codes" but not to "remove colour escapes". In
production I'd plug in a real embedding model behind the same `Embedder` interface.

---

## 3. Ingestion

### 3.1 Staging first, then normalize

**What.** Every fetched record goes into `staging_records` exactly as received, with
its entity type, source id and a sha256 of the payload. A separate step normalizes
staged rows into the core tables.

**Why.**
- *Replay without re-fetching:* if the normalizer has a bug, fix it and replay. No
  API calls, no rate limit spent.
- *Failure isolation:* a malformed record fails its own staging row (the error is
  stored) instead of aborting the batch.
- *Free re-fetches:* the unique key includes the payload hash, so fetching an
  unchanged record again is a no-op, and only records that really changed get
  processed.
- *An audit trail:* you can see what arrived, when, and in which batch.

**Alternative rejected.** Transforming API responses straight into the core tables
(plain ETL), or one staging table per entity type.

**Tradeoff.** Stored data is roughly doubled until staging is pruned (production
would partition by month and expire old rows). A single generic staging table is less
self-documenting than one table per entity, but the normalizer, not the staging layer,
is where the shape matters, and one table keeps the replay logic in one place.

### 3.2 Idempotent upserts

**What.** Every core write is
`INSERT ... ON CONFLICT (source_system, source_id) DO UPDATE SET ... WHERE
table.raw_hash <> excluded.raw_hash RETURNING id`. Links use
`ON CONFLICT DO NOTHING`. The same SQL runs on SQLite (3.35+) and Postgres.

**Why.** Running the load twice must never duplicate rows, and should not even touch
rows that didn't change. The `WHERE raw_hash <> ...` clause turns a rerun into zero
writes: `synced_at` keeps meaning "last time the source changed", and there are no
pointless row versions. The test `test_rerunning_the_seed_changes_nothing` asserts no
writes, identical counts and an unchanged `synced_at`.

**Alternative rejected.** Delete-and-reload, or a check-then-insert in Python
(`SELECT`, then `INSERT` if missing).

**Tradeoff.** Delete-and-reload breaks foreign keys and loses history.
Check-then-insert has a race between the check and the insert when two loaders run.
The database-native upsert is atomic. The cost is a hash computation per record,
which is negligible.

### 3.3 One transaction per record

**What.** The normalizer processes each staging row inside its own transaction. If it
fails, the error goes on the staging row, `processed_at` stays empty, and the next run
retries it.

**Why.** On Postgres a single failed statement aborts the entire transaction, so one
bad record in a big transaction loses the whole batch. Per-record transactions keep
the good records and make the bad ones visible (the data-quality check
`no_failed_staging_rows`). A record that failed because its parent hadn't arrived yet
loads on the next run, once the parent is there.

**Alternative rejected.** One transaction per batch: all or nothing.

**Tradeoff.** More commits, so it is slower at very large volume. Production would
batch by a few hundred with savepoints. All-or-nothing is the right choice when
partial data is worse than no data; here, partial data with visible errors is better.

### 3.4 Incremental sync with a source-side watermark

**What.** `sync_state` stores, per repo and entity, the newest **source-side**
`updated_at` (or commit date) we have fully processed.
- *Commits and issues* use `since=watermark`.
- *Pull requests* have no `since` parameter, so they are fetched sorted by update
  time and the walk stops at the first PR older than the watermark.
- *Overlap:* the watermark is moved back 10 minutes on every run.
- *When it advances:* only after the batch is normalized.
- *Commit details* (files and patches cost one request each) are fetched only for
  shas we don't already have.

**Why.**
- *Source time, not our clock:* the two clocks drift, and the source time is what
  `since` filters on.
- *The overlap:* it covers clock skew and records updated while we were fetching. The
  idempotent upserts make the overlap free.
- *Advancing only after processing:* a crash means the next run re-fetches the same
  window instead of silently skipping it.

**Alternative rejected.** A full re-sync every time, or a watermark set to "now"
when the run starts.

**Tradeoff.** "Now" can skip records that change during the run. A full re-sync
burns the rate limit and doesn't scale. The overlap re-reads a few records each run,
which costs almost nothing.

### 3.5 The API client

**What.**
- *Auth:* a bearer token from `GITHUB_TOKEN`.
- *Pagination:* follows the `Link: rel="next"` header.
- *Primary rate limit:* when `X-RateLimit-Remaining` hits 0, the client waits until
  `X-RateLimit-Reset`.
- *Secondary rate limit:* it honours `Retry-After`.
- *Pacing:* it spreads requests out once the remaining budget drops below a
  low-water mark.
- *Retries:* 5xx responses and connection errors are retried with exponential
  backoff and **full jitter**.
- *Fail fast:* other 4xx responses are not retried.
- *Testability:* the clock and sleep are injected.

**Why.**
- *Link headers:* that is GitHub's documented contract, and it keeps working when an
  endpoint uses cursors instead of page numbers.
- *Full jitter:* many workers don't retry in lockstep.
- *Failing fast on 4xx:* retrying a 404 or a permissions 403 is wasted time.
- *Pacing:* the client slows down before GitHub makes it stop.
- *Injected clock and sleep:* the tests check exact wait times instantly.

**Alternative rejected.** Computing page URLs, retrying everything, or a fixed sleep
between requests.

**Tradeoff.** More code than a bare `requests.get` loop. The token never appears in
logs, and a log filter redacts token shapes as a second line of defence.

### 3.6 The seed comes from real git history

**What.** `scripts/build_seed_from_git.py` reads local clones of three public repos
and writes JSON shaped exactly like GitHub REST responses. That covers commits with
diffs, and PRs reconstructed from merge commits and `(#123)` squash subjects. Each
record says where it came from.

**Why.** The demo must run offline and without a token, but it should run on real
code with real diffs, or the AI checker has nothing honest to judge. Because the
records are shaped like API responses, the seed exercises the same staging →
normalize path as a live sync.

**Alternative rejected.** Hand-written fake commits, or requiring a token and network
for the demo.

**Tradeoff.** Git can't tell us everything. PR open times for squash merges are
unknown, so they are left empty rather than invented. Reviews and issues aren't in
git, so seed reviews are synthetic and flagged, and real ones arrive with
`devtrack sync`.

### 3.7 A synthetic backlog, built on purpose to contain gaps

**What.** `seed/backlog.json` is a purpose-written hierarchy (6 features, 17
stories, 55 tasks, 64 acceptance criteria, 12 requirements in a three-level tree, with
version history) mapped onto real PRs. Claude Code wrote it, at my direction, by
reading the real diffs. It plants:
- criteria with no code;
- criteria the linked code doesn't satisfy;
- requirements revised after their code merged;
- commits with no task;
- one commit carrying a prompt injection.

**Why.** A gap finder needs gaps to find, and an AI checker needs cases where the
right answer is "fail". Writing each criterion against the actual diff also makes the
eval labels defensible.

**Alternative rejected.** Generating criteria automatically from PR titles.

**Tradeoff.** Hand-writing takes time and the result is smaller (64 criteria). It is
also written by the same person who labels the evals, which is noted in the labels
file: a second labeller should re-check a sample.

---

## 4. Queries and portability

### 4.1 SQL in files, no ORM

**What.** Each saved query is one `.sql` file. Dialect differences are minimal and
explicit: one `HOURS_BETWEEN(a, b)` macro becomes `julianday` arithmetic on SQLite
and `EXTRACT(EPOCH ...)` on Postgres. The database layer is a thin wrapper.

**Why.** The SQL *is* the deliverable here: it is what gets `EXPLAIN ANALYZE`d,
reviewed and tuned. An ORM would hide it, and query-builder code is harder to read
than SQL.

**Alternative rejected.** SQLAlchemy ORM or Core.

**Tradeoff.** No automatic dialect translation and no model classes. Two dialect
differences surfaced: date arithmetic, and `ROUND` needing `NUMERIC` on Postgres.
Both are covered by the tests on both databases.

### 4.2 Postgres first, SQLite as a fallback

**What.** Two sets of migrations with the same tables and constraints. Timestamps in
SQLite are ISO-8601 UTC text (string order equals time order), JSON is text with
`json_valid` checks, and booleans are 0/1. The test suite runs on both.

**Why.** Postgres is the production target (JSONB, GIN indexes, roles, triggers).
SQLite means anyone can run the full demo with `pip install` in seconds. Since the
production review ([section 16](#16-the-tradeoffs-revisited)), SQLite is explicitly *not* a production option: it stays
for tests, CI speed and the offline demo.

**Alternative rejected.** Postgres only, or SQLite only.

**Tradeoff.** Two migration sets to keep in step. The shared test suite on both
databases is what keeps them honest; it found the rollback bug in the Postgres
backend that SQLite would never have shown.

---

## 5. The AI checker

### 5.1 What the model sees

**What.** For one criterion the model gets:
- the criterion text, its story and feature, and the requirement text it serves;
- the linked tasks and tests;
- every commit behind those tasks, oldest first, with message and diff.

The diff budget is split evenly across commits, and truncation is flagged.

**Why.** "Does the code meet the criterion?" needs the code that was meant to meet
it, which is exactly what the link tables provide. Oldest-first order matters because
a later commit can undo an earlier one: `requests` #7431 made headers a
`MutableMapping`, and #7441 reverted that a day later. The prompt says to judge the
final state.

**Alternative rejected.** Whole files or the whole repository as context (too large,
and mostly irrelevant), or only the latest commit (misses multi-commit work).

**Tradeoff.** Truncated diffs can hide the relevant line. When that happens the
verdict is flagged `context_truncated`, and v2's rubric says to answer "unclear"
rather than guess.

### 5.2 Structured output, validated

**What.** The model must return
`{verdict, evidence: [{quote, source}], confidence, reasoning}`, validated by a
pydantic schema. An invalid reply gets one repair attempt; if that fails too, the
verdict is "unclear" with the flag `invalid_model_output`. Both calls are billed and
recorded.

**Why.** Downstream code (the database, the dashboard, the evals) needs a fixed shape,
and "the model usually returns JSON" is not a contract.

**Alternative rejected.** Free-text answers parsed with regexes, or failing the whole
run on bad output.

**Tradeoff.** The repair costs a second call. The cap of one retry bounds that cost.

### 5.3 Prompt versions are immutable

**What.** Prompts live in `checker/prompts/v1.*` and `v2.*`. Every verdict records its
`prompt_version` and `model`.

**Why.** If you edit a prompt in place, old results become uninterpretable. With
versions, `eval_runs` can compare v1 and v2 on the same labels. v2 adds a rubric:
every part of the criterion needs evidence; performance claims are "unclear";
docs and test claims need files of that kind; judge the final state.

**Tradeoff.** More files. That is the price of being able to say "v2 is better" with
numbers instead of impressions.

### 5.4 The offline heuristic baseline

**What.** `HeuristicJudge` checks keyword coverage of the criterion in added lines,
plus three rules: performance claims become "unclear", and docs or tests claims need
a changed file of that kind.

**Why.** It runs anywhere at zero cost, it exercises the whole pipeline, and it sets a
**floor** any model must beat: 60% accuracy, 64% fail precision and 56% fail recall
on the 48 labels. Its misses show what lexical matching can't do: the criterion and
the code use different words, or a criterion is partly met.

**Tradeoff.** It is deliberately simple and will never be good. It is a baseline, not
a product.

---

## 6. Guardrails

All of these are in `checker/guardrails.py` and `checker/run.py`, and each has a test.

### 6.1 Repository content is untrusted

**What.** Commit messages, diffs and PR text are wrapped in
`<untrusted_repo_content nonce="...">` tags with a random nonce per call. Any attempt
inside the content to write a closing tag is neutralised. The system prompt says
everything inside those tags is data.

**Why.** Anyone who can push a commit can write text the model will read. That is
**prompt injection**. The demo data contains one: a comment telling "AI reviewers" to
mark every criterion as passed.

**Alternative rejected.** Pasting diffs straight into the prompt.

**Tradeoff.** Fencing lowers the risk but doesn't remove it: models can still be
talked into things. That is why 6.2 exists.

### 6.2 Don't trust the model to resist: check after it answers

**What.** Three deterministic post-checks run after the model answers:
1. **Verbatim evidence.** Every quote must appear in the exact text we sent, ignoring
   whitespace, case and leading diff markers. Quotes that don't are dropped
   (`evidence_not_found`).
2. **A pass needs evidence.** A "pass" left with no valid quote becomes "unclear"
   (`unsupported_pass`).
3. **Injection downgrade.** If the repository text contains instruction-like
   patterns, any "pass" becomes "unclear" with confidence capped at 0.5
   (`injection_suspected`), and a person has to look.

**Why.** A prompt is a request; code is a guarantee. The test
`test_compromised_model_pass_is_downgraded` simulates a model that *obeys* the
injected instruction, and the pass still doesn't stand. Verbatim quotes also catch
the most common hallucination: confident evidence that isn't in the diff.

**Tradeoff.** Detection uses patterns, so it will miss clever phrasing and
occasionally flag innocent text. A miss is bounded, because a pass still needs
verbatim evidence. A false flag costs one human review, which is the cheap side of
the trade.

### 6.3 Secrets never leave

**What.** Before a prompt is built:
- *Files* that usually hold secrets (`.env`, `*.pem`, `*.key`, `id_rsa`) are dropped
  from the diff entirely.
- *Credential shapes* are redacted: AWS keys, GitHub, Anthropic, OpenAI and Slack
  tokens, JWTs, passwords inside URLs, private key blocks, and `api_key = "..."`
  style assignments.
- *The test:* `test_secrets_never_reach_the_model` links the demo commit that
  contains a fake key and inspects exactly what the fake model received.
- *Configuration* (the API key, the database URL) is never part of any prompt.

**Why.** Sending a customer's leaked credential to a third-party model provider turns
one incident into two.

**Tradeoff.** Pattern redaction has false negatives (an unusual key format) and false
positives (an innocent long string next to the word "token"). Over-redacting costs
little accuracy. Under-redacting is the risk, so the patterns err broad.

### 6.4 Cost is capped before each call

**What.** A per-run budget. Before each call the worst-case cost (estimated input
tokens plus the maximum output) is checked against what's left, and the run stops
cleanly when the next call could overspend. Actual usage from the API is recorded on
every verdict. Prices are configuration, not code.

**Why.** A loop over thousands of criteria with an unexpectedly large diff is how a
demo becomes an invoice. Checking *before* the call is what makes the cap a cap.

**Tradeoff.** The token estimate (about 4 characters per token) is rough, so the cap
is approximate. Reserving for the maximum output means stopping slightly early rather
than slightly over.

### 6.5 Caching

**What.** The cache key is a sha256 of the prompt version, the model and the rendered
prompt (with the random nonce removed). An identical input reuses the stored verdict.

**Why.** Re-checking unchanged criteria costs money and returns the same answer.
Changing the code, the criterion, the prompt version or the model changes the key,
so stale verdicts can't be served.

**Tradeoff.** Temperature 0 makes repeats very similar, but a cached verdict hides
run-to-run variation. Measuring that is a deliberate `--force` run, not a side effect.

### 6.6 No code, no call

**What.** A criterion with no linked commits is recorded as "fail" by a rule
(`rule:no-linked-code`), with no model call and no cost, and it still goes to human
review.

**Why.** There is nothing for a model to judge, and asking anyway invites a
hallucinated pass.

---

## 7. Human approval

**What.** Every "fail" is stored as `pending_review`. A person confirms it
(`approved`) or overturns it (`rejected`) with their name and an optional note, in the
dashboard or with `devtrack review`. The `counted_verdicts` view counts a fail only
once it is approved. A database `CHECK` makes it impossible to store a fail as
`final`.

**Why.** A fail has consequences: someone's work gets reopened. The model's
precision on fail is measured, not perfect, so a fail is a recommendation until a
person signs it. Passes and unclears don't need sign-off, because "unclear" already
routes to a person and "pass" carries verbatim evidence.

**Alternative rejected.** Letting the AI's verdict stand, or reviewing everything.

**Tradeoff.** It adds human work proportional to the number of fails. The eval's
fail precision tells you how much of that work is wasted on false alarms, so the
trade is visible.

---

## 8. Evaluation

**What.** 48 criteria are labelled pass/fail/unclear, each with a written rationale.
The labels are AI-generated, with a two-pass independent agreement check (section 15.3). Every eval run stores accuracy, precision and recall for "fail", the
unclear rate, the full confusion matrix and the cost in `eval_runs`, with per-criterion
results in `eval_results`, keyed by prompt version and model. The report groups misses
by cause.

**Why these metrics.**
- *Fail is the positive class* because the expensive mistake is a missed fail:
  unbuilt work marked done.
- *Precision* tells you how much reviewer time is wasted on false alarms.
- *The unclear rate* is tracked because a checker that says "unclear" to everything
  scores no false passes and is useless.
- *Accuracy alone* would hide all of that.

**Why store runs.** "The new prompt looks better" is an impression; a stored run per
version on the same labels is a measurement. CI now blocks a change that drops fail
detection below the keyword baseline (section 15.2).

**Alternative rejected.** Eyeballing a few outputs, or LLM-as-judge with no human
ground truth.

**Tradeoff.**
- *Size:* 48 labels give wide error bars. One criterion is about 2 points of
  accuracy.
- *Who labelled:* an AI, the same family as the judge. See section 15.3.

---

## 9. Natural-language questions

**What.** Templates only: common questions map to vetted, parameterised SQL. Feature
and repo names are matched against the database and passed as parameters, never
pasted into SQL. Questions no template covers get suggestions, not a guess.

**Cut before release.** An earlier version sent unmatched questions to a model that
wrote SQL. Even validated and read-only, model-written SQL against a production
database needs its own security review, and the templates already covered what people
asked. It was removed ([section 16](#16-the-tradeoffs-revisited)). The way back is a semantic layer: named metrics the
model chooses from, rather than raw SQL.

Every statement, even a template's, still passes a validator, as defence in depth:
- exactly one statement, starting with SELECT or WITH;
- allow-listed tables only, with any mention of internal tables or system catalogs
  rejected, including inside comma joins;
- denied functions blocked (`pg_sleep`, `pg_read_file`, `load_extension`, and so on);
- a LIMIT is always applied.

Finally it runs on a **read-only connection**: SQLite opened `mode=ro` with
`query_only`, and on Postgres a `devtrack_reader` role with SELECT on the allow-list
only, read-only transactions by default and a 5-second statement timeout.

**Why.** The question is untrusted. The validator gives
clear, early errors and blocks things a read-only connection would still allow
(expensive functions, internal tables, unbounded results). The database role is the
layer that actually holds if the validator has a gap; the tests try both
(`test_read_only_connection_refuses_writes_even_if_validation_were_bypassed`).

**Alternative rejected.** Keeping model-written SQL behind the validator. The
validator and read-only role would probably hold, but "probably" isn't a launch bar.

**Tradeoff.** Templates cover a fixed set of questions, and the validator rejects
some legitimate SQL (quoted identifiers, for example). Read-only access is the right
default for a box anyone can type into.

---

## 10. The dashboard

**What.** Flask with server-rendered Jinja templates (autoescaped), one database
connection per request, no JavaScript framework. The only write is the approve or
overturn form, protected by a CSRF token and requiring a reviewer name.

**Why.** It is a reporting tool, and server rendering keeps it small, fast to load,
easy to test with Flask's test client, and free of a build step. Autoescaping matters
because commit messages are untrusted text that ends up in HTML.

**Alternative rejected.** A React single-page app over a JSON API.

**Tradeoff.** Less interactivity (no live filtering without a reload). There is no
login: this is a local demo, and production would put it behind SSO.

---

## 11. Verification: method, build and change control

The tracker originally answered "is there code and a test for this criterion?". A
systems-integration reviewer asks a harder question: *is this requirement verified,
by the method it calls for, on the build we are shipping, and is that evidence still
valid after the last change?* Sections 11.1 to 11.5 are how the schema answers it
(migration `006_verification`, module `verification.py`).

### 11.1 Every requirement names one verification method

**What.** `requirements.verification_method` is one of test, analysis, inspection or
demonstration (the classic TAID set), with a CHECK constraint. Only evidence of that
method counts toward the requirement. A passing unit test does nothing for a
requirement that says "inspection".

**Why.** "Secrets never appear in logs" (REQ-HTTPX-03) can't be proven by a test that
happens to pass; someone has to read the logging paths. Recording the method makes the
gap visible instead of letting a green test stand in for a review.

**Alternative rejected.** A method per criterion. More flexible, but in practice the
method is decided when the requirement is written, and one value per requirement keeps
the roll-up honest and the data entry small.

**Tradeoff.** A requirement that genuinely needs two methods (test plus inspection)
has to be split into two requirements. That is also how most verification matrices
handle it.

### 11.2 Builds and releases sit in the chain

**What.** Two new tables give the chain its missing links:
commit → `builds` (a CI run of one sha) → `releases` (a tag pointing at a commit,
resolved to the build of that commit). `test_runs` holds one test's outcome in one
build. Every `verification_results` row has a NOT NULL `build_id`.

**Why.** A verification that doesn't name the build it applies to is a claim about
nothing in particular. With a build on every result, status is always relative to a
**target build**: by default the latest main-branch build of each repository, or any
release picked in the dashboard. Results from builds that finished *after* the target
don't count, so "what did we know when 8.4.0 shipped?" has a real answer.

**What's real.** In the offline seed the 12 release tags are real (read from git).
Git keeps no CI history, so the 147 builds and their test results are synthetic,
generated deterministically from the real merged PRs and release tags, flagged
`source_system = 'synthetic'`, and include one scripted regression (a click pager test
starts failing on 1 October 2026). Live sync reads real GitHub Actions runs and
releases, and `devtrack ingest-junit` loads real JUnit reports.

**Alternative rejected.** Storing "verified: yes/no" on the criterion. It can't say
*where* it was verified, and it can't go stale.

**Tradeoff.** Run numbers are only unique per workflow, so builds carry the workflow
name, and `GITHUB_CI_WORKFLOW` narrows sync to the workflow that runs the tests.

### 11.3 Test results become verification results, conservatively

**What.** For a test-method criterion, each build produces a result when its linked
tests ran: **failed** if any linked test failed or errored, **passed** only if *every*
linked test ran and passed. A skipped or missing test produces no result at all. The
evidence column lists each test and its outcome, with the failure message.

**Why.** "Didn't run" must never read as "passed". Deriving results from `test_runs`
(instead of trusting the build's overall conclusion) also means an unrelated flaky
test can't fail a criterion, and a green build can't verify a criterion whose test was
skipped.

**Manual methods.** Analysis, inspection and demonstration results are sign-offs by a
named person against one build (`devtrack verify`). Test results can only come from CI:
`record_verification` refuses method `test`, a blank name, or a build from another
repository.

### 11.4 Change control that acts

**What.** When a requirement gets a new version, `apply_change_control`:
1. **invalidates** every result for its criteria recorded before the change (rows are
   kept with `invalidated_at` and a reason such as "REQ-REQ-02 changed to v3 on
   2026-06-20", never deleted);
2. **opens a re-verification item** per criterion, tied to the new version;
3. **closes** an item on the first valid passing result of the right method recorded
   inside its window (from its change until the next one), and marks it **superseded**
   when a newer version opens its own item first.

**Why.** The original "requirements changed after merge" query could only *report*
drift. Now drift has consequences: old evidence stops counting, and there is a work
item until someone re-verifies. In the seed, REQ-REQ-02's three analyses predate v3,
so it shows "Needs re-verification" with three open items; REQ-CLICK-01's v2 items
were closed automatically by the passing builds after the change.

**Idempotent.** `refresh()` runs after every seed and sync and writes nothing when
nothing changed (a test asserts this), so it is safe to run on a schedule.

**Alternative rejected.** Deleting stale results. That destroys the history an auditor
wants: what was verified, on which build, and why it stopped counting.

**Tradeoff.** A criterion shared by two requirements is invalidated when either one
changes. That is the conservative direction: the cost is an extra re-verification, not
a stale pass.

### 11.5 Status precedence and the roll-up

**What.** For each criterion, against the target build:
**failed** (the newest valid result failed) → **verified** (passing result on the
target build) → **re-verify** (open item) → **verified on older build** → **not
verified**. A requirement rolls up to failed if any criterion failed, then needs
re-verification, verified (all on target), verified on older build, partial, or not
verified. The two rules are pure functions with their own unit tests.

**Why.** A failure always wins, because it is the one fact you can't afford to hide.
"Verified on an older build" is kept separate from "verified" because a passing
inspection of build #1028 says nothing certain about #1049.

**Tradeoff.** Against an old release, the status is computed for the requirement's
*current* version, so evidence invalidated by a later change shows as not verified
there. That answers "does this release meet today's requirement?"; answering "did it
meet the requirement as it stood then?" means replaying the audit trail (section 14.2)
to a date, which the log supports but no command does yet.

---

## 12. Data classification and which models may see what

**What.** Each repository has a classification in `repo_policies`: public, internal
or controlled (export-controlled, such as ITAR or EAR). Every judge declares whether it
is `external`. The rule is one line: controlled data never goes to an external model.
The check runs in `check_criterion` *before* the context is gathered, so a blocked
criterion never reaches a prompt, a cache entry or a verdict row. `check_many` counts
and lists the blocked criteria rather than failing the run.

**Fail closed, twice.** A repository with no classification is treated as controlled,
and a model client that doesn't declare `external` is treated as external. Forgetting
a setting blocks data instead of leaking it.

**The approved internal model.** `ApprovedInternalModelClient` is the only client with
`external = False`. It is deliberately a stub: it raises a clear "not configured"
error, because the real transport depends on the approved deployment (endpoint, auth,
network path), and a guessed implementation would be worse than a loud failure. It
plugs into the same `LLMJudge`, so prompts, guardrails, budget, caching and review
work unchanged once it is wired to `INTERNAL_MODEL_URL`.

**Why here and not at the network edge.** Egress controls belong at the network too,
but the application is the only place that knows which repository a prompt came from.
Doing it in code also makes it testable: a test asserts that a recording client is
never called for a controlled repository.

**Tradeoff.** This is the light version. Classification is per repository, not per
file or path, and changes have no approval workflow. They are recorded in the audit
log (who, when, from what to what), but nobody has to approve them.

---

## 13. Testing strategy

**What.** 223 tests on SQLite; with Postgres enabled the 128 database-backed ones run
again, 351 in all,
every phase covered, including verification, the data policy, the audit log and the
eval gate. Real data where it matters: the seed is real git history and the schema runs
on real Postgres. Fakes where determinism matters:
- a fake GitHub that paginates, filters by `since` and rate-limits, and serves
  Actions runs and releases;
- a recording model client that proves controlled code is never sent;
- a scripted fake model that returns valid, invalid and deliberately compromised
  answers;
- injected sleep and clock in the API client.

The same suite runs on SQLite by default and on Postgres via `TEST_DATABASE_URL`.

**Why.** Tests that call real APIs are slow, flaky and cost money. Tests that only use
mocks miss real-database behaviour; running on Postgres found a genuine bug, a
connection left unusable after a failed statement.

**Tradeoff.** Fakes encode my understanding of GitHub's and the model API's behaviour;
if that understanding is wrong, the tests pass anyway. A small nightly job against the
real APIs would close that gap in production.

---

## 14. Trust: hierarchy, audit trail, full verdict record

These were the "next" items of the previous round (a requirement hierarchy, an
append-only audit trail, and a full record per AI verdict). Migration `007_trust`.

### 14.1 Requirement hierarchy with roll-ups

**What.** `requirements.parent_id` makes requirements a tree: SYS-01 (system) has
SUB-CLI-01 and SUB-HTTP-01 (subsystems), which hold the nine component requirements.
A parent's status is rolled up from its children and any criteria of its own, with the
same precedence as criteria: one failure fails the parent, one open re-verification
blocks it, and it is verified only when everything under it is. Each child is judged
against its own repository's build, so a cross-repo parent never pretends to have one
build.

**Why a tree, not a graph.** Real verification matrices decompose top-down, one parent
per requirement. A graph (one requirement satisfying two parents) makes roll-ups
ambiguous: does a failure count twice? A tree keeps the answer obvious.

**Cycles.** Refused three ways: the normalizer walks up from the proposed parent
before linking, the database refuses a requirement as its own parent, and a
recursive-CTE data-quality check catches any cycle written around the application.
Tests cover all three.

**Tradeoff.** Roll-ups are computed in Python on read. With 12 requirements that's
microseconds; with thousands I'd use a recursive CTE or a closure table.

### 14.2 An append-only, hash-chained audit log

**What.** `audit_log(recorded_at, effective_at, actor, action, entity, entity_key,
build_id, detail, prev_hash, hash)`. Events are written by the code that makes the
change, in the same transaction, for the decisions people later ask about: requirement
created or changed, verification recorded, derived, or invalidated, re-verification
opened, closed or superseded, AI verdict created or reviewed, classification changed.
Each row's hash covers its content plus the previous row's hash.

- **Append-only:** database triggers refuse UPDATE and DELETE (and TRUNCATE on
  Postgres).
- **Tamper-evident:** `devtrack audit --verify` recomputes the chain. A test drops
  the trigger, edits row 10, and checks the verifier names row 10.
- **No forks:** `prev_hash` is UNIQUE, and Postgres writers take an advisory lock, so
  two transactions can't both extend the same tail.
- **Two times:** `recorded_at` is when we wrote it; `effective_at` is when it
  happened (a sign-off date, a requirement change), which matters when loading
  history.

**Why application events, not generic row triggers.** Row triggers would log every
column change, without knowing *who* (the database user is the app) or *why*. The
application knows the actor (a reviewer's name, `ci`, `change-control`,
`checker:<model>`) and the build. That's the information an auditor asks for.

**Tradeoff.** Tamper-evident, not tamper-proof: someone with DDL rights can drop the
triggers and rebuild the whole chain. The fix is to anchor the latest hash outside our
control every day (section 17). Also, a code path that forgets to call
`audit.record` leaves a gap. The tests check that each audited action appears after a
seed load.

### 14.3 The full exchange behind every verdict

**What.** `ai_verdicts` now stores the system prompt and user prompt exactly as sent
(already redacted), every raw response (two if the output was repaired), the model
name the provider reported, the provider's request or session ids, and the stop
reason. The heuristic judge stores its model name and no prompt.

**Why.** "The model said pass" isn't reviewable. With the exact input and output, a
reviewer can see what the model saw, re-run it on another model, or show an auditor
why a criterion was flagged. It also makes prompt-injection incidents investigable
after the fact.

**Tradeoff.** Storage: about 10 to 20 KB per verdict. At 10,000 verdicts a month that
is a few hundred MB a year, which is fine; compression or cold storage after 90 days
if it grows. Prompts contain code, so the table inherits the repository's
classification: a controlled repository's prompts only ever exist for the internal
model.

---

## 15. Running the eval through Claude Code, and gating on it

### 15.1 The judge runs through Claude Code in headless mode

**What.** `CHECKER_BACKEND=claude-cli` (the default) runs each judgement as
`claude -p --output-format json` with our system prompt replacing Claude Code's
(`--system-prompt`), all tools disabled (`--tools ""`), no MCP servers, no slash
commands, no saved session, in an empty working directory. The prompt goes on stdin.
From the JSON we keep the answer, the model name the CLI reports in `modelUsage`, the
session id, token counts and the reported cost. `CHECKER_BACKEND=api` with
`ANTHROPIC_API_KEY` uses the Messages API instead; both feed the same judge, guardrails
and storage.

**Why.** The project owner has a Claude subscription and no API key, and the evals had
to be real runs, not projections. Headless Claude Code is the supported way to script
the subscription.

**What it costs, stated plainly:**
- Claude Code still adds its own context: about 13,000 cached input tokens per call
  in our runs, out of about 18,000. The judge sees our system prompt plus that.
- No temperature or output-token cap, so runs are less repeatable than on the API.
- The model name comes back as `claude-sonnet-5-5`, not a dated snapshot, so a
  silent model update between runs wouldn't show in the record.
- The reported cost is a list-price equivalent, not what the subscription bills.

So the API path stays in the code, behind the environment variable, for anyone who
needs an exact reproduction.

**Results** (48 labels; fail is the positive class; [docs/eval-report.md](docs/eval-report.md)):

| Judge | Accuracy | Fail precision | Fail recall | Unclear | Cost per run |
|---|---:|---:|---:|---:|---:|
| keyword heuristic | 60% | 64% | 56% | 27% | $0 |
| v1, claude-sonnet-5-5 via CLI | 96% | 94% | 94% | 8% | $1.25 |
| v2, claude-sonnet-5-5 via CLI | 100% | 100% | 100% | 8% | $1.22 |

### 15.2 The gate: never below the keyword baseline

**What.** `devtrack eval-gate`, run in CI on SQLite and on Postgres, fails the build
when fail detection (recall and precision on "fail") drops below the heuristic's, which
is recorded in `docs/eval-baseline.json`. It does three checks: it re-runs the
deterministic heuristic on a fresh seed, it checks every recorded model run in
`docs/evidence/eval-results.json`, and, on a manual CI run with an API key secret, it
runs the model live.

**Why gate on the baseline and not on 94%?** The heuristic re-run is free and
deterministic, so any drop is a real regression in ingestion, linking, context building
or the labels. A model floor of 94% would make CI flaky, since one criterion is two
points, and would bake in numbers measured against unverified labels.

**Why precision as well as recall?** A judge that calls everything "fail" has perfect
recall. The precision floor is what stops a degenerate judge passing; a test proves it.

### 15.3 The labels are the weak point, so measure them

The 48 labels are AI-generated (by Claude Code, during the build), and so was the v2
rubric. The judge is a Claude model. A perfect score in that setup most likely shows shared
judgment, not correctness. It's the classic evaluator-contamination problem, and I'd
rather say it than have a reviewer find it.

Labels are AI-generated with a two-pass independent agreement check. Ed specified the process; a third independent AI pass adjudicated the disagreements, which are listed for human review.

**The two-pass check** (`scripts/relabel_two_pass.py`). The same 20 sampled criteria
are labelled twice from scratch, each pass in a fresh Claude Code session with no tools
and no access to the repository, the stored labels or the other pass. Each label must
quote its evidence, and the script checks the quote is really in the material. A third
fresh session adjudicates disagreements without seeing the stored labels. Results:
pass 1 and pass 2 agreed on 17 of 20 (85%, Cohen's kappa 0.72); the adjudicated labels
agreed with the stored ones on 18 of 20 (90%, kappa 0.82). Every prompt and response is
in `seed/relabel_two_pass.json`.

**Why this design.** Separate processes, not one conversation asked twice, because a
model that can see its first answer anchors on it. Quotes checked in code because a
reason is only as good as the evidence it cites. A blind adjudicator because one that
can see the stored label will tend to agree with it. Stored labels are not changed by
the check: a disagreement is something for a person to decide.

**What it doesn't prove.** Every labeller is a Claude model, the same family as the
judge, so this measures stability, not correctness. `scripts/relabel.py` is the
human check: it shows a person exactly what the model sees, hides the stored label
until the end, and reports the same agreement numbers. It runs as a terminal tool or
as a self-contained page that works on a tablet. Kappa below about 0.6 would mean the
labels need work before any accuracy number means much.

---

## 16. The tradeoffs, revisited

With that brief (who uses this, what decision it supports, what can go
wrong), here is my verdict on each major choice.

| Decision | What it costs | Right? |
|---|---|---|
| **Postgres primary, SQLite fallback** | Two SQL dialects; CI runs both | Right for tests and the offline demo. Wrong for production, so production is Postgres only (cut) |
| **Keyword heuristic as baseline** | Weak: 60% accuracy, 0.56 fail recall | Right as a free, deterministic floor and a CI gate. Wrong as the production judge |
| **Model as advisory, verification evidence decides** | An AI "pass" never makes a requirement verified | Right. Certification and audit accept test and inspection records, not model opinions |
| **Polling sync with a watermark and overlap** | Up to one poll interval stale; some repeated requests | Right for v1. Webhooks next, with polling as reconciliation |
| **Stage raw, normalize per record** | About 2x storage; slower bulk loads | Right: replay after a parser fix beats re-fetching |
| **Synthetic backlog and CI in the seed** | The headline findings are about made-up work | Acceptable only because it's labelled everywhere it appears |
| **Status computed on read** | Recomputed per page view | Right at this size; materialize later |
| **Claude Code CLI for the eval** | Extra harness context, no temperature or output cap | Right for this project (no key needed); the API path stays for exact reproduction |

**What I cut to be production-ready next week:**
1. **pgvector and the "embeddings" framing.** Never exercised, and the "embedder"
   was lexical. Removed from the migration; renamed to lexical similarity.
2. **Model-written SQL in the question box.** Templates only. A semantic layer is the
   way back.
3. **SQLite as a production option.** Postgres only in production.

What I kept even though it's work: human approval of fails, the eval gate, the audit
log and the data-classification gate, because those are what an auditor asks about.

---

## 17. What I'd do next with more time

The next three problems, in order:

1. **Trustworthy labels.** The 48 labels were written by an AI during the build.
   Have two people relabel them with `scripts/relabel.py`, report Cohen's kappa,
   resolve disagreements, and grow the set to a few hundred with a held-out split.
   Until then every accuracy number, including the model's, is provisional.
2. **Real data at scale.** Install as a GitHub App (per-installation tokens, higher
   limits), move to webhooks with a nightly reconciliation sync, use conditional
   requests (ETags), and run each repo as an orchestrated job with alerts on sync lag.
   Then run it against a real internal repository for a month and measure it there.
3. **An audit trail that survives an insider.** Anchor the newest audit hash outside
   our control every day (a write-once bucket or a signed timestamp), and add an
   approval workflow with history for classification changes.

Also on the list: give the checker the linked tests' outcomes on the target build as
evidence; a semantic layer for the question box; materialized roll-ups for large
requirement trees; and per-path (not just per-repo) data classification.
