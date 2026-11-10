from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess


PROTOCOL_VERSION = 1
EXAMPLE = Path(__file__).with_name("example.json")
PROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ANALYZER_NAME = "impactmap-analyzer.exe" if os.name == "nt" else "impactmap-analyzer"
BUILT_ANALYZER = Path(__file__).resolve().parents[1] / "analyzer-rs" / "target" / "release" / ANALYZER_NAME


class AnalysisError(Exception):
    pass


def git(repo: Path, *args: str, env: dict[str, str] | None = None, timeout: int = 30, diff: bool = False) -> bytes:
    env = {**(os.environ if env is None else env), "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
            capture_output=True,
            env=env,
            timeout=timeout,
            creationflags=PROCESS_FLAGS,
        )
    except FileNotFoundError as exc:
        raise AnalysisError("Git не найден в PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError(f"Git не ответил за {timeout} секунд.") from exc
    if result.returncode and not (diff and result.returncode == 1):
        message = os.fsdecode(result.stderr).strip()
        raise AnalysisError(f"Ошибка Git: {message or result.returncode}")
    return result.stdout


def git_paths(output: bytes) -> list[str]:
    # Git writes repository-relative paths with forward slashes, even on Windows.
    return sorted({os.fsdecode(path) for path in output.split(b"\0") if path})


@dataclass(frozen=True)
class Change:
    path: str
    status: str
    old_path: str | None = None

    def request(self) -> dict:
        result = {"path": self.path, "status": self.status}
        if self.old_path is not None:
            result["old_path"] = self.old_path
        return result


@dataclass
class AnalysisResult:
    source: str
    comparison: str
    changes: list[Change]
    response: dict
    notes: tuple[str, ...] = ()


def parse_changes(output: bytes, snapshot_paths: bool = False) -> list[Change]:
    fields = iter(os.fsdecode(output).split("\0"))
    changes = []
    statuses = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
    for status in fields:
        if not status:
            break
        path = next(fields)
        if status.startswith("R"):
            new_path = next(fields)
            changes.append(Change(
                new_path.removeprefix("after/") if snapshot_paths else new_path,
                "renamed",
                path.removeprefix("before/") if snapshot_paths else path,
            ))
        else:
            if snapshot_paths:
                path = path.removeprefix("after/" if status == "A" else "before/")
            changes.append(Change(path, statuses[status]))
    return sorted(changes, key=lambda item: item.path)


def isolated_git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {**env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0"}


def checkout_snapshot(repository: Path, commit: str, destination: Path, env: dict) -> None:
    destination.mkdir()
    private_env = {**env, "GIT_INDEX_FILE": str(destination.with_suffix(".index"))}
    work_tree = f"--work-tree={destination}"
    git(repository, work_tree, "read-tree", commit, env=private_env)
    git(repository, work_tree, "checkout-index", "--all", env=private_env)


def find_analyzer(setting: str | None) -> Path | None:
    if not setting:
        return BUILT_ANALYZER if BUILT_ANALYZER.is_file() else None
    executable = Path(os.path.expanduser(setting))
    if executable.is_file():
        return executable.resolve()
    # A bare executable name may also refer to a command on PATH.
    if executable.name == setting and (found := shutil.which(setting)):
        return Path(found).resolve()
    return None


def parse_response(data: bytes) -> dict:
    try:
        response = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AnalysisError("Ответ анализатора должен быть JSON в кодировке UTF-8.") from exc

    if not isinstance(response, dict) or type(response.get("protocol_version")) is not int:
        raise AnalysisError("В ответе анализатора отсутствует версия протокола.")
    if response["protocol_version"] != PROTOCOL_VERSION:
        raise AnalysisError(f"Версия протокола анализатора: {response['protocol_version']}. Ожидается 1.")

    fields = {
        "changed_files": str,
        "affected_files": {"path": str, "distance": int, "reason": str},
        "edges": {"from": str, "to": str, "relation": str},
        "warnings": str,
    }
    for name, schema in fields.items():
        items = response.get(name)
        if not isinstance(items, list):
            raise AnalysisError(f"Поле «{name}» в ответе анализатора должно быть списком.")
        for item in items:
            if isinstance(schema, dict):
                valid = isinstance(item, dict) and all(
                    type(item.get(key)) is value_type for key, value_type in schema.items()
                )
                if valid and name == "affected_files":
                    valid = item["distance"] >= 0
            else:
                valid = isinstance(item, schema)
            if not valid:
                raise AnalysisError(f"Некорректный элемент в поле «{name}» ответа анализатора.")
    for item in response["affected_files"]:
        if not strings(item.get("chain")):
            item.pop("chain", None)

    impact = response.get("impact")
    if isinstance(impact, dict):
        findings = impact.get("findings")
        limitations = impact.get("limitations")
        response["impact"] = {
            "findings": [item for item in findings if valid_finding(item)] if isinstance(findings, list) else [],
            "limitations": [item for item in limitations if isinstance(item, str)] if isinstance(limitations, list) else [],
        }
    else:
        response.pop("impact", None)
    return response


def strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def valid_finding(item) -> bool:
    return isinstance(item, dict) and strings(item.get("paths")) and all(
        isinstance(item.get(key), str) for key in ("code", "severity", "confidence", "evidence")
    )


def analyze(repo: Path, baseline: Path, changes: list[Change], setting: str | None) -> dict:
    executable = find_analyzer(setting)
    if executable is None:
        raise AnalysisError(
            "Не найден анализатор ImpactMap. Соберите его по инструкции в README "
            "или задайте путь через --analyzer. Пока можно посмотреть учебный пример."
        )

    # Older v1 analyzers need both names to see dependencies around a rename.
    changed_files = sorted({path for change in changes for path in (change.path, change.old_path) if path})
    request = {
        "protocol_version": PROTOCOL_VERSION,
        "repo_path": repo.as_posix(),
        "baseline_repo_path": baseline.as_posix(),
        "changed_files": changed_files,
        "changes": [change.request() for change in changes],
    }
    try:
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    except UnicodeError as exc:
        raise AnalysisError("Путь к репозиторию или файлу невозможно передать в UTF-8.") from exc
    try:
        result = subprocess.run(
            [str(executable)],
            input=payload,
            capture_output=True,
            cwd=repo,
            timeout=60,
            creationflags=PROCESS_FLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("Анализатор не ответил за 60 секунд.") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        try:
            warnings = parse_response(result.stdout)["warnings"]
        except AnalysisError:
            warnings = []
        details = "\n".join([*warnings, message]).strip()
        summary = f"Анализатор завершился с кодом {result.returncode}."
        raise AnalysisError(f"{summary}\n{details}" if details else summary)
    return parse_response(result.stdout)
