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
    await page.getByRole("tab", { name: "Local catalog" }).click();
    const uploadedResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/catalog/upload") &&
        response.request().method() === "POST",
    );
    await page.locator('input[type="file"]').setInputFiles({
      name: uploadedName,
      mimeType: "application/vnd.apache.parquet",
      buffer: await readFile(resolve(".e2e-workspace/data/binary_media.parquet")),
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
    // This one-row binary input is inside the bounded direct-run envelope. Observe the request
    // before clicking because the managed Write starts without an extra size confirmation.
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
    const certifiedDetail = page.waitForResponse(
      (response) => {
        if (!response.url().endsWith("/api/catalog/revision-details")
          || response.request().method() !== "POST" || !response.ok()) return false;
        const body = response.request().postDataJSON() as {
          datasetId?: string; revisionId?: string;
        } | null;
        return body?.datasetId === receipt.datasetId && body.revisionId === receipt.revisionId;
      },
    );
    const mediaRequest = (column: "image" | "video") => page.waitForRequest(
      (request) => {
        if (!request.url().endsWith("/api/catalog/revision-media-cell") || request.method() !== "POST") return false;
        const body = request.postDataJSON() as {
          datasetId?: string; revisionId?: string; column?: string;
        } | null;
        return body?.datasetId === receipt.datasetId
          && body.revisionId === receipt.revisionId && body.column === column;
      },
      { timeout: 30_000 },
    );
    const imageRequest = mediaRequest("image");
    const videoRequest = mediaRequest("video");
    await history
      .getByRole("button", {
        name: /Start certification|Confirm and start full scan/,
      })
      .click();
    await expect(history.getByText("Certified row identity")).toBeVisible({ timeout: 30_000 });
    const refreshed = await certifiedDetail;
    const refreshedDetail = (await refreshed.json()) as {
      rowIdentity: { proofStatus: string };
      preview: { rowIdentities: Array<Array<{ name: string; arrowType: string; value: unknown }>> | null };
    };
    expect(refreshedDetail.rowIdentity.proofStatus).toBe("certified");
    const identity = refreshedDetail.preview.rowIdentities?.[0];
    if (!identity) throw new Error("certified exact detail did not include the preview row identity");
    await expect(history.getByLabel("Certified row identity")).toBeVisible({ timeout: 30_000 });

    // These are bytes inside the managed output, not direct URL fixtures. Read them only through
    // the certified exact endpoint. Assert the shipped renderer, rather than issuing a parallel
    // fetch in the test that could pass while the product UI remains broken.
    const exactPreview = history.getByText("Exact revision preview");
    await expect(exactPreview).toBeVisible({ timeout: 30_000 });
    await exactPreview.scrollIntoViewIfNeeded();
    const image = history.getByRole("img", { name: "Media image" });
    const video = history.getByLabel("Media video");
    await expect(image).toBeVisible({ timeout: 30_000 });
    await expect(video).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => image.evaluate((element) => {
      const media = element as HTMLImageElement;
      return media.complete && media.naturalWidth > 0;
    })).toBe(true);
    await expect.poll(() => video.evaluate((element) => (element as HTMLVideoElement).readyState))
      .toBeGreaterThanOrEqual(1);

    const imageBody = imageRequest.then((request) => request.postDataJSON() as {
      datasetId: string;
      revisionId: string;
      identity: Array<{ name: string; arrowType: string; value: unknown }>;
      column: string;
    });
    const videoBody = videoRequest.then((request) => request.postDataJSON() as {
      datasetId: string;
      revisionId: string;
      identity: Array<{ name: string; arrowType: string; value: unknown }>;
      column: string;
    });
    const exactIdentity = { datasetId: receipt.datasetId, revisionId: receipt.revisionId };
    await expect(imageBody).resolves.toEqual({ ...exactIdentity, identity, column: "image" });
    await expect(videoBody).resolves.toEqual({ ...exactIdentity, identity, column: "video" });

    // The same rendered cells remain usable at the documented minimum and reference desktop
    // viewports in both themes; resizing or retheming must not replace them with placeholders.
    for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(viewport);
      for (const theme of ["dark", "light"] as const) {
        const html = page.locator("html");
        // The Workspace route intentionally has no Canvas TopBar theme button. Apply the same
        // persisted root contract used by the theme controller so this exact-revision dialog stays
        // open while its token-driven renderer is checked in both modes.
        await page.evaluate((next) => {
          localStorage.setItem("dp-theme", next);
          if (next === "dark") document.documentElement.setAttribute("data-theme", "dark");
          else document.documentElement.removeAttribute("data-theme");
          window.dispatchEvent(new Event("dp-theme-change"));
        }, theme);
        if (theme === "dark") await expect(html).toHaveAttribute("data-theme", "dark");
        else await expect(html).not.toHaveAttribute("data-theme", "dark");
        await image.scrollIntoViewIfNeeded();
        await expect(image).toBeVisible();
        await expect(video).toBeVisible();
      }
    }

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
