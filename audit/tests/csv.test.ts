import { describe, expect, it } from "vitest";
import { toCsv } from "../worker/csv";

describe("CSV export", () => {
  it("quotes commas, quotes, and newlines", () => {
    const output = toCsv([{ name: 'A, "B"', note: "line 1\nline 2" }], ["name", "note"]);
    expect(output).toContain('"A, ""B"""');
    expect(output).toContain('"line 1\nline 2"');
    expect(output.startsWith("\uFEFF")).toBe(true);
  });
});
