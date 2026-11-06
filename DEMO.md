# Demo script (4 minutes)

For showing the project live, or for a reviewer clicking through alone. Times are
cumulative. Screenshots of each stop are in [docs/screenshots/](docs/screenshots/).

**Before you start** (once, about a minute):

```bash
pip install -e ".[dev]"
devtrack init && devtrack seed       # real git history of 3 repos + the synthetic backlog
devtrack serve                       # http://127.0.0.1:8000
```

Say up front: *the commits, PRs and release tags are real; the backlog, the CI builds
and the sign-offs are synthetic and flagged as such. The findings show what the tool
can see, not facts about these open-source projects.*

---

### 0:00 Overview: the question it answers

Open **Overview**.

> "A release lead has one question: can we sign off this build, and if not, what
> blocks it? The top row answers that: how many requirements are fully verified on the
> latest build, how many fail, and how many open re-verification items exist because a
> requirement changed after it was verified."

### 0:40 Verification: one failure, traced to a test on a build

Open **Verification**. Point at the tree.

> "Requirements form a tree. SYS-01 is failed because SUB-CLI-01 is failed because
> REQ-CLICK-01 is failed. Each requirement names its method: test, analysis,
> inspection or demonstration. Only that kind of evidence counts."

Click **REQ-CLICK-01**.

> "Criterion 101-3 fails on build #1049: this test, with this message. Every result
> names the build it applies to. That's the regression we scripted into the synthetic
> CI history on 1 October."

Switch the target to the **8.4.0** release.

> "Same question against a release. Results from later builds are ignored."

### 1:30 Change control: evidence that stopped counting

Scroll to **REQ-REQ-02**.

> "This requirement changed to version 3 on 20 June. Its three analyses were done
> before that, so they're invalidated, not deleted, and each criterion has an open
> re-verification item until someone signs off again."

Click **history** next to AC-REQ-202-1.

### 2:00 Audit: who, what, when, which build

The **Audit** page opens filtered to that criterion.

> "Every decision is an event: the re-verification item v2 opened, Nina's sign-off on
> build #1046 that closed it, then the invalidation when v3 landed and the new item it
> opened. The log is append-only, the database refuses edits, and each row carries the
> hash of the one before. The green badge means every hash was recomputed on this
> page load."

Optional, in a terminal: `devtrack audit --verify`.

### 2:40 The AI checker: advisory, checkable, reproducible

Open **AI verdicts**.

> "The model reads the diffs behind each criterion and returns pass, fail or unclear,
> quoting the code as evidence. A pass without a verbatim quote is downgraded. A fail
> waits for a named person. Every verdict stores the exact prompt, the raw response
> and the model that answered."

Open **Evals**.

> "We measure it: 48 labelled criteria, fail as the positive class, against a free
> keyword baseline. CI fails if fail detection drops below that baseline. One caveat I
> say out loud: the labels are AI-generated. Two independent AI passes agreed on 17 of
> 20 sampled labels, and a third AI pass adjudicated the disagreements, but every labeller is a Claude
> model like the judge. A person relabelling blind is the next step, and the tool for
> it is ready."

### 3:30 Close

> "What I'd do next: get a person to relabel and report agreement, replace polling
> with a GitHub App and webhooks, and anchor the audit chain outside our own database.
> The reasoning for those cuts is in DESIGN-DECISIONS.md."

---

**If asked for one command that proves it's real:**

```bash
pytest -q                       # 200+ tests, SQLite; set TEST_DATABASE_URL for Postgres
devtrack eval-gate              # fails if fail detection drops below the baseline
devtrack status --criteria      # the verification tree in a terminal
```
