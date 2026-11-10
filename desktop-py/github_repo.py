from hashlib import sha256
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from analysis import AnalysisError, AnalysisResult, analyze, checkout_snapshot, git, isolated_git_env, parse_changes


REPOSITORY = "DarolCup/ImpactMap"
CACHED_REF = "refs/heads/selected"
REPOSITORY_PATTERN = re.compile(r"(?:https://github\.com/)?([a-z0-9-]+/[a-z0-9_.-]+)/?", re.IGNORECASE | re.ASCII)


def parse_repository(text: str) -> str:
    match = REPOSITORY_PATTERN.fullmatch(text.strip())
    if match:
        repository = match[1].removesuffix(".git")
        if repository.split("/")[1] not in ("", ".", ".."):
            return repository
    raise AnalysisError("Укажите публичный репозиторий GitHub в формате владелец/репозиторий или вставьте его HTTPS-ссылку.")


def analyze_github(cache: Path, repository_name: str, analyzer: str | None) -> AnalysisResult:
    env = isolated_git_env()

    url = f"https://github.com/{repository_name}.git"
    cache_key = sha256(repository_name.lower().encode("ascii")).hexdigest()
    repository = cache / f"github-{cache_key}.git"
    repository.mkdir(parents=True, exist_ok=True)
    if not (repository / "HEAD").is_file():
        git(repository, "init", "--bare", "--template=", env=env)

    try:
        head = git(repository, "ls-remote", "--symref", "--exit-code", url, "HEAD", env=env)
        branch = next(
            (line.removeprefix(b"ref: refs/heads/").removesuffix(b"\tHEAD").decode("utf-8")
             for line in head.splitlines()
             if line.startswith(b"ref: refs/heads/") and line.endswith(b"\tHEAD")),
            None,
        )
        if branch is None:
            raise AnalysisError("Не удалось определить ветку по умолчанию.")
        git(
            repository, "fetch", "--depth=2", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
            url, f"+refs/heads/{branch}:{CACHED_REF}", env=env, timeout=90,
        )
    except AnalysisError as exc:
        raise AnalysisError(f"Не удалось обновить {repository_name} с GitHub.\n{exc}") from exc

    commit, *parents = git(
        repository, "rev-list", "--parents", "-n", "1", CACHED_REF, env=env,
    ).decode("ascii").split()
    with TemporaryDirectory(prefix="impactmap-") as temporary:
        temp = Path(temporary)
        baseline = temp / "before"
        snapshot = temp / "after"
        checkout_snapshot(repository, commit, snapshot, env)
        if parents:
            parent = parents[0]
            checkout_snapshot(repository, parent, baseline, env)
            changes = parse_changes(git(
                repository, "diff", "--name-status", "-z", "-M", "--no-ext-diff",
                parent, commit, "--", env=env,
            ))
            comparison = f"Первый родитель {parent[:10]} → коммит {commit[:10]}"
            notes = ()
        else:
            baseline.mkdir()
            changes = parse_changes(git(
                repository, "diff-tree", "--root", "--no-commit-id", "--name-status", "-rz",
                commit, env=env,
            ))
            comparison = f"Пустой снимок → первый коммит {commit[:10]}"
            notes = ("У первого коммита нет родителя. Все его файлы считаются добавленными.",)
        response = analyze(snapshot, baseline, changes, analyzer)
    return AnalysisResult(f"{repository_name} / {branch}", comparison, changes, response, notes)
