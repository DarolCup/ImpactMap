from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from analysis import AnalysisError, analyze, git, git_paths


REPOSITORY = "DarolCup/ImpactMap"
CACHED_REF = "refs/heads/selected"
REPOSITORY_PATTERN = re.compile(r"(?:https://github\.com/)?([a-z0-9-]+/[a-z0-9_.-]+)/?", re.IGNORECASE | re.ASCII)


@dataclass
class GitHubResult:
    repository: str
    branch: str
    commit: str
    changes: list[str]
    response: dict | None = None
    example: bool = False


def parse_repository(text: str) -> str:
    match = REPOSITORY_PATTERN.fullmatch(text.strip())
    if match:
        repository = match[1].removesuffix(".git")
        if repository.split("/")[1] not in ("", ".", ".."):
            return repository
    raise AnalysisError("Укажите публичный репозиторий GitHub в формате владелец/репозиторий или вставьте его HTTPS-ссылку.")


def analyze_github(cache: Path, repository_name: str, analyzer: str | None) -> GitHubResult:
    # Keep checkout filters, credentials and repository overrides from the user's Git
    # setup out of the managed cache. Ordinary proxy environment variables still work.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )

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
    if not parents:
        return GitHubResult(repository_name, branch, commit, [])

    changes = git_paths(git(
        repository, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff",
        parents[0], commit, "--", env=env,
    ))
    with TemporaryDirectory(prefix="impactmap-") as temporary:
        # A private index lets concurrent windows materialize independent snapshots.
        temp = Path(temporary)
        snapshot = temp / "snapshot"
        snapshot.mkdir()
        snapshot_env = {**env, "GIT_INDEX_FILE": str(temp / "index")}
        work_tree = f"--work-tree={snapshot}"
        git(repository, work_tree, "read-tree", commit, env=snapshot_env)
        git(repository, work_tree, "checkout-index", "--all", env=snapshot_env)
        response, example = analyze(snapshot, changes, analyzer)
    return GitHubResult(repository_name, branch, commit, changes, response, example)
