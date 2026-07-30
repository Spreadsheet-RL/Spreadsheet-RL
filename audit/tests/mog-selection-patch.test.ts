import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const MOG_RUNTIME_BUNDLES = [
  "index.js",
  "index.cjs",
  "react.js",
  "react.cjs",
  "web-component.js",
  "web-component.cjs",
] as const;

describe("Mog read-only interaction patch", () => {
  it.each(MOG_RUNTIME_BUNDLES)("supports selection, navigation, and view-only column resizing in %s", async (bundle) => {
    const source = await readFile(
      join(process.cwd(), "node_modules", "@mog-sdk", "embed", "dist", bundle),
      "utf8",
    );

    expect(source).toContain("this._view.renderState.update({");
    expect(source).toContain("selection: { ranges: [range3], activeCell: { row, col } }");
    expect(source).toContain('this._canvasArea.addEventListener("keydown", this._onKeyDown);');
    expect(source).toContain("ArrowUp: { row: -1, col: 0 }");
    expect(source).toContain("ArrowDown: { row: 1, col: 0 }");
    expect(source).toContain("ArrowLeft: { row: 0, col: -1 }");
    expect(source).toContain("ArrowRight: { row: 0, col: 1 }");
    expect(source).toContain("this._view.viewport.getScrollToCell({ row, col })");
    expect(source).toContain('this._canvasArea.removeEventListener("keydown", this._onKeyDown);');
    expect(source).toContain("this._formulaBar?.setRef(row, col);");

    expect(source).toContain("this._colWidthOverrides = /* @__PURE__ */ new Map();");
    expect(source).toContain("setColumnWidthOverride(col, width)");
    expect(source).toContain("replaceColumnWidthOverrides(overrides)");
    expect(source).toContain("var MIN_SESSION_COLUMN_WIDTH = 24;");
    expect(source).toContain('el.addEventListener("pointerdown", onPointerDown);');
    expect(source).toContain('el.addEventListener("pointermove", onPointerMove);');
    expect(source).toContain('el.addEventListener("pointerup", onPointerUp);');
    expect(source).toContain('el.removeEventListener("pointerdown", onPointerDown);');
    expect(source).toContain('el.removeEventListener("pointermove", onPointerMove);');
    expect(source).toContain('el.removeEventListener("pointerup", onPointerUp);');
    expect(source).toContain('hit.type !== "columnResize"');
    expect(source).toContain("(e.clientX - drag.startClientX) / zoom");
    expect(source).toContain("this._positionIndex.setColumnWidthOverride(col, width);");
    expect(source).toContain("This view-only change is not saved.");

    expect(source).toContain("await computeBridge.importFromXlsxBytes(xlsxBytes, false);");
    expect(source).not.toContain("await computeBridge.importFromXlsxBytesDeferred(xlsxBytes);");
    expect(source).toContain("this.deferredHydrationPending = false;");
    expect(source).toContain("this.importDurabilityPending = false;");
    expect(source).toContain("this.materializationTracker.markAllMaterialized();");

    const methodStart = source.indexOf("_setSessionColumnWidth(col, width)");
    const methodEnd = source.indexOf("\n  /**", methodStart);
    const method = source.slice(methodStart, methodEnd);
    expect(methodStart).toBeGreaterThan(-1);
    expect(methodEnd).toBeGreaterThan(methodStart);
    expect(method).not.toContain("computeBridge");
    expect(method).not.toContain("setColWidth(");
  });
});
