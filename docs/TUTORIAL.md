# 5-minute tour

Build a small pipeline on the seeded sample data: events → purchases → spend per user → save.

## 0. Launch

```bash
make setup && make run
```

Open <http://127.0.0.1:8471>. After setup you can also start with:

```bash
cd kernel && uv run dataplay
```

For Docker Compose, follow the README migrate-then-start sequence. First run seeds three datasets in
Tables: `images` (with an embedding column), `movies`, and `events`
(`id`, `user_id`, `event`, `amount` — 2,000 rows). This tour uses `events`.

If you want the finished graph without building it by hand, use **New from example → Purchases per
user** in the file menu.

## 1. Add a source

The empty canvas prompts you to add a source. Click that, or use the Add-node toolbar →
**Sources & sinks** → `source`. Bind the **events** dataset. The output port fills in once the node
is typed as `dataset`.

## 2. Preview the rows

Hover the node and click the eye icon. The Data viewer shows a bounded sample of
`id`, `user_id`, `event`, `amount`. Previews stay on a sample so they stay fast. The same graph runs
over the full dataset when you execute it.

## 3. Keep only purchases

Click the source output port and pick **filter**. Set the predicate to:

```
event = 'purchase'
```

Preview again. Only purchase rows remain. `filter` lowers to SQL and pushes down.

## 4. Optional: assert on amounts

From the filter output, add an **assert** with predicate `amount > 0`. The node has two ports:

- `passes` forwards every input row unchanged — wire this downstream
- `violations` holds the failing rows — preview it to see bad purchases

With **severity** `error`, any violation fails the run before a write commits. With `warn`, every row
still passes through and the violation count is recorded. This checks values; schema hints only check
that a column exists.

## 5. Total spend per user

From the filter output — or from assert `passes` if you added step 4 — add an **aggregate**:

- group by: `user_id`
- aggregations: `sum(amount) AS spend, count(*) AS purchases`

A group-by cannot be sampled honestly from a prefix, so the panel says **needs a full pass** instead of
showing wrong totals. Click **Run a full pass →** for one row per user over the whole input.

Writes and global aggregates use that rule. Downstream ops that can preview faithfully (for example
`sort`) do not.

## 6. Optional: biggest spenders first

Add a **sort** with **by** set to `spend DESC`. Preview shows the ordered sample after the aggregate's
full pass has materialized upstream.

## 7. Save and run

Add a **write**. Name the file `top_spenders.parquet`, leave mode **overwrite**, and keep the default
Workspace outputs destination. Click **Run (▶)** on the write node.

Writes always need a full pass. When the run finishes, open Tables and you should see `top_spenders`,
ready to use as a later `source`.

## What just happened

Each node added one step to a logical plan. Previews ran on a bounded sample when that preserved
meaning; the full run executed the same plan out of core, with per-node progress and a Run history
entry. Edit a node and it — plus everything downstream — goes stale. Re-running a target whose inputs
have not changed can reuse a content-addressed cached result.

## Where next

- Agent dock: describe the same outcome and let a configured model build the nodes.
- Pin an output-schema contract on a code node (Inspector → Output schema) and set `enforce` to fail
  on drift.
- Add your own node with [PLUGINS.md](PLUGINS.md).
- The local engine spills sorts, joins, and aggregates to disk, so the same graph can run past RAM.
- The README covers collaboration, auth, and multi-instance deploy.
