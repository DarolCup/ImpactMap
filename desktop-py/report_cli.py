"""Generate a shareable impact report without installing the desktop UI."""
import argparse
import os
from pathlib import Path

from analysis import AnalysisError, find_analyzer
from github_repo import analyze_github, parse_repository
from local_repo import analyze_example, analyze_local
from report import render_report
from result import preview_impact


def main() -> int:
    parser = argparse.ArgumentParser(description="ImpactMap 0.0.3: HTML-отчёт без PySide6")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--github", help="публичный GitHub-репозиторий или HTTPS-ссылка")
    source.add_argument("--local", type=Path, help="локальный Git-репозиторий")
    source.add_argument("--demo", action="store_true", help="учебный проект из трёх файлов")
    parser.add_argument("--file", help="путь файла для прогноза до изменения; без него — проверка diff")
    parser.add_argument("--output", type=Path, default=Path("impactmap-report.html"))
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "impactmap")
    parser.add_argument("--analyzer", default=os.environ.get("IMPACTMAP_ANALYZER"))
    args = parser.parse_args()
    try:
        if find_analyzer(args.analyzer) is None:
            raise AnalysisError("Сначала соберите анализатор: cargo build --release --locked --manifest-path analyzer-rs/Cargo.toml")
        if args.github:
            result = analyze_github(args.cache.expanduser(), parse_repository(args.github), args.analyzer)
        elif args.local:
            result = analyze_local(args.local.expanduser(), args.analyzer)
        else:
            result = analyze_example(args.analyzer)
        if args.file:
            result = preview_impact(result, args.file.replace("\\", "/"))
        args.output.write_text(render_report(result), encoding="utf-8")
    except (AnalysisError, OSError) as exc:
        parser.exit(1, f"ImpactMap: {exc}\n")
    print(f"Отчёт: {args.output.resolve()}\nПотенциально затронуто: {len(result.response['affected_files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
