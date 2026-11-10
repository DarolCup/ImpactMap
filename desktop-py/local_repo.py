import os
from pathlib import Path
import shutil
import stat
from tempfile import TemporaryDirectory

from analysis import (
    EXAMPLE, AnalysisError, AnalysisResult, analyze, checkout_snapshot, find_analyzer, git,
    git_paths, isolated_git_env, parse_changes, parse_response,
)


def git_path(output: bytes) -> Path:
    output = output.removesuffix(b"\n")
    if os.name == "nt":
        output = output.removesuffix(b"\r")
    return Path(os.fsdecode(output))


def copy_worktree(root: Path, snapshot: Path, paths: list[str]) -> None:
    snapshot.mkdir()
    for path in paths:
        source = root / path
        try:
            # Tracked files may have been replaced by a symlink or junction to another tree.
            if any(
                parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
                for parent in source.parents if parent != root and root in parent.parents
            ):
                continue
            mode = source.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            destination = snapshot / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)


def configure_snapshot(root: Path, repository: Path, env: dict) -> None:
    # Match normal checkout line endings and symlink handling without loading
    # the user's filter drivers or other repository configuration.
    for key in ("core.autocrlf", "core.eol", "core.symlinks"):
        try:
            value = os.fsdecode(git(root, "config", "--get", key)).rstrip("\r\n")
        except AnalysisError:
            continue
        git(repository, "config", key, value, env=env)


def analyze_local(folder: Path, analyzer: str | None, *, demo: bool = False) -> AnalysisResult:
    root = git_path(git(folder, "rev-parse", "--show-toplevel"))
    try:
        head = git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    except AnalysisError as exc:
        raise AnalysisError("В локальном репозитории нужен хотя бы один коммит для сравнения с HEAD.") from exc

    with TemporaryDirectory(prefix="impactmap-") as temporary:
        temp = Path(temporary)
        baseline = temp / "before"
        snapshot = temp / "after"
        objects = git_path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
        env = isolated_git_env()
        repository = temp / "snapshot.git"
        repository.mkdir()
        git(repository, "init", "--bare", "--template=", env=env)
        (repository / "objects" / "info" / "alternates").write_bytes(os.fsencode(objects.as_posix()) + b"\n")
        configure_snapshot(root, repository, env)
        checkout_snapshot(repository, head, baseline, env)
        tracked = git(root, "ls-tree", "--full-tree", "-rz", "--name-only", head)
        working = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        copy_worktree(root, snapshot, git_paths(tracked + working))
        changes = parse_changes(git(
            temp, "diff", "--no-index", "--no-ext-diff", "--name-status", "-z", "-M",
            "--", "before", "after", env=env, diff=True,
        ), snapshot_paths=True)

        notes = ()
        if demo and find_analyzer(analyzer) is None:
            response = parse_response(EXAMPLE.read_bytes())
            notes = ("Анализатор не найден. Показан сохранённый результат для этого примера.",)
        else:
            response = analyze(snapshot, baseline, changes, analyzer)
    return AnalysisResult(
        "Учебный проект · Python" if demo else str(root),
        f"HEAD {head[:10]} → рабочая папка", changes, response, notes,
    )


def analyze_example(analyzer: str | None) -> AnalysisResult:
    with TemporaryDirectory(prefix="impactmap-example-") as temporary:
        repo = Path(temporary)
        env = isolated_git_env()
        git(repo, "init", "--template=", env=env)
        (repo / "runner.py").write_text("import app\n\napp.run()\n", encoding="utf-8")
        (repo / "app.py").write_text(
            "from helpers import greet\n\ndef run():\n    print(greet('ImpactMap'))\n", encoding="utf-8",
        )
        helpers = repo / "helpers.py"
        helpers.write_text('def greet(name):\n    return f"Привет, {name}!"\n', encoding="utf-8")
        git(repo, "add", "--all", env=env)
        git(repo, "-c", "user.name=ImpactMap", "-c", "user.email=example@impactmap.invalid",
            "commit", "-m", "Initial example", env=env)
        helpers.write_text(
            'def greet(name, punctuation):\n    return f"Привет, {name}{punctuation}"\n', encoding="utf-8",
        )
        return analyze_local(repo, analyzer, demo=True)
