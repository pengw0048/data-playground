import { useEffect, useMemo, useRef, useState } from "react";
import { api, KernelError } from "../api/client";
import { useStore } from "../store/graph";
import type {
  DatasetRevisionDetail,
  RowIdentityCertificationPreflight,
  RowIdentityCertificationTask,
} from "../types/api";
import { Button } from "@/components/ui/button";

const ACTIVE = new Set(["queued", "running"]);
const SUCCESS = new Set(["certified", "already_certified_same_spec"]);
const MAX_KEY_COLUMNS = 16;
const TERMINAL_COPY: Record<string, string> = {
  certified: "Row identity is certified for this exact revision.",
  already_certified_same_spec:
    "This exact revision already has this certified row identity.",
  duplicate_key:
    "These key values are not unique. Choose a key that distinguishes every row.",
  null_key:
    "Some selected key values are blank. Choose key columns with a value for every row.",
  unsupported_type:
    "One or more selected key columns cannot be used for row identity.",
  conflicting_retained_spec:
    "This revision already retains a different certified row identity.",
  stale_or_unavailable_revision:
    "This exact revision is no longer available to scan.",
  cancelled: "Certification was cancelled before a result was retained.",
  failed:
    "Certification stopped because the background worker could not complete the scan.",
};

interface PendingCertification {
  submissionId: string;
  keyColumns: string[];
  confirmationSha256: string;
  schemaSha256: string;
  specSha256: string;
}

