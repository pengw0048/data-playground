import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

type UploadedDataset = { id: string; uri: string; registrationId: string };
type Receipt = { datasetId: string; revisionId: string };

test("certifies a browser-uploaded Parquet source after a normal managed Write @ux-smoke", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const stamp = Date.now();
  const uploadedName = `row-identity-${stamp}.parquet`;
  const canvasId = `row-identity-certification-${stamp}`;
  const destination = `row-identity-certified-${stamp}.parquet`;
  let uploaded: UploadedDataset | null = null;
  try {
    await page.goto("/#/workspace");
    await page.getByRole("tab", { name: "Datasets" }).click();
    const uploadedResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/catalog/upload") &&
        response.request().method() === "POST",
    );
    await page.locator('input[type="file"]').setInputFiles({
      name: uploadedName,
      mimeType: "application/vnd.apache.parquet",
      buffer: await readFile(resolve(".e2e-workspace/data/images.parquet")),
    });
    uploaded = (await (await uploadedResponse).json()) as UploadedDataset;

    const canvas = {
      id: canvasId,
      name: "Row identity certification",
      version: 1,
      requirements: [],
      nodes: [
        {
          id: "source",
          type: "source",
          position: { x: 80, y: 80 },
          data: {
            title: "Uploaded Parquet",
            config: { uri: uploaded.uri, tableId: uploaded.id },
          },
        },
        {
          id: "write",
          type: "write",
          position: { x: 420, y: 80 },
          data: {
            title: "Managed Write",
            config: { filename: destination, writeMode: "overwrite" },
          },
        },
      ],
      edges: [{ id: "source-write", source: "source", target: "write" }],
    };
    expect(
      (await page.request.post("/api/canvas", { data: canvas })).ok(),
    ).toBeTruthy();
    await page.goto(`/#/canvas/${canvasId}`);
    await page.locator('.react-flow__node[data-id="write"]').click();
    const inspector = page.getByTestId("inspector");
    const runResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/run") &&
        response.request().method() === "POST",
    );
    await inspector.getByRole("button", { name: "Run", exact: true }).click();
    const started = await runResponse;
    expect(started.ok()).toBeTruthy();
    const { runId } = (await started.json()) as { runId: string };
    const publication = inspector.getByLabel("Write publication");
    await expect(
      publication.getByRole("button", { name: "Open exact revision" }),
    ).toBeVisible({ timeout: 30_000 });
    let receipt: Receipt | null = null;
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `/api/jobs?run_id=${encodeURIComponent(runId)}&limit=1`,
          );
          if (!response.ok()) return "pending";
          const body = (await response.json()) as {
            items: Array<{
              runId: string;
              status: string;
              outputReceipt?: Receipt | null;
            }>;
          };
          const job = body.items.find((item) => item.runId === runId);
          receipt = job?.status === "done" ? (job.outputReceipt ?? null) : null;
          return receipt ? "done" : "pending";
        },
        { timeout: 30_000 },
      )
      .toBe("done");
    if (!receipt)
      throw new Error("managed Write completed without an exact receipt");

    await page.goto(
      `/#/workspace/dataset%3A${encodeURIComponent(receipt.datasetId)}?scope=datasets&revision=${encodeURIComponent(receipt.revisionId)}&revisionDataset=${encodeURIComponent(receipt.datasetId)}`,
    );
    const history = page.getByTestId("dataset-revision-history");
    await expect(
      history.getByText(`Exact revision ${receipt.revisionId}`),
    ).toBeVisible({ timeout: 15_000 });
    await history.getByRole("button", { name: "Certify row identity" }).click();
    await history.getByRole("checkbox", { name: /^id/ }).check();
    await history.getByRole("button", { name: "Check scan cost" }).click();
    await expect(history.getByText("Ordered key schema:")).toBeVisible();
    await history
      .getByRole("button", {
        name: /Start certification|Confirm and start full scan/,
      })
      .click();
    await expect(history.getByText("Certified row identity")).toBeVisible({
      timeout: 30_000,
    });

    await page.reload();
    await expect(history.getByText("Certified row identity")).toBeVisible();
  } finally {
    if (uploaded)
      await page.request.delete(
        `/api/catalog/tables/${encodeURIComponent(uploaded.id)}`,
        { params: { expected_registration_id: uploaded.registrationId } },
      );
    await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`);
  }
});
