import json
import os
from pathlib import Path
import shutil
import subprocess


PROTOCOL_VERSION = 1
EXAMPLE = Path(__file__).with_name("example.json")
PROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AnalysisError(Exception):
    pass


def git(repo: Path, *args: str) -> bytes:
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
            capture_output=True,
            env=env,
            timeout=30,
            creationflags=PROCESS_FLAGS,
        )
    except FileNotFoundError as exc:
        raise AnalysisError("Git не найден в PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("Git не ответил за 30 секунд.") from exc
    if result.returncode:
        message = os.fsdecode(result.stderr).strip()
        raise AnalysisError(f"Ошибка Git: {message or result.returncode}")
    return result.stdout


def repository_changes(folder: Path) -> tuple[Path, list[str]]:
    root_output = git(folder, "rev-parse", "--show-toplevel").removesuffix(b"\n")
    if os.name == "nt":
        root_output = root_output.removesuffix(b"\r")
    root = Path(os.fsdecode(root_output))
    tracked = git(root, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "HEAD", "--")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    # Git writes repository-relative paths with forward slashes, even on Windows.
    paths = sorted({os.fsdecode(path) for path in (tracked + untracked).split(b"\0") if path})
    return root, paths


def find_analyzer(setting: str | None) -> Path | None:
    if not setting:
        return None
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
    return response


def analyze(repo: Path, changes: list[str], setting: str | None) -> tuple[dict, bool]:
    executable = find_analyzer(setting)
    if executable is None:
        return parse_response(EXAMPLE.read_bytes()), True

    request = {"protocol_version": PROTOCOL_VERSION, "repo_path": repo.as_posix(), "changed_files": changes}
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
    except FileNotFoundError:
        if not executable.exists():
            return parse_response(EXAMPLE.read_bytes()), True
        raise
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("Анализатор не ответил за 60 секунд.") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise AnalysisError(f"Анализатор завершился с кодом {result.returncode}. {message}")
    return parse_response(result.stdout), False