function pendingKey(userId: string, detail: DatasetRevisionDetail) {
  return `dataplay.row-identity-certification.v1:${userId}:${detail.datasetId}:${detail.revisionId}`;
}
function readPending(
  userId: string | undefined,
  detail: DatasetRevisionDetail,
): PendingCertification | null {
  if (!userId) return null;
  try {
    const value: unknown = JSON.parse(
      localStorage.getItem(pendingKey(userId, detail)) ?? "null",
    );
    if (!value || typeof value !== "object") return null;
    const item = value as Partial<PendingCertification>;
    return typeof item.submissionId === "string" &&
      typeof item.confirmationSha256 === "string" &&
      typeof item.schemaSha256 === "string" &&
      typeof item.specSha256 === "string" &&
      Array.isArray(item.keyColumns) &&
      item.keyColumns.every((key) => typeof key === "string")
      ? {
          submissionId: item.submissionId,
          keyColumns: item.keyColumns,
          confirmationSha256: item.confirmationSha256,
          schemaSha256: item.schemaSha256,
          specSha256: item.specSha256,
        }
      : null;
  } catch {
    return null;
  }
}
function newSubmissionId() {
  return globalThis.crypto.randomUUID();
}
function message(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
function definitelyRejected(error: unknown) {
  return (
    error instanceof KernelError && error.status >= 400 && error.status < 500
  );
}
function count(value?: number | null) {
  return value == null ? "unknown" : value.toLocaleString();
}
function bytes(value?: number | null) {
  return value == null
    ? "unknown"
    : value < 1024
      ? `${value} B`
      : value < 1024 * 1024
        ? `${(value / 1024).toFixed(1)} KiB`
        : `${(value / 1024 / 1024).toFixed(1)} MiB`;
}
function suggestedKeys(declaredKey: string[], schemaColumns: string[]) {
  const schema = new Set(schemaColumns);
  return [...new Set(declaredKey)]
    .filter((key) => schema.has(key))
    .slice(0, MAX_KEY_COLUMNS);
}

export function RowIdentityCertificationControl({
  detail,
  declaredKey,
  onRefresh,
}: {
  detail: DatasetRevisionDetail;
  declaredKey: string[];
  onRefresh: () => void;
}) {
  const encodedQuery = useStore((state) => state.workspaceDatasetQuery);
  const setEncodedQuery = useStore((state) => state.setWorkspaceDatasetQuery);
  const currentUser = useStore((state) => state.currentUser);
  const params = useMemo(
    () => new URLSearchParams(encodedQuery),
    [encodedQuery],
  );
  const actionOpen = params.get("rowIdentityAction") === "certify";
  const taskId = params.get("rowIdentityTask") || "";
  const schemaColumns = detail.preview.columns.map((column) => column.name);
  const defaultKeys = suggestedKeys(declaredKey, schemaColumns);
  const [keys, setKeys] = useState(defaultKeys);
  const [preflight, setPreflight] =
    useState<RowIdentityCertificationPreflight | null>(null);
  const [task, setTask] = useState<RowIdentityCertificationTask | null>(null);
  const [pending, setPending] = useState<PendingCertification | null>(null);
  const [busy, setBusy] = useState<"preflight" | "submit" | "cancel" | null>(
    null,
  );
  const [error, setError] = useState("");
  const requestGeneration = useRef(0);
  const refreshedTask = useRef("");
  const currentTask =
    task?.taskId === taskId &&
    task.datasetId === detail.datasetId &&
    task.revisionId === detail.revisionId
      ? task
      : null;

  const updateRoute = (next: { open?: boolean; task?: string }) => {
    const query = new URLSearchParams(encodedQuery);
    query.set("revision", detail.revisionId);
    query.set("revisionDataset", detail.datasetId);
    if (next.open === false) query.delete("rowIdentityAction");
    else query.set("rowIdentityAction", "certify");
    if (next.task) query.set("rowIdentityTask", next.task);
    else query.delete("rowIdentityTask");
    setEncodedQuery(query.toString());
  };
  const persistPending = (value: PendingCertification) => {
    if (!currentUser?.id) return false;
    try {
      localStorage.setItem(
        pendingKey(currentUser.id, detail),
        JSON.stringify(value),
      );
    } catch {
      setError(
        "This browser could not retain the confirmed submission, so no scan was started.",
      );
      return false;
    }
    setPending(value);
    return true;
  };
  const clearPending = () => {
    if (currentUser?.id) {
      try {
        localStorage.removeItem(pendingKey(currentUser.id, detail));
      } catch {
        // The server task route remains authoritative even if browser storage is unavailable.
      }
    }
    setPending(null);
  };

  useEffect(() => {
    requestGeneration.current += 1;
    const saved = readPending(currentUser?.id, detail);
    setPending(saved);
    setKeys(
      saved?.keyColumns.filter((key) => schemaColumns.includes(key)) ??
        defaultKeys,
    );
    setPreflight(null);
    setTask(null);
    setBusy(null);
    setError("");
    return () => {
      requestGeneration.current += 1;
    };
  }, [currentUser?.id, detail.datasetId, detail.revisionId]); // schema changes cannot occur for an exact revision

  useEffect(() => {
    // A refreshed exact detail is authoritative. Do not reopen the completed task retained in the
    // route after it has already produced this certified detail, or it would trigger another refresh.
    if (detail.rowIdentity.proofStatus === "certified") return;
    if (
      task?.taskId === taskId &&
      task.datasetId === detail.datasetId &&
      task.revisionId === detail.revisionId
    )
      return;
    const generation = ++requestGeneration.current;
    setTask(null);
    setError("");
    if (!taskId) return;
    let live = true;
    void api
      .rowIdentityCertificationTask(taskId)
      .then((next) => {
        if (!live || generation !== requestGeneration.current) return;
        if (
          next.taskId !== taskId ||
          next.datasetId !== detail.datasetId ||
          next.revisionId !== detail.revisionId
        ) {
          setError(
            "Couldn’t reopen this certification task because the response did not match this exact task and revision.",
          );
          return;
        }
        setTask(next);
        setKeys(next.keyColumns);
      })
      .catch((caught) => {
        if (live && generation === requestGeneration.current)
          setError(
            `Couldn’t reopen this certification task: ${message(caught)}`,
          );
      });
    return () => {
      live = false;
    };
  }, [currentUser?.id, detail.datasetId, detail.revisionId, detail.rowIdentity.proofStatus, taskId]);
  useEffect(() => {
    if (!currentTask || !ACTIVE.has(currentTask.status)) return;
    const expected = currentTask.taskId;
    const generation = requestGeneration.current;
    let live = true;
    const timer = window.setInterval(() => {
      void api
        .rowIdentityCertificationTask(expected)
        .then((next) => {
          if (
            live &&
            generation === requestGeneration.current &&
            next.taskId === expected &&
            next.datasetId === detail.datasetId &&
            next.revisionId === detail.revisionId
          )
            setTask(next);
        })
        .catch((caught) => {
          if (live && generation === requestGeneration.current)
            setError(message(caught));
        });
    }, 1500);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [currentTask, detail.datasetId, detail.revisionId]);
  useEffect(() => {
    if (
      currentTask?.status === "done" &&
      SUCCESS.has(currentTask.receipt?.outcome ?? "") &&
      refreshedTask.current !== currentTask.taskId
    ) {
      refreshedTask.current = currentTask.taskId;
      onRefresh();
    }
  }, [currentTask, onRefresh]);
  useEffect(() => {
    if (detail.rowIdentity.proofStatus !== "certified" || !taskId) return;
    const query = new URLSearchParams(encodedQuery);
    query.delete("rowIdentityAction");
    query.delete("rowIdentityTask");
    setEncodedQuery(query.toString());
  }, [detail.rowIdentity.proofStatus, encodedQuery, setEncodedQuery, taskId]);

  const recover = async (saved: PendingCertification) => {
    if (!currentUser?.id || busy) return;
    const generation = ++requestGeneration.current;
    setBusy("submit");
    setError("");
    try {
      const next = await api.submitRowIdentityCertification({
        datasetId: detail.datasetId,
        revisionId: detail.revisionId,
        keyColumns: saved.keyColumns,
        submissionId: saved.submissionId,
        confirmationSha256: saved.confirmationSha256,
      });
      if (generation !== requestGeneration.current) return;
      setTask(next);
      clearPending();
      updateRoute({ task: next.taskId });
    } catch (caught) {
      if (generation === requestGeneration.current) {
        if (definitelyRejected(caught)) clearPending();
        setError(message(caught));
      }
    } finally {
      if (generation === requestGeneration.current) setBusy(null);
    }
  };
  if (detail.rowIdentity.proofStatus === "certified")
    return (
      <section
        aria-label="Certified row identity"
        className="rounded-md border border-emerald-500/30 bg-emerald-500/5 p-2 text-[10.5px]"
      >
        <div className="font-semibold text-foreground">
          Certified row identity
        </div>
        <div className="mt-0.5 text-muted-foreground">
          Rows can be addressed by the ordered key{" "}
          <span className="font-mono">
            {detail.rowIdentity.fields.map((field) => field.name).join(", ")}
          </span>
          .
        </div>
      </section>
    );
  if (!detail.rowIdentity.certificationSupported) return null;
  const changeKeys = (next: string[]) => {
    if (next.length > MAX_KEY_COLUMNS) {
      setError("Choose at most 16 key columns for row identity.");
      return;
    }
    requestGeneration.current += 1;
    setKeys(next);
    setPreflight(null);
    setError("");
    setTask(null);
    clearPending();
    updateRoute({});
  };
  const check = async () => {
    if (!keys.length) {
      setError("Choose one or more key columns before checking the scan.");
      return;
    }
    if (keys.length > MAX_KEY_COLUMNS) {
      setError("Choose at most 16 key columns for row identity.");
      return;
    }
    const generation = ++requestGeneration.current;
    setBusy("preflight");
    setError("");
    setPreflight(null);
    try {
      const next = await api.rowIdentityCertificationPreflight({
        datasetId: detail.datasetId,
        revisionId: detail.revisionId,
        keyColumns: keys,
      });
      if (generation === requestGeneration.current) setPreflight(next);
    } catch (caught) {
      if (generation === requestGeneration.current) setError(message(caught));
    } finally {
      if (generation === requestGeneration.current) setBusy(null);
    }
  };
  const submit = async () => {
    if (!preflight || !keys.length || busy) return;
    if (keys.length > MAX_KEY_COLUMNS) {
      setError("Choose at most 16 key columns for row identity.");
      return;
    }
    if (!currentUser?.id) {
      setError("A confirmed user is required to start certification.");
      return;
    }
    const saved = {
      submissionId: newSubmissionId(),
      keyColumns: keys,
      confirmationSha256: preflight.confirmationSha256,
      schemaSha256: preflight.schemaSha256,
      specSha256: preflight.specSha256,
    };
    setError("");
    if (!persistPending(saved)) return;
    const generation = ++requestGeneration.current;
    setBusy("submit");
    try {
      const next = await api.submitRowIdentityCertification({
        datasetId: detail.datasetId,
        revisionId: detail.revisionId,
        keyColumns: keys,
        submissionId: saved.submissionId,
        confirmationSha256: preflight.needsConfirmation
          ? preflight.confirmationSha256
          : undefined,
      });
      if (generation === requestGeneration.current) {
        setTask(next);
        clearPending();
        updateRoute({ task: next.taskId });
      }
    } catch (caught) {
      if (generation === requestGeneration.current) {
        if (definitelyRejected(caught)) clearPending();
        setError(message(caught));
      }
    } finally {
      if (generation === requestGeneration.current) setBusy(null);
    }
  };
  const cancel = async () => {
    if (!currentTask || busy) return;
    const expected = currentTask.taskId;
    const generation = ++requestGeneration.current;
    setBusy("cancel");
    setError("");
    try {
      const next = await api.cancelRowIdentityCertificationTask(expected);
      if (generation === requestGeneration.current && next.taskId === expected)
        setTask(next);
    } catch (caught) {
      if (generation === requestGeneration.current) setError(message(caught));
    } finally {
      if (generation === requestGeneration.current) setBusy(null);
    }
  };
  const outcome =
    currentTask?.receipt?.outcome ??
    (currentTask?.status === "cancelled"
      ? "cancelled"
      : currentTask?.status === "failed" || currentTask?.status === "done"
        ? "failed"
        : "");
  const taskSucceeded = currentTask?.status === "done" && SUCCESS.has(outcome);
  const intentLocked = !!currentTask || !!pending;
  const selectionLocked = intentLocked || busy !== null;
  const chooseAnotherKey =
    currentTask != null &&
    ["duplicate_key", "null_key", "unsupported_type"].includes(outcome);
  const startAgain =
    currentTask != null && ["cancelled", "failed"].includes(outcome);
  const refreshExact =
    currentTask != null &&
    ["conflicting_retained_spec", "stale_or_unavailable_revision"].includes(
      outcome,
    );
  const startNewAdmission = () => {
    requestGeneration.current += 1;
    setTask(null);
    setPreflight(null);
    setError("");
    clearPending();
    updateRoute({});
  };

  return (
    <section
      aria-label="Row identity certification"
      className="rounded-md border border-border bg-muted/20 p-2 text-[10.5px]"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-semibold text-foreground">Row identity</div>
          <p className="mt-0.5 text-muted-foreground">
            A certified key lets this exact revision safely identify a row
            later. It scans every row; the preview cannot prove uniqueness.
          </p>
        </div>
        {!actionOpen && (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-[10.5px]"
            onClick={() => updateRoute({})}
          >
            Certify row identity
          </Button>
        )}
      </div>
      {actionOpen && (
        <>
          <div className="mt-2 rounded border border-border bg-background p-2">
            <div className="font-semibold text-foreground">
              Choose key columns in order
            </div>
            {defaultKeys.length > 0 && (
              <div className="mt-0.5 text-muted-foreground">
                Declared key suggestion (not verified):{" "}
                <span className="font-mono">{defaultKeys.join(", ")}</span>
              </div>
            )}
            {intentLocked && (
              <div className="mt-1 text-muted-foreground">
                This exact key order is locked while the tracked task remains
                available.
              </div>
            )}
            <div className="mt-1 grid gap-1">
              {schemaColumns.map((column) => {
                const index = keys.indexOf(column);
                return (
                  <div key={column} className="flex items-center gap-1">
                    <label className="min-w-0 flex-1">
                      <input
                        type="checkbox"
                        disabled={selectionLocked}
                        checked={index >= 0}
                        onChange={() =>
                          changeKeys(
                            index >= 0
                              ? keys.filter((key) => key !== column)
                              : [...keys, column],
                          )
                        }
                      />{" "}
                      <span className="font-mono">{column}</span>
                      {index >= 0 && (
                        <span className="ml-1 text-muted-foreground">
                          #{index + 1}
                        </span>
                      )}
                    </label>
                    {index >= 0 && (
                      <>
                        <button
                          type="button"
                          aria-label={`Move ${column} earlier`}
                          disabled={selectionLocked || index === 0}
                          onClick={() =>
                            changeKeys(
                              keys.map((key, i) =>
                                i === index - 1
                                  ? column
                                  : i === index
                                    ? keys[index - 1]
                                    : key,
                              ),
                            )
                          }
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          aria-label={`Move ${column} later`}
                          disabled={selectionLocked || index === keys.length - 1}
                          onClick={() =>
                            changeKeys(
                              keys.map((key, i) =>
                                i === index + 1
                                  ? column
                                  : i === index
                                    ? keys[index + 1]
                                    : key,
                              ),
                            )
                          }
                        >
                          ↓
                        </button>
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
          {!currentTask && !pending && (
            <Button
              size="sm"
              variant="outline"
              className="mt-2"
              disabled={busy === "preflight" || busy === "submit"}
              onClick={() => void check()}
            >
              {busy === "preflight"
                ? "Checking exact revision…"
                : "Check scan cost"}
            </Button>
          )}
          {preflight && !currentTask && !pending && (
            <div className="mt-2 rounded border border-border bg-background p-2">
              <div>
                Exact input{" "}
                <span className="font-mono">
                  {preflight.datasetRef.datasetId}@
                  {preflight.datasetRef.revisionId}
                </span>
              </div>
              <div>
                Schema and key specification are pinned for this request.
              </div>
              <div>
                Ordered key schema:{" "}
                <span className="font-mono">
                  {preflight.keyFields
                    .map((field) => `${field.name}: ${field.arrowType}`)
                    .join(", ")}
                </span>
              </div>
              <div>
                Full scan: {count(preflight.estimatedScanRows)} rows ·{" "}
                {bytes(preflight.estimatedScanBytes)}
              </div>
              {preflight.needsConfirmation && (
                <div className="mt-1 font-semibold text-foreground">
                  Size is{" "}
                  {preflight.reason === "unknown_size" ? "unknown" : "large"};
                  confirm this full scan before starting.
                </div>
              )}
              {!preflight.supported && (
                <div role="alert" className="mt-1 text-destructive">
                  These selected column types cannot be certified for row
                  identity.
                </div>
              )}
              <Button
                size="sm"
                className="mt-2"
                disabled={!preflight.supported || busy === "submit"}
                onClick={() => void submit()}
              >
                {busy === "submit"
                  ? "Starting…"
                  : preflight.needsConfirmation
                    ? "Confirm and start full scan"
                    : "Start certification"}
              </Button>
            </div>
          )}
          {currentTask && (
            <div className="mt-2 rounded border border-border bg-background p-2">
              <div className="font-semibold text-foreground">
                Certification {currentTask.status}
              </div>
              <div>
                Exact key{" "}
                <span className="font-mono">
                  {currentTask.keyColumns.join(", ")}
                </span>
              </div>
              {ACTIVE.has(currentTask.status) && (
                <div className="mt-1 text-muted-foreground">
                  This task remains available after navigation or restart in
                  Jobs and this exact revision.
                </div>
              )}
              {currentTask.status === "done" && (
                <div
                  role={taskSucceeded ? undefined : "alert"}
                  className={`mt-1 ${taskSucceeded ? "text-muted-foreground" : "text-destructive"}`}
                >
                  {TERMINAL_COPY[outcome]}
                </div>
              )}
              {currentTask.status === "failed" && (
                <div role="alert" className="mt-1 text-destructive">
                  {TERMINAL_COPY[outcome]}
                </div>
              )}
              {currentTask.status === "cancelled" && (
                <div className="mt-1 text-muted-foreground">
                  {TERMINAL_COPY.cancelled}
                </div>
              )}
              {currentTask.canCancel && (
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-2"
                  disabled={busy === "cancel"}
                  onClick={() => void cancel()}
                >
                  {busy === "cancel" ? "Cancelling…" : "Cancel task"}
                </Button>
              )}
              {chooseAnotherKey && (
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-2"
                  onClick={startNewAdmission}
                >
                  Choose another key
                </Button>
              )}
              {startAgain && (
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-2"
                  onClick={startNewAdmission}
                >
                  Start again
                </Button>
              )}
              {refreshExact && (
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-2"
                  onClick={onRefresh}
                >
                  Refresh exact revision
                </Button>
              )}
            </div>
          )}
          {!currentTask && pending && (
            <div className="mt-2 rounded border border-border bg-background p-2">
              <div className="text-muted-foreground">
                A confirmed certification submission did not return a task.
                Recover it with the same exact key and submission identity.
              </div>
              <Button
                size="sm"
                variant="outline"
                className="mt-2"
                disabled={busy === "submit"}
                onClick={() => void recover(pending)}
              >
                {busy === "submit"
                  ? "Recovering…"
                  : "Recover previous submission"}
              </Button>
            </div>
          )}
          {error && (
            <div role="alert" className="mt-2 text-destructive">
              {error}
            </div>
          )}
        </>
      )}
    </section>
  );
}
