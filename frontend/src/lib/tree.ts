import type { ClassSummary } from "../types/api";

export interface PackageGroup {
  name: string;
  classes: ClassSummary[];
}

/** Group classes by package, sorted by package then class name. Nested classes stay with their file. */
export function groupByPackage(classes: ClassSummary[], filter = ""): PackageGroup[] {
  const needle = filter.trim().toLowerCase();
  const groups = new Map<string, ClassSummary[]>();
  for (const c of classes) {
    if (needle && !c.fqn.toLowerCase().includes(needle)) continue;
    const key = c.package ?? "(default package)";
    groups.set(key, [...(groups.get(key) ?? []), c]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, items]) => ({ name, classes: items.sort((x, y) => x.fqn.localeCompare(y.fqn)) }));
}

/** `com.acme.Outer.Inner` shown relative to its package: `Outer.Inner`. */
export function shortName(c: ClassSummary): string {
  return c.package && c.fqn.startsWith(c.package + ".") ? c.fqn.slice(c.package.length + 1) : c.fqn;
}
