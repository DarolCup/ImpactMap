import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from analysis import AnalysisError, AnalysisResult, Change, git, isolated_git_env, parse_response
from github_repo import analyze_github
from local_repo import analyze_example, analyze_local
from main import MainWindow
from result import ResultModel, finding_text, preview_impact
from report import render_report


REAL = os.environ.get("IMPACTMAP_TEST_ANALYZER")


def response(changed=(), affected=(), edges=(), warnings=(), **extra):
    return {
        "protocol_version": 1, "changed_files": list(changed), "affected_files": list(affected),
        "edges": [{"from": source, "to": target, "relation": "imports"} for source, target in edges],
        "warnings": list(warnings), **extra,
    }


def affected(path, distance, **extra):
    return {"path": path, "distance": distance, "reason": "depends on changed file", **extra}


class AnalysisTests(unittest.TestCase):
    def test_preview_cycles_ties_isolated_files_and_historical_edges(self):
        edges = [("runner.py", "app.py"), ("runner.py", "alt.py"), ("app.py", "helpers.py"),
                 ("alt.py", "helpers.py"), ("helpers.py", "runner.py")]
        data = response(["deleted.py"], edges=[*edges, ("isolated.py", "deleted.py")],
                        files=["alt.py", "app.py", "helpers.py", "isolated.py", "runner.py"],
                        current_edges=response(edges=edges)["edges"],
                        impact={"findings": [], "limitations": []})
        original = AnalysisResult("Test", "clean HEAD", [Change("deleted.py", "deleted")], data)
        saved = json.dumps(data, sort_keys=True)
        planned = preview_impact(original, "helpers.py")
        model = ResultModel(planned)
        self.assertEqual(model.chains["runner.py"], ("runner.py", "alt.py", "helpers.py"))
        self.assertEqual(set(model.affected), {"alt.py", "app.py", "runner.py"})
        self.assertEqual(model.role("helpers.py"), "Планируется")
        self.assertNotIn(("isolated.py", "deleted.py"), model.edges)
        self.assertFalse(preview_impact(original, "isolated.py").response["affected_files"])
        self.assertEqual(json.dumps(data, sort_keys=True), saved)
        with self.assertRaises(AnalysisError):
            preview_impact(original, "deleted.py")

    def test_report_escapes_repository_content_and_response_rejects_bad_planning_data(self):
        html = render_report(AnalysisResult("<script>alert(1)</script>", "HEAD", [], response(warnings=["<img src=x>"])))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img src=x&gt;", html)
        for extra in ({"files": [None]}, {"current_edges": [{"from": "a.py"}]}):
            with self.assertRaises(AnalysisError):
                parse_response(json.dumps(response(**extra)).encode())

    @unittest.skipUnless(REAL, "set IMPACTMAP_TEST_ANALYZER")
    def test_preview_clean_repository_matches_rust_without_touching_files(self):
        from analysis import analyze
        with TemporaryDirectory() as temp:
            root = Path(temp)
            env = isolated_git_env()
            git(root, "init", "--template=", env=env)
            for name, content in {"runner.py": "import app\n", "app.py": "import helpers\n", "helpers.py": "", "alone.py": ""}.items():
                (root / name).write_text(content, encoding="utf-8")
            git(root, "add", "--all", env=env)
            git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "First", env=env)
            original = analyze_local(root, REAL)
            self.assertFalse(original.changes)
            planned = preview_impact(original, "helpers.py")
            rust = analyze(root, root, [Change("helpers.py", "modified")], REAL)
            self.assertEqual([(a["path"], a["distance"], a["chain"]) for a in planned.response["affected_files"]],
                             [(a["path"], a["distance"], a["chain"]) for a in rust["affected_files"]])
            self.assertEqual(git(root, "status", "--porcelain", env=env), b"")
            self.assertIn("alone.py", ResultModel(original).paths)

    def test_old_response_recovers_chain_and_groups_full_warning(self):
        parsed = parse_response(json.dumps(response(
            ["helpers.py"], [affected("app.py", 1), affected("runner.py", 2)],
            [("app.py", "helpers.py"), ("runner.py", "app.py")],
            ["app.py: one", "Baseline: app.py: two", "skipped.rs: three"],
        )).encode())
        model = ResultModel(AnalysisResult("Test", "HEAD", [Change("helpers.py", "modified")], parsed))
        self.assertEqual(model.chains["runner.py"], ("runner.py", "app.py", "helpers.py"))
        self.assertEqual(model.warnings["app.py"], ["app.py: one", "Baseline: app.py: two"])
        self.assertEqual(model.warnings["skipped.rs"], ["skipped.rs: three"])

    def test_new_response_prefers_supplied_chain(self):
        finding = dict(code="renamed_dependency", severity="high", confidence="confirmed",
                       paths=["app.py", "helpers.py", "shared.py"], evidence="import helpers")
        parsed = parse_response(json.dumps(response(
            ["helpers.py", "shared.py"], [affected("app.py", 1, chain=["app.py", "helpers.py"])],
            [("app.py", "shared.py"), ("app.py", "helpers.py")],
            impact={"findings": [finding], "limitations": ["Historical links"]},
        )).encode())
        model = ResultModel(AnalysisResult("Test", "HEAD", [Change("shared.py", "renamed", "helpers.py")], parsed))
        self.assertEqual(model.chains["app.py"], ("app.py", "helpers.py"))
        self.assertIn("app.py", model.selection("shared.py")[0])
        self.assertIn("helpers.py переименован в shared.py", finding_text(model.findings[0])[0])
        self.assertEqual(model.limitations, ["Historical links"])

    def test_local_snapshot_includes_unstaged_rename_and_keeps_user_index(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            env = isolated_git_env()
            git(root, "init", "--template=", env=env)
            for name in ("old", "removed", "modified", "unchanged"):
                (root / f"{name}.py").write_text(f"print('{name}')\n", encoding="utf-8")
            (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            git(root, "add", "--all", env=env)
            git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "First", env=env)
            (root / "old.py").rename(root / "new.py")
            (root / "removed.py").unlink()
            (root / "added.py").write_text("print('added')\n", encoding="utf-8")
            (root / "ignored.py").write_text("print('ignored')\n", encoding="utf-8")
            file = root / "modified.py"
            file.write_text("print('staged')\n", encoding="utf-8")
            git(root, "add", "modified.py", env=env)
            file.write_text("print('unstaged')\n", encoding="utf-8")
            before = (root / ".git" / "index").read_bytes()

            def analyzer(current, baseline, changes, setting):
                self.assertNotEqual(current, root)
                self.assertEqual((baseline / "modified.py").read_text(), "print('modified')\n")
                self.assertEqual((current / "modified.py").read_text(), "print('unstaged')\n")
                self.assertFalse((current / "ignored.py").exists())
                return response()

            with patch("local_repo.analyze", analyzer):
                result = analyze_local(root, None)
            self.assertEqual(result.changes, [Change("added.py", "added"), Change("modified.py", "modified"),
                                             Change("new.py", "renamed", "old.py"), Change("removed.py", "deleted")])
            self.assertEqual((root / ".git" / "index").read_bytes(), before)
            self.assertEqual(file.read_text(), "print('unstaged')\n")

    @unittest.skipUnless(REAL, "set IMPACTMAP_TEST_ANALYZER")
    def test_real_example(self):
        model = ResultModel(analyze_example(REAL))
        self.assertEqual(model.changes["helpers.py"].status, "modified")
        self.assertEqual(model.chains["runner.py"], ("runner.py", "app.py", "helpers.py"))
        self.assertEqual(model.chains["app.py"], ("app.py", "helpers.py"))
        self.assertFalse(model.result.notes)
        if "impact" in model.result.response:
            self.assertEqual(model.findings[0]["code"], "public_symbol_changed")

    @unittest.skipUnless(REAL, "set IMPACTMAP_TEST_ANALYZER")
    def test_real_deleted_and_renamed_imports(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            env = isolated_git_env()
            git(root, "init", "--template=", env=env)
            (root / "app.py").write_text("import helpers\nimport utils\n", encoding="utf-8")
            (root / "helpers.py").write_text("def greet():\n    pass\n", encoding="utf-8")
            (root / "utils.py").write_text("def launch():\n    pass\n", encoding="utf-8")
            git(root, "add", "--all", env=env)
            git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "First", env=env)
            (root / "helpers.py").unlink()
            (root / "utils.py").rename(root / "shared.py")
            model = ResultModel(analyze_local(root, REAL))
            self.assertEqual(model.changes["helpers.py"], Change("helpers.py", "deleted"))
            self.assertEqual(model.changes["shared.py"], Change("shared.py", "renamed", "utils.py"))
            if "impact" in model.result.response:
                self.assertEqual({item["code"] for item in model.findings},
                                 {"deleted_dependency", "renamed_dependency"})
                self.assertTrue(all(item["confidence"] == "confirmed" for item in model.findings))
            else:
                self.assertTrue(model.warnings["app.py"])

    @unittest.skipUnless(REAL and os.environ.get("IMPACTMAP_TEST_GITHUB"), "requires real analyzer and GitHub")
    def test_switch_github_repositories(self):
        with TemporaryDirectory() as temp:
            cache = Path(temp)
            first = analyze_github(cache, "DarolCup/ImpactMap", REAL)
            second = analyze_github(cache, "pallets/itsdangerous", REAL)
            self.assertTrue(first.source.startswith("DarolCup/ImpactMap / "))
            self.assertTrue(second.source.startswith("pallets/itsdangerous / "))
            self.assertNotEqual(first.comparison, second.comparison)
            self.assertEqual(len(list(cache.glob("github-*.git"))), 2)


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow(REAL)
        self.window.resize(620, 565)
        self.window.show()
        QTest.qWait(30)

    def tearDown(self):
        self.window.close()
        QTest.qWait(20)

    def wait_for_worker(self):
        deadline = monotonic() + 15
        while self.window.worker is not None and monotonic() < deadline:
            QTest.qWait(25)
        self.assertIsNone(self.window.worker)

    @unittest.skipUnless(REAL, "set IMPACTMAP_TEST_ANALYZER")
    def test_preview_and_restore_demo_then_export(self):
        self.window.demo.click()
        self.wait_for_worker()
        original = self.window.loaded_result
        self.window.plan_file.setCurrentIndex(self.window.plan_file.findData("helpers.py"))
        self.assertTrue(self.window.model.result.planned)
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertEqual(set(self.window.graph.nodes), {"helpers.py", "app.py", "runner.py"})
        self.assertFalse(self.window.model.findings)
        with TemporaryDirectory() as temp:
            output = Path(temp) / "report.html"
            with patch("main.QFileDialog.getSaveFileName", return_value=(str(output), "HTML (*.html)")):
                self.window.export.click()
            self.assertIn("Прогноз до изменения", output.read_text(encoding="utf-8"))
        self.window.plan_file.setCurrentIndex(0)
        self.assertIs(self.window.model.result, original)
        self.assertFalse(self.window.model.result.planned)

    def test_demo_loading_and_empty_result(self):
        self.window.analyzer = "/nonexistent/impactmap-analyzer"
        self.window.demo.click()
        self.assertFalse(self.window.progress.isHidden())
        self.assertFalse(self.window.check.isEnabled())
        self.wait_for_worker()
        self.assertEqual(self.window.tabs.currentIndex(), 0)
        self.assertIn("сохранённый результат", self.window.overview.toPlainText())
        self.assertIn("Косвенная связь", self.window.overview.toPlainText())
        self.window.show_result(AnalysisResult("Test", "HEAD", [], response(
            impact={"findings": [], "limitations": ["Partial analysis"]},
        )))
        self.assertIn("Связанных файлов не найдено", self.window.overview.toPlainText())
        self.assertIn("Ограничения проверки", self.window.overview.toPlainText())

    def test_network_failure_keeps_only_matching_previous_source(self):
        with patch("main.analyze_github") as analyze:
            analyze.return_value = AnalysisResult("first/project", "First parent", [], response())
            self.window.repository.setText("first/project")
            self.window.check.click()
            self.wait_for_worker()
            analyze.side_effect = AnalysisError("Не удалось обновить с GitHub.\nConnection timed out.")
            self.window.check.click()
            self.wait_for_worker()
            self.assertIn("Показана предыдущая проверка", self.window.overview.toPlainText())
            self.window.repository.setText("second/project")
            self.window.check.click()
            self.wait_for_worker()
            self.assertIn("Connection timed out", self.window.overview.toPlainText())
            self.assertNotIn("предыдущая проверка", self.window.overview.toPlainText())
            self.assertIsNone(self.window.model)

    def test_select_chain_on_26_file_map(self):
        long_path = "folder/" + "very_long_file_name_" * 9 + ".py"
        names = [f"task_{i:02}.py" for i in range(20)] + [long_path]
        branches = [f"module_{i}.py" for i in range(4)]
        changed = "helpers.py"
        edges = [(name, branches[i % 4]) for i, name in enumerate(names)]
        edges += [(branch, changed) for branch in branches]
        extra = [(name, branches[(i + 1) % 4]) for i, name in enumerate(names)]
        items = [affected(name, 2, chain=[name, branches[i % 4], changed]) for i, name in enumerate(names)]
        items += [affected(branch, 1, chain=[branch, changed]) for branch in branches]
        self.window.show_result(AnalysisResult("Test", "HEAD", [Change(changed, "modified")],
                                               response([changed], items, edges + extra)))
        self.window.tabs.setCurrentIndex(1)
        QTest.qWait(40)
        graph = self.window.graph
        self.assertEqual(len(graph.nodes), 26)
        self.assertEqual(len(graph.arrows), 25)
        self.window.full.setChecked(True)
        self.assertEqual(len(graph.arrows), 46)
        self.window.picker.setCurrentIndex(self.window.picker.findData(long_path))
        self.assertGreaterEqual(graph.transform().m11(), 0.72)
        self.assertEqual(self.window.selected_path, long_path)
        self.assertIn(long_path + " → module_0.py → helpers.py", self.window.map_detail.toPlainText())
        self.assertEqual(sum(line.zValue() == 1 for line, _ in graph.arrows.values()), 2)
        box = graph.nodes[long_path][0]
        self.assertEqual(box.toolTip(), long_path)
        pos = graph.mapFromScene(box.sceneBoundingRect().center())
        QTest.mouseClick(graph.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.assertEqual(self.window.selected_path, long_path)
        self.window.only.setChecked(True)
        self.assertEqual(set(graph.nodes), {long_path, "module_0.py", changed})
        self.assertEqual(len(graph.arrows), 2)
        self.assertGreaterEqual(graph.transform().m11(), 0.6)
        self.window.only.setChecked(False)
        self.assertEqual(len(graph.arrows), 46)
        self.window.picker.setCurrentIndex(self.window.picker.findData(changed))
        self.assertEqual(self.window.selected_path, changed)
        self.assertGreater(graph.nodes[changed][0].pen().widthF(), 2)
