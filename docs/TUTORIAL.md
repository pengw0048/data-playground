# From Workspace to a reusable result

This tour follows one complete researcher loop: find a dataset in **Workspace**, make a Canvas
choice, inspect what a preview can honestly show, run a managed write, and return to the revision and
durable Job evidence afterward.

It uses the seeded `events` dataset and the shipped **Purchases per user** example. The result is the
managed `top_users` dataset, with its revision, lineage, Job, and Inbox outcome still available after
you leave the Canvas.

## 0. Launch

```bash
make setup && make run
```

Open <http://127.0.0.1:8471>. The first run creates a local Workspace and seeds `events`, `images`,
and `movies`. This tour starts with `events`, which has 2,000 rows and the columns `id`, `user_id`,
`event`, and `amount`.

## 1. Start in Workspace

Open **Workspace**, then select `events`. Its detail panel is the place to establish context before
making a graph: it shows the registered name and location, row and column counts, current version
information when the adapter provides it, columns, organization fields, and existing lineage.

Click **Preview**. The seeded file shows a **bounded prefix preview**: the panel says how many rows it
requested and returned, whether it knows the total, the input revision it read, and that the rows are
not representative or random. Treat it as a quick structural check, not as a statistical conclusion
about the whole dataset.

## 2. Choose the Canvas deliberately

The **Use** button in the dataset detail does not silently modify a graph. It opens a choice:

- **Explore in a new Canvas** creates a named, editable Canvas in the current Workspace and adds this
  dataset as a Source.
- **Choose a Canvas** lets you select one exact editable destination before adding the Source.

That is the path for your own analysis. For the rest of this short tour, open the Canvas app menu and
choose **Create example Canvas → Purchases per user**. It creates a separate, runnable Canvas whose
Source is the same seeded dataset.

## 3. Inspect the graph, then respect the full-pass boundary

The example is:

```text
events → filter purchases → aggregate by user → sort by total → Write top_users
```

Select the `events` Source and choose **View data** in the Inspector. Canvas previews report their
own bound: the source-read limit, returned rows, and the fact that an output prefix is not necessarily
the first rows of a final result.

Now select `aggregate` and choose **View data**. Its panel says **Not sample-previewable** and offers
**Run a full pass →**. A grouped aggregate needs every relevant input row; presenting a prefix total
as though it were the result would be misleading. This is deliberate: a preview is shown only when it
can preserve the operation's meaning. The same rule applies to writes and other global operations.

## 4. Publish a managed result

Select the `top_users` **Write** card. It is configured to publish to **Workspace outputs** as
`top_users`. The first run creates that managed dataset; a later run replaces it only after admitting
the exact currently published revision. Then choose **Rerun all** in the Canvas header.

The graph runs over the full input. When it finishes, the Write card reports its published revision
and row count. A successful managed write has a receipt: it identifies the output dataset and the
revision that was actually published, rather than merely saying that a node ran.

## 5. Leave the Canvas and inspect the evidence

You do not need to keep the Canvas open while work finishes or to prove what happened later.

1. Open **Jobs** from the navigation or Canvas app menu. The completed entry for **Purchases per
   user** shows the terminal state, backend, duration, row count, and **1 output published**. Expand
   it to open the Canvas, the Write node, or the retained output artifact.
2. Open **Inbox**. The terminal outcome states that `top_users` was written and records its row count;
   **Open job** returns to the same durable Job.
3. Return to **Workspace** and open `top_users`. Its detail panel has **Revision history**. Open the
   listed revision to inspect that exact retained result. Its lineage names `events` as a parent after
   this example has run.

These are different views of one outcome: Workspace organizes the dataset, the revision identifies a
retained state, Jobs records the submitted work, and Inbox is the researcher-facing terminal notice.

## 6. Continue from the result

Use `top_users` as a new Source when you want to extend the analysis, reopen **Purchases per user** to
change its graph, or reopen the Job when you need the recorded output evidence. A later catalog head
does not rewrite the identity of a completed managed publication or its admitted inputs.

## Build your own variant

After step 2, choose **Explore in a new Canvas** instead of the example. The Source is already added.
Use the Canvas Add controls to connect a filter, aggregate, or other typed operation, inspect each
step through **View data**, and add a **Write** only when you are ready to publish. The same
preview/full-pass and Jobs/Inbox rules apply.

## Where next

- [Catalog and Workspace guide](CATALOG.md) explains browsing, search, relationships, lineage, and
  revision inspection in more depth.
- [Versioned data and durable execution](VERSIONED_DATA_AND_DURABLE_EXECUTION.md) explains admitted
  inputs, revisions, receipts, and their supported boundaries.
- [Plugin onboarding](PLUGIN_ONBOARDING.md) shows where adapters, destinations, catalogs, and
  execution backends fit without changing the open-source core.
