import { useCallback, useMemo, useState } from "react";
import {
  MogSheet,
  type MogEmbedConfig,
  type MogEmbedEffectiveState,
  type MogEmbedHostPolicy,
} from "@mog-sdk/embed/react";
import { downloadWorkbook, fetchWorkbookBytes, type WorkbookKind } from "../api";
import { errorMessage } from "../format";
import { useElementSize } from "../hooks";

/**
 * Read-only but inspectable: cell selection is allowed so auditors can read
 * formulas, while edit, save, and export capabilities remain excluded.
 */
const VIEW_ONLY_CAPABILITIES = Object.freeze([
  "view.render",
  "view.select",
  "view.scroll",
  "view.zoom",
  "view.sheet-switch",
]);

const XLSX_CONTENT_TYPE =
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" as const;

interface WorkbookViewerProps {
  taskId: string;
  kind: WorkbookKind;
  heading: string;
  fileName: string;
  onApiError: (caught: unknown) => void;
}

export function WorkbookViewer({
  taskId,
  kind,
  heading,
  fileName,
  onApiError,
}: WorkbookViewerProps) {
  const [canvasRef, size] = useElementSize<HTMLDivElement>();
  const [loadError, setLoadError] = useState<Error | null>(null);
  const [ready, setReady] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  // Identity depends only on which workbook is shown, never on audit form state,
  // so re-renders of the surrounding workspace never reload the sheet.
  const config = useMemo<MogEmbedConfig>(
    () => ({
      source: { kind: "file", ref: `${taskId}/${kind}` },
      requestedMode: "readonly",
      requestedCapabilities: VIEW_ONLY_CAPABILITIES,
      requestedSavePolicy: "none",
      requestedCollaboration: "none",
      chrome: {
        formulaBar: true,
        sheetTabs: true,
        headers: true,
        gridlines: true,
        scrollbars: true,
        zoomControls: true,
      },
    }),
    [taskId, kind],
  );

  const hostPolicy = useMemo<MogEmbedHostPolicy>(
    () => ({
      async resolveSource() {
        const bytes = await fetchWorkbookBytes(taskId, kind);
        return { bytes, authorizationRef: `${taskId}/${kind}`, contentType: XLSX_CONTENT_TYPE };
      },
      resolveEffectiveState(): MogEmbedEffectiveState {
        return {
          mode: "readonly",
          capabilities: VIEW_ONLY_CAPABILITIES,
          deniedCapabilities: ["edit", "save", "export", "comment"],
          savePolicy: "none",
          collaboration: "none",
          dirty: false,
          saveState: "idle",
        };
      },
    }),
    [taskId, kind],
  );

  const handleReady = useCallback(() => {
    setReady(true);
    setLoadError(null);
  }, []);

  const handleError = useCallback(
    (caught: Error) => {
      setReady(false);
      setLoadError(caught);
      // `hostPolicy.resolveSource` rejections reach us unwrapped, so an expired
      // session surfacing here still has to end the session.
      onApiError(caught);
    },
    [onApiError],
  );

  const retry = useCallback(() => {
    setLoadError(null);
    setReady(false);
    setAttempt((value) => value + 1);
  }, []);

  const handleDownload = useCallback(async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      await downloadWorkbook(taskId, kind);
    } catch (caught) {
      setDownloadError(errorMessage(caught));
      onApiError(caught);
    } finally {
      setDownloading(false);
    }
  }, [taskId, kind, onApiError]);

  const measured = size.width > 0 && size.height > 0;

  return (
    <section className="viewer" aria-labelledby={`viewer-${kind}-heading`}>
      <header className="viewer__header">
        <div>
          <h3 id={`viewer-${kind}-heading`} className="viewer__title">
            {heading}
          </h3>
          <p className="viewer__file">
            <code>{fileName}</code>
            <span className="badge badge--muted">Read-only</span>
          </p>
        </div>
        <button
          type="button"
          className="button button--small"
          onClick={handleDownload}
          disabled={downloading}
        >
          {downloading ? "Preparing…" : `Download ${fileName}`}
        </button>
      </header>

      {downloadError !== null && (
        <p className="callout callout--error" role="alert">
          {downloadError}
        </p>
      )}

      <div className="viewer__canvas" ref={canvasRef}>
        {loadError !== null ? (
          <div className="viewer__fallback" role="alert">
            <p>
              <strong>This workbook could not be rendered in the browser.</strong>
            </p>
            <p className="viewer__fallback-detail">{loadError.message}</p>
            <p>
              You can still download the file and inspect it locally, and you can still submit
              your audit.
            </p>
            <button type="button" className="button button--small" onClick={retry}>
              Try rendering again
            </button>
          </div>
        ) : (
          <>
            {!ready && (
              <p className="viewer__loading" role="status">
                Loading {fileName}…
              </p>
            )}
            {measured && (
              <MogSheet
                key={`${taskId}:${kind}:${attempt}`}
                className="viewer__mog"
                config={config}
                hostPolicy={hostPolicy}
                width={size.width}
                height={size.height}
                mode="readonly"
                formulaBar
                scrollable
                onReady={handleReady}
                onError={handleError}
              />
            )}
          </>
        )}
      </div>
    </section>
  );
}
