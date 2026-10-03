"""Print the extracted AST of one Java file as JSON.

Usage: .venv/bin/python scripts/dump_ast.py path/to/File.java [--no-source]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analyzer import JavaParserAnalyzer  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    s = get_settings()
    analyzer = JavaParserAnalyzer(s.java_bin, s.analyzer_jar, s.analyzer_timeout_seconds)
    result = analyzer.analyze_files([Path(args[0])])[0]
    exclude = {"types": {"__all__": {"methods": {"__all__": {"source_text"}}}}} \
        if "--no-source" in sys.argv else None
    print(result.model_dump_json(by_alias=True, indent=2, exclude=exclude, exclude_none=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
