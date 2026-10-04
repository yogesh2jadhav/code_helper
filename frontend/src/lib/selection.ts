export interface LineRange {
  start: number;
  end: number;
}

/** Click selects a line; shift-click extends from the existing anchor. Always returns start <= end. */
export function nextSelection(current: LineRange | null, line: number, shift: boolean): LineRange {
  if (shift && current) {
    return { start: Math.min(current.start, line), end: Math.max(current.end, line) };
  }
  return { start: line, end: line };
}

export function inRange(range: LineRange | null | undefined, line: number): boolean {
  return !!range && line >= range.start && line <= range.end;
}

export function describeRange(range: LineRange): string {
  return range.start === range.end ? `L${range.start}` : `L${range.start}-${range.end}`;
}
