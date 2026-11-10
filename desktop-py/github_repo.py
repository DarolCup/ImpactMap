from dataclasses import dataclass
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from analysis import AnalysisError, analyze, git, git_paths


REPOSITORY = "DarolCup/ImpactMap"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}.git"
BRANCH = "main"
CACHED_REF = f"refs/heads/{BRANCH}"


@dataclass
class GitHubResult:
    commit: str
    changes: list[str]
    response: dict | None = None
    example: bool = False


def analyze_github(cache: Path, analyzer: str | None) -> GitHubResult:
    # Keep checkout filters, credentials and repository overrides from the user's Git
    # setup out of the managed cache. Ordinary proxy environment variables still work.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )

    repository = cache / "github.git"
    repository.mkdir(parents=True, exist_ok=True)
    if not (repository / "HEAD").is_file():
        git(repository, "init", "--bare", "--template=", env=env)

    try:
        git(
            repository, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", REPOSITORY_URL,
            f"+refs/heads/{BRANCH}:{CACHED_REF}", env=env, timeout=90,
        )
    except AnalysisError as exc:
        raise AnalysisError(f"Не удалось обновить данные с GitHub.\n{exc}") from exc

    commit, *parents = git(
        repository, "rev-list", "--parents", "-n", "1", CACHED_REF, env=env,
    ).decode("ascii").split()
    if not parents:
        return GitHubResult(commit, [])

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
    return GitHubResult(commit, changes, response, example)
