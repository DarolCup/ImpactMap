from collections import defaultdict, deque

from analysis import AnalysisResult


STATUS = {"added": "Добавлен", "modified": "Изменён", "deleted": "Удалён", "renamed": "Переименован"}


def display_path(path: str) -> str:
    return path.replace("\n", r"\n").replace("\r", r"\r").replace("\t", r"\t")


def chain_text(chain) -> str:
    return " → ".join(map(display_path, chain))


def finding_text(item: dict) -> tuple[str, str, str]:
    paths = list(map(display_path, item["paths"]))
    code = item["code"]
    if code == "deleted_dependency" and len(paths) >= 2:
        file, old = paths[:2]
        return f"Удалён {old}", f"В {file} остался импорт, который вёл к этому файлу.", f"Проверьте импорт в {file}."
    if code == "renamed_dependency" and len(paths) >= 3:
        file, old, new = paths[:3]
        return (f"{old} переименован в {new}", f"В {file} остался прежний импорт.",
                f"Проверьте путь импорта в {file}.")
    if code == "public_symbol_changed" and len(paths) >= 2:
        file, module = paths[:2]
        return (f"Изменилось объявление в {module}", f"Файл {file} импортирует это объявление.",
                f"Проверьте, как {file} использует импортированную функцию, класс или другое объявление.")
    return "Проверьте находку анализатора", ", ".join(paths), "Откройте пояснение анализатора."


def limitation_text(message: str) -> str:
    if message.startswith("Findings describe static evidence"):
        return "Проверка читает исходный код. Возможные сбои можно подтвердить запуском тестов."
    if message.startswith("Public symbol checks cover"):
        return "Проверены только некоторые объявления. Динамические импорты, макросы и реэкспорт могут остаться вне проверки."
    if message.startswith("Edges/chains may include baseline"):
        return "На карте могут быть прежние связи с удалёнными файлами. Проверьте импорты, которые вели к ним."
    return "Часть кода или связей могла остаться вне проверки. Точный текст доступен в подробностях."


class ResultModel:
    def __init__(self, result: AnalysisResult):
        self.result = result
        response = result.response
        self.changes = {item.path: item for item in result.changes}
        self.previous_names = {item.old_path: item.path for item in result.changes if item.old_path}
        self.changed = set(self.changes) | self.previous_names.keys() | set(response["changed_files"])
        self.affected = {item["path"]: item for item in response["affected_files"]}
        impact = response.get("impact", {})
        self.findings = sorted(impact.get("findings", []), key=lambda item: item["severity"] != "high")
        self.edges = {(edge["from"], edge["to"]) for edge in response["edges"]}
        self.paths = self.changed | self.affected.keys() | {path for pair in self.edges for path in pair}
        self.incoming = defaultdict(set)
        self.outgoing = defaultdict(set)
        for source, target in self.edges:
            self.incoming[target].add(source)
            self.outgoing[source].add(target)
        self.chains = self.build_chains()
        self.focus_edges = {(source, target) for chain in self.chains.values() for source, target in zip(chain, chain[1:])}
        self.focus_paths = self.changed | self.affected.keys() | {path for pair in self.focus_edges for path in pair}
        self.paths.update(self.focus_paths)
        self.distances = {**{path: item["distance"] for path, item in self.affected.items()},
                          **dict.fromkeys(self.changed, 0)}
        self.warnings = self.group_warnings(response["warnings"])
        self.limitations = impact.get("limitations", [])

    def build_chains(self) -> dict[str, tuple[str, ...]]:
        # A reverse traversal supplies shortest routes for older analyzer replies.
        distance = dict.fromkeys(self.changed, 0)
        queue = deque(sorted(self.changed))
        while queue:
            target = queue.popleft()
            for source in self.incoming[target]:
                if source not in distance:
                    distance[source] = distance[target] + 1
                    queue.append(source)
        chains = {}
        for path, item in self.affected.items():
            supplied = tuple(item.get("chain", ()))
            if supplied and supplied[0] == path:
                chains[path] = supplied
                continue
            route = [path]
            while distance.get(route[-1], 0):
                end = route[-1]
                route.append(min(target for target in self.outgoing[end] if distance.get(target) == distance[end] - 1))
            if len(route) > 1:
                chains[path] = tuple(route)
        return chains

    def group_warnings(self, warnings: list[str]) -> dict[str, list[str]]:
        groups = defaultdict(list)
        for warning in warnings:
            diagnostic = warning.removeprefix("Baseline: ")
            offset = diagnostic.find(": ")
            while offset >= 0 and diagnostic[:offset] not in self.paths:
                offset = diagnostic.find(": ", offset + 2)
            # Skipped files may be missing from both the graph and the changes.
            fallback = diagnostic.partition(": ")[0]
            path = diagnostic[:offset] if offset >= 0 else ""
            if not path and fallback.endswith((".py", ".rs", "Cargo.toml")) and ": " in diagnostic:
                path = fallback
            groups[path or "Общие предупреждения"].append(warning)
        return dict(groups)

    def selection(self, path: str, full: bool = False) -> tuple[set[str], set[tuple[str, str]]]:
        if path in self.chains:
            chain = self.chains[path]
            return set(chain), set(zip(chain, chain[1:]))
        if path in self.changed:
            aliases = {path}
            if path in self.previous_names:
                aliases.add(self.previous_names[path])
            if path in self.changes and self.changes[path].old_path:
                aliases.add(self.changes[path].old_path)
            routes = [chain for chain in self.chains.values() if chain[-1] in aliases]
            return aliases | {node for chain in routes for node in chain}, {
                (source, target) for chain in routes for source, target in zip(chain, chain[1:])
            }
        edges = {(path, target) for target in self.outgoing[path]} | {(source, path) for source in self.incoming[path]}
        return ({path} | {node for pair in edges for node in pair}, edges) if full else ({path}, set())

    def role(self, path: str) -> str:
        if path in self.changes:
            return STATUS[self.changes[path].status]
        if path in self.previous_names:
            return "Прежнее имя"
        if path in self.changed:
            return "Изменён"
        if path in self.affected:
            return "Затронут"
        return "Другой файл"
