import { useRef, useState, type KeyboardEvent } from "react";
import type { WorkbookKind } from "../api";
import { useMediaQuery } from "../hooks";
import { WorkbookViewer } from "./WorkbookViewer";

const PANELS: ReadonlyArray<{
  kind: WorkbookKind;
  heading: string;
  fileName: string;
  tabLabel: string;
}> = [
  {
    kind: "output",
    heading: "Initial workbook",
    fileName: "output.xlsx",
    tabLabel: "Initial workbook",
  },
  {
    kind: "target",
    heading: "Ground truth",
    fileName: "target.xlsx",
    tabLabel: "Ground truth",
  },
];

/**
 * Side by side only once each viewer still gets a usable width. Below this the
 * 20rem task sidebar would squeeze both grids to roughly 370px, which is worse
 * for auditing than tabbing between two full-width sheets.
 */
const SIDE_BY_SIDE_QUERY = "(min-width: 1400px)";

const RENDERING_WARNING =
  "Mog displays the values cached in the original XLSX file and does not recalculate formulas. " +
  "Cached values can be stale or differ from desktop Microsoft Excel. " +
  "If the ground truth looks suspicious, download the workbook and open it locally before deciding. " +
  "Drag a column-header boundary to adjust its width for this browser session; downloads remain unchanged.";

interface WorkbookPanelsProps {
  taskId: string;
  onApiError: (caught: unknown) => void;
}

export function WorkbookPanels({ taskId, onApiError }: WorkbookPanelsProps) {
  const sideBySide = useMediaQuery(SIDE_BY_SIDE_QUERY);
  const [activeKind, setActiveKind] = useState<WorkbookKind>("output");
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = PANELS.length - 1;
    let target: number | null = null;
    if (event.key === "ArrowRight") target = index === last ? 0 : index + 1;
    else if (event.key === "ArrowLeft") target = index === 0 ? last : index - 1;
    else if (event.key === "Home") target = 0;
    else if (event.key === "End") target = last;
    if (target === null) return;

    event.preventDefault();
    setActiveKind(PANELS[target].kind);
    tabRefs.current[target]?.focus();
  }

  return (
    <section className="workbooks" aria-label="Workbooks">
      <p className="callout callout--warning" role="note">
        <strong>Rendering caveat.</strong> {RENDERING_WARNING}
      </p>

      {!sideBySide && (
        <div className="tabs" role="tablist" aria-label="Workbook to view">
          {PANELS.map((panel, index) => (
            <button
              key={panel.kind}
              type="button"
              role="tab"
              id={`tab-${panel.kind}`}
              ref={(node) => {
                tabRefs.current[index] = node;
              }}
              aria-selected={activeKind === panel.kind}
              aria-controls={`tabpanel-${panel.kind}`}
              tabIndex={activeKind === panel.kind ? 0 : -1}
              className="tabs__tab"
              onClick={() => setActiveKind(panel.kind)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
            >
              {panel.tabLabel}
            </button>
          ))}
        </div>
      )}

      <div className={sideBySide ? "workbooks__grid" : "workbooks__stack"}>
        {PANELS.map((panel) => (
          <div
            key={panel.kind}
            id={sideBySide ? undefined : `tabpanel-${panel.kind}`}
            role={sideBySide ? undefined : "tabpanel"}
            aria-labelledby={sideBySide ? undefined : `tab-${panel.kind}`}
            // Hidden rather than unmounted: the sheet keeps its parsed workbook
            // and does not re-download when the auditor switches tabs.
            hidden={!sideBySide && activeKind !== panel.kind}
          >
            <WorkbookViewer
              key={taskId}
              taskId={taskId}
              kind={panel.kind}
              heading={`${panel.heading} — ${panel.fileName}`}
              fileName={panel.fileName}
              onApiError={onApiError}
            />
          </div>
        ))}
      </div>
    </section>
  );
}
