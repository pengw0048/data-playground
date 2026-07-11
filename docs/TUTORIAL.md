# 5-minute tour — from a table to a saved result

This walks you from a fresh launch to a real, saved output using the sample data every install seeds.
You'll build **events → keep purchases → total per user → save** — clean, summarize, export.

## 0 · Launch

```bash
make setup && make run             # from a clone → serves on http://localhost:8471, opens a browser
# after setup the command is:  cd kernel && uv run dataplay
```

(Or `docker compose up` — see the README.) On first run it seeds three generic datasets you'll see in
the **Tables** view: `images` (with an embedding column), `movies`, and `events`
(`id, user_id, event, amount` — 2,000 rows). We'll use `events`.

> **In a hurry?** The file menu → **New from example → Purchases per user** drops this whole pipeline,
> ready to run. The steps below build it by hand so you see how each piece works.

## 1 · Add a source

The canvas starts empty with an **"Add a source"** prompt — click it (or open the **Add-node toolbar**
at the bottom → **Sources & sinks** → `source`). In the source card, pick the **events** dataset. The
card shows the bound dataset name and its output port turns solid (it's typed `dataset`).

## 2 · See the data (preview)

Hover the node and click the **eye** (preview) in its action bar. A sample opens in the Data viewer —
`id, user_id, event, amount`. Previews run on a bounded sample, so they're instant; the *same graph*
will run over the full dataset when you execute it.

## 3 · Keep only purchases (filter)

Click the source's **output port** — a menu offers the nodes that can accept its output. Pick
**filter** (it wires up automatically). In the filter card set the predicate to a SQL boolean:

```
event = 'purchase'
```

Preview the filter — now only purchase rows. (A `filter` builds SQL and pushes down, so it's cheap.)

## 4 · Catch bad rows before they skew the totals (assert — optional)

A guard against silently-wrong data, placed **inline** in the main path. From the filter's output port
add an **assert**, and set its predicate to what *should* hold for every row — say `amount > 0`. The
assert has two output ports: **passes** forwards *every* row through unchanged (wire this to the next
node — the aggregate below), while the default **violations** port is the rows that fail, so *previewing*
the assert shows exactly which purchases are bad (ideally none). Set **severity** to `error` and any
violation *fails the run before the write commits*, so a bad batch can't flow downstream or be published;
leave it `warn` to pass every row through and just record the count. (It checks *values*, unlike the
schema hints, which only check that a column exists.)

## 5 · Total spend per user (aggregate)

From the filter's output port — or, if you added the assert in step 4, from its **passes** port — add
an **aggregate** (or toolbar → **Compute** → `aggregate`). Set:

- **group by**: `user_id`
- **aggregations**: `sum(amount) AS spend, count(*) AS purchases`

Preview it — but a group-by can't be sampled honestly (a 2,000-row prefix would lie about the totals),
so instead of a wrong answer the panel says **"needs a full pass"** with a **Run a full pass →**
button. Click it: one row per user with their total spend, computed over the whole input. (That's the
honesty rule from the README — aggregates/writes refuse a sample rather than mislead.)

## 6 · Biggest spenders first (sort — optional)

Add a **sort** (toolbar → **Shape** → `sort`), set **by** to `spend DESC`. Because it sits downstream
of the aggregate, its preview also says **"needs a full pass"** — click **Run a full pass →**: top
spenders first.

## 7 · Save it (write + run)

Add a **write** (toolbar → **Sources & sinks** → `write`). Give it a file name like
`top_spenders.parquet`, leave mode **overwrite**, and pick a destination (defaults to *Workspace
outputs*). Click **Run (▶)** on the write node.

Writes need a full pass (they say so — "needs a full pass" — instead of pretending a sample is the
answer). When it finishes, the output is registered in the catalog: open **Tables** and you'll see
`top_spenders` — ready to be the `source` of the next canvas.

## What just happened

Each node **built one logical plan** (a DuckDB relation); the identical plan ran on a bounded
sample for each preview and over the full dataset out-of-core for the run — with live per-node progress,
and the finished run kept in **Run history** (native charts of run duration + per-node *plan-build* time —
the out-of-core engine defers the heavy work to the target's single pass, so those bars are plan
construction, not each node's share of the run). If a run fails, the panel names the node that broke and
suggests a fix. Edit any node and it — plus everything downstream — goes **stale**; re-running a target
whose inputs are unchanged reuses its content-addressed cached result rather than recomputing it.

## Where to go next

- **Let the agent build it** — open the Agent dock, type *"from events, total amount per user for
  purchases, keep the top spenders"*; it builds the same nodes (needs a model configured in Settings).
- **Make it trustworthy** — pin an **output-schema contract** on a code node (Inspector → *Output
  schema*) and turn on `enforce` to fail the run on drift, or reuse a named/versioned contract across
  canvases — the value-level companion to the `assert` node in step 4.
- **Add your own node** — [docs/PLUGINS.md](PLUGINS.md): a plugin node shows up typed & wired with no
  core edit.
- **Run it at scale** — the engine sorts multi-GB datasets under a small memory cap by spilling to
  disk, so the same graph runs over data bigger than RAM.
- **Collaborate / deploy** — the README covers real-time collab, multi-user auth, and scaling out.
