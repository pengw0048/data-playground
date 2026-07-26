import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  state: {} as any,
  preflight: vi.fn(),
  submit: vi.fn(),
  task: vi.fn(),
  cancel: vi.fn(),
}));
vi.mock("../store/graph", () => ({
  useStore: (selector: (state: any) => unknown) => selector(mocks.state),
}));
vi.mock("../api/client", () => ({
  api: {
    rowIdentityCertificationPreflight: mocks.preflight,
    submitRowIdentityCertification: mocks.submit,
    rowIdentityCertificationTask: mocks.task,
    cancelRowIdentityCertificationTask: mocks.cancel,
  },
  KernelError: class KernelError extends Error {
    status: number;
    constructor(status: number, text: string) {
      super(text);
      this.status = status;
    }
  },
}));

import { RowIdentityCertificationControl } from "./RowIdentityCertificationControl";

const detail = {
  datasetId: "dataset-1",
  revisionId: "revision-1",
  retentionOwner: "core" as const,
  summary: { rowCount: 4, totalBytes: 64, dataFileCount: 1, fragmentCount: 1 },
  preview: {
    columns: [
      { name: "order,id", type: "string", capabilities: [] },
      { name: "sequence", type: "int64", capabilities: [] },
    ],
    rows: [],
    rowIdentities: null,
    hasMore: false,
    rowLimit: 100 as const,
  },
  rowIdentity: {
    datasetId: "dataset-1",
    revisionId: "revision-1",
    proofStatus: "unavailable" as const,
    certificationSupported: true,
    fields: [],
    encodingVersion: null,
  },
};
const preflight = {
  datasetRef: {
    kind: "exact" as const,
    datasetId: "dataset-1",
    revisionId: "revision-1",
  },
  keyFields: [{ name: "order,id", arrowType: "string" }],
  schemaSha256: "schema",
  specSha256: "spec",
  estimatedScanRows: null,
  estimatedScanBytes: null,
  needsConfirmation: true,
  reason: "unknown_size" as const,
  supported: true,
  confirmationSha256: "confirmation",
};
const certificationTask = (
  taskId: string,
  keyColumns: string[],
  overrides: Record<string, unknown> = {},
) => ({
  taskId,
  status: "running",
  datasetId: "dataset-1",
  revisionId: "revision-1",
  schemaSha256: "schema",
  specSha256: "spec",
  keyColumns,
  canCancel: true,
  receipt: null,
  ...overrides,
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

describe("RowIdentityCertificationControl", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => {
        values.set(key, value);
      },
      removeItem: (key: string) => {
        values.delete(key);
      },
      clear: () => {
        values.clear();
      },
    });
    mocks.state = {
      workspaceDatasetQuery: "revision=revision-1&revisionDataset=dataset-1",
      currentUser: { id: "alice", name: "Alice" },
      setWorkspaceDatasetQuery: vi.fn((query: string) => {
        mocks.state.workspaceDatasetQuery = query;
      }),
    };
    mocks.preflight.mockResolvedValue(preflight);
    mocks.submit.mockResolvedValue({
      taskId: "task-1",
      status: "queued",
      datasetId: "dataset-1",
      revisionId: "revision-1",
      schemaSha256: "schema",
      specSha256: "spec",
      keyColumns: ["order,id"],
      canCancel: true,
      receipt: null,
    });
  });

  it("uses a declared key only as an unverified ordered suggestion and persists confirmation per principal, not in the route", async () => {
    const view = render(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={["order,id"]}
        onRefresh={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Certify row identity" }),
    );
    view.rerender(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={["order,id"]}
        onRefresh={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Declared key suggestion (not verified):"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check scan cost" }));
    await screen.findByText(/Size is unknown/);
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm and start full scan" }),
    );
    await waitFor(() =>
      expect(mocks.submit).toHaveBeenCalledWith(
        expect.objectContaining({
          keyColumns: ["order,id"],
          confirmationSha256: "confirmation",
        }),
      ),
    );
    const route = mocks.state.setWorkspaceDatasetQuery.mock.calls.at(-1)[0];
    expect(route).toContain("rowIdentityAction=certify");
    expect(route).toContain("rowIdentityTask=task-1");
    expect(route).not.toContain("submission");
    expect(route).not.toContain("rowIdentityKeys");
    expect(
      globalThis.localStorage.getItem(
        "dataplay.row-identity-certification.v1:alice:dataset-1:revision-1",
      ),
    ).toBeNull();
  });

  it("replays only a prior locally confirmed submission after a lost response", async () => {
    globalThis.localStorage.setItem(
      "dataplay.row-identity-certification.v1:alice:dataset-1:revision-1",
      JSON.stringify({
        submissionId: "persisted-id",
        keyColumns: ["order,id"],
        confirmationSha256: "confirmation",
        schemaSha256: "schema",
        specSha256: "spec",
      }),
    );
    mocks.submit.mockResolvedValueOnce({
      taskId: "recovered-task",
      status: "running",
      datasetId: "dataset-1",
      revisionId: "revision-1",
      schemaSha256: "schema",
      specSha256: "spec",
      keyColumns: ["order,id"],
      canCancel: true,
      receipt: null,
    });
    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify";
    render(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={[]}
        onRefresh={vi.fn()}
      />,
    );
    await screen.findByRole("button", { name: "Recover previous submission" });
    expect(mocks.submit).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: /order,id/ })).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "Check scan cost" }),
    ).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Recover previous submission" }),
    );
    await waitFor(() =>
      expect(mocks.submit).toHaveBeenCalledWith(
        expect.objectContaining({
          submissionId: "persisted-id",
          keyColumns: ["order,id"],
        }),
      ),
    );
    expect(mocks.state.setWorkspaceDatasetQuery.mock.calls.at(-1)[0]).toContain(
      "rowIdentityTask=recovered-task",
    );
  });

  it("clears the prior task immediately while a new route task loads and keeps it cleared when loading fails", async () => {
    const nextTask = deferred<ReturnType<typeof certificationTask>>();
    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify&rowIdentityTask=old-task";
    mocks.task.mockImplementation((taskId: string) =>
      taskId === "old-task"
        ? Promise.resolve(certificationTask("old-task", ["order,id"]))
        : nextTask.promise,
    );
    const view = render(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={[]}
        onRefresh={vi.fn()}
      />,
    );
    await screen.findByText("Certification running");
    expect(screen.getByRole("button", { name: "Cancel task" })).toBeVisible();

    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify&rowIdentityTask=new-task";
    view.rerender(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={[]}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.queryByText("Certification running")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel task" })).toBeNull();

    await act(async () => {
      nextTask.reject(new Error("new task unavailable"));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "new task unavailable",
    );
    expect(screen.queryByRole("button", { name: "Cancel task" })).toBeNull();
  });

  it("ignores a stale route response and cancels only the newly loaded task", async () => {
    const oldTask = deferred<ReturnType<typeof certificationTask>>();
    const newTask = deferred<ReturnType<typeof certificationTask>>();
    mocks.task.mockImplementation((taskId: string) =>
      taskId === "old-task" ? oldTask.promise : newTask.promise,
    );
    mocks.cancel.mockResolvedValue(
      certificationTask("new-task", ["sequence"], {
        status: "cancelled",
        canCancel: false,
      }),
    );
    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify&rowIdentityTask=old-task";
    const view = render(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={[]}
        onRefresh={vi.fn()}
      />,
    );

    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify&rowIdentityTask=new-task";
    view.rerender(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={[]}
        onRefresh={vi.fn()}
      />,
    );
    await act(async () => {
      newTask.resolve(certificationTask("new-task", ["sequence"]));
    });
    expect(await screen.findByText("Certification running")).toBeVisible();
    expect(screen.getByText("Exact key").parentElement).toHaveTextContent(
      "Exact key sequence",
    );

    await act(async () => {
      oldTask.resolve(certificationTask("old-task", ["order,id"]));
    });
    expect(screen.getByText("Exact key").parentElement).toHaveTextContent(
      "Exact key sequence",
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
    await waitFor(() => expect(mocks.cancel).toHaveBeenCalledWith("new-task"));
  });

  it("rejects a reopen response whose task identity does not match the requested route", async () => {
    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify&rowIdentityTask=requested-task";
    mocks.task.mockResolvedValue(
      certificationTask("different-task", ["order,id"]),
    );
    render(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={[]}
        onRefresh={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response did not match this exact task and revision",
    );
    expect(screen.queryByText("Certification running")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel task" })).toBeNull();
  });

  it("restores and locks the ordered key columns pinned by a reopened task", async () => {
    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify&rowIdentityTask=task-keys";
    mocks.task.mockResolvedValue(
      certificationTask("task-keys", ["sequence", "order,id"]),
    );
    render(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={["order,id"]}
        onRefresh={vi.fn()}
      />,
    );

    await screen.findByText("Certification running");
    expect(screen.getByText("Exact key").parentElement).toHaveTextContent(
      "Exact key sequence, order,id",
    );
    expect(screen.getByRole("checkbox", { name: /sequence/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /sequence/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /order,id/ })).toBeChecked();
  });

  it("deduplicates, schema-filters, and caps declared key suggestions before submission", async () => {
    const columnNames = Array.from(
      { length: 18 },
      (_, index) => `column-${index}`,
    );
    const manyColumnDetail = {
      ...detail,
      preview: {
        ...detail.preview,
        columns: columnNames.map((name) => ({
          name,
          type: "string",
          capabilities: [],
        })),
      },
    };
    const expectedKeys = columnNames.slice(0, 16);
    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify";
    render(
      <RowIdentityCertificationControl
        detail={manyColumnDetail}
        declaredKey={[
          columnNames[0],
          "not-in-schema",
          columnNames[0],
          ...columnNames.slice(1),
        ]}
        onRefresh={vi.fn()}
      />,
    );

    const suggestion = screen.getByText(
      "Declared key suggestion (not verified):",
    ).parentElement;
    expect(suggestion).toHaveTextContent(expectedKeys.join(", "));
    expect(suggestion).not.toHaveTextContent("not-in-schema");
    expect(
      screen
        .getAllByRole("checkbox")
        .filter((checkbox) => (checkbox as HTMLInputElement).checked),
    ).toHaveLength(16);

    fireEvent.click(screen.getByRole("button", { name: "Check scan cost" }));
    await waitFor(() =>
      expect(mocks.preflight).toHaveBeenCalledWith({
        datasetId: "dataset-1",
        revisionId: "revision-1",
        keyColumns: expectedKeys,
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Confirm and start full scan",
      }),
    );
    await waitFor(() =>
      expect(mocks.submit).toHaveBeenCalledWith(
        expect.objectContaining({ keyColumns: expectedKeys }),
      ),
    );
  });

  it("formats sub-MiB scan estimates in KiB", async () => {
    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify";
    mocks.preflight.mockResolvedValue({
      ...preflight,
      estimatedScanRows: 4,
      estimatedScanBytes: 1024,
    });
    render(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={["order,id"]}
        onRefresh={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Check scan cost" }));
    expect(await screen.findByText(/Full scan:/)).toHaveTextContent(
      "4 rows · 1.0 KiB",
    );
    expect(screen.queryByText(/0\.0 MiB/)).toBeNull();
  });

  it("shows a done conflicting certificate as an actionable terminal failure", async () => {
    mocks.state.workspaceDatasetQuery =
      "revision=revision-1&revisionDataset=dataset-1&rowIdentityAction=certify&rowIdentityTask=conflict";
    mocks.task.mockResolvedValueOnce({
      taskId: "conflict",
      status: "done",
      datasetId: "dataset-1",
      revisionId: "revision-1",
      schemaSha256: "schema",
      specSha256: "other",
      keyColumns: ["order,id"],
      canCancel: false,
      receipt: { outcome: "conflicting_retained_spec" },
    });
    render(
      <RowIdentityCertificationControl
        detail={detail}
        declaredKey={[]}
        onRefresh={vi.fn()}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "already retains a different",
    );
    expect(
      screen.getByRole("button", { name: "Refresh exact revision" }),
    ).toBeVisible();
  });
});
