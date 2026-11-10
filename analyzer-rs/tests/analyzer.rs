use impactmap_analyzer::{analyze, Input, Output};
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT: AtomicUsize = AtomicUsize::new(0);

fn compare(before: &Repo, after: &Repo, changes: serde_json::Value) -> Output {
    analyze(
        serde_json::from_value(serde_json::json!({
            "protocol_version":1,"repo_path":after.0,"baseline_repo_path":before.0,
            "changes":changes
        }))
        .unwrap(),
    )
    .unwrap()
}

#[test]
fn three_file_chain_and_stable_ties_with_cycles() {
    let repo = Repo::new(&[
        ("runner.py", "import app\nimport alt\n"),
        ("app.py", "import helpers\nimport runner\n"),
        ("alt.py", "import helpers\n"),
        ("helpers.py", ""),
    ]);
    let out = repo.run(&["helpers.py"]);
    let runner = out
        .affected_files
        .iter()
        .find(|a| a.path == "runner.py")
        .unwrap();
    assert_eq!(runner.chain, ["runner.py", "alt.py", "helpers.py"]);
    assert_eq!(runner.chain.len(), runner.distance + 1);
    let out = repo.run(&["helpers.py", "app.py"]);
    assert_eq!(
        out.affected_files
            .iter()
            .find(|a| a.path == "runner.py")
            .unwrap()
            .chain,
        ["runner.py", "app.py"]
    );
    let simple = Repo::new(&[
        ("runner.py", "import app\n"),
        ("app.py", "import helpers\n"),
        ("helpers.py", ""),
    ]);
    assert_eq!(
        simple
            .run(&["helpers.py"])
            .affected_files
            .iter()
            .find(|a| a.path == "runner.py")
            .unwrap()
            .chain,
        ["runner.py", "app.py", "helpers.py"]
    );
}

#[test]
fn deleted_dependency_retains_historical_chain() {
    let before = Repo::new(&[
        ("runner.py", "import app\n"),
        ("app.py", "import helpers\n"),
        ("helpers.py", ""),
    ]);
    let after = Repo::new(&[
        ("runner.py", "import app\n"),
        ("app.py", "import helpers\n"),
    ]);
    let out = compare(
        &before,
        &after,
        serde_json::json!([{"path":"helpers.py","status":"deleted"}]),
    );
    let finding = out
        .impact
        .findings
        .iter()
        .find(|f| f.code == "deleted_dependency")
        .unwrap();
    assert_eq!(finding.confidence, "confirmed");
    assert!(finding.evidence.contains("import helpers"));
    assert_eq!(finding.paths, ["app.py", "helpers.py"]);
    assert_eq!(
        out.affected_files
            .iter()
            .find(|a| a.path == "runner.py")
            .unwrap()
            .chain,
        ["runner.py", "app.py", "helpers.py"]
    );
}

#[test]
fn renamed_dependency_requires_stale_import() {
    let before = Repo::new(&[("app.py", "import helpers\n"), ("helpers.py", "")]);
    let after = Repo::new(&[("app.py", "import helpers\n"), ("utils.py", "")]);
    let changes =
        serde_json::json!([{"path":"utils.py","old_path":"helpers.py","status":"renamed"}]);
    let out = compare(&before, &after, changes.clone());
    assert_eq!(out.impact.findings[0].code, "renamed_dependency");
    assert_eq!(
        out.impact.findings[0].paths,
        ["app.py", "helpers.py", "utils.py"]
    );
    let fixed = Repo::new(&[("app.py", "import utils\n"), ("utils.py", "")]);
    assert!(compare(&before, &fixed, changes).impact.findings.is_empty());
}

#[test]
fn deleted_rust_module_and_renamed_use() {
    let before = Repo::new(&[
        ("src/lib.rs", "mod helpers; mod app;"),
        ("src/helpers.rs", "pub fn run() {}"),
        ("src/app.rs", "use crate::helpers::run;"),
    ]);
    let after = Repo::new(&[
        ("src/lib.rs", "mod utils; mod app;"),
        ("src/utils.rs", "pub fn run() {}"),
        ("src/app.rs", "use crate::helpers::run;"),
    ]);
    let out = compare(
        &before,
        &after,
        serde_json::json!([{"path":"src/utils.rs","old_path":"src/helpers.rs","status":"renamed"}]),
    );
    assert!(out
        .impact
        .findings
        .iter()
        .any(|f| f.code == "renamed_dependency" && f.paths[0] == "src/app.rs"));
    assert!(!out
        .impact
        .findings
        .iter()
        .any(|f| f.paths[0] == "src/lib.rs"));
    let deleted = Repo::new(&[
        ("src/lib.rs", "mod helpers; mod app;"),
        ("src/app.rs", "use crate::helpers::run;"),
    ]);
    assert!(compare(
        &before,
        &deleted,
        serde_json::json!([{"path":"src/helpers.rs","status":"deleted"}])
    )
    .impact
    .findings
    .iter()
    .any(|f| f.code == "deleted_dependency"));
}

#[test]
fn public_python_symbol_change_is_possible_not_runtime_failure() {
    let before = Repo::new(&[
        ("app.py", "from helpers import run\n"),
        ("helpers.py", "def run(x):\n    return x\n"),
    ]);
    let after = Repo::new(&[
        ("app.py", "from helpers import run\n"),
        ("helpers.py", "def run(x, y):\n    return y\n"),
    ]);
    let out = compare(
        &before,
        &after,
        serde_json::json!([{"path":"helpers.py","status":"modified"}]),
    );
    let finding = out
        .impact
        .findings
        .iter()
        .find(|f| f.code == "public_symbol_changed")
        .unwrap();
    assert_eq!(finding.confidence, "possible");
    assert!(finding.evidence.contains("run(x, y)"));
    let body_only = Repo::new(&[
        ("app.py", "from helpers import run\n"),
        ("helpers.py", "def run(x):\n    return x + 1\n"),
    ]);
    assert!(compare(
        &before,
        &body_only,
        serde_json::json!([{"path":"helpers.py","status":"modified"}])
    )
    .impact
    .findings
    .is_empty());
}

#[test]
fn rust_public_symbol_removed_or_signature_changed() {
    let before = Repo::new(&[
        ("src/lib.rs", "mod helpers; mod app;"),
        ("src/helpers.rs", "pub fn run(x: u32) {}"),
        ("src/app.rs", "use crate::helpers::run;"),
    ]);
    for source in [
        "pub fn run(x: String) {}",
        "fn run(x: u32) {}",
        "pub fn other() {}",
    ] {
        let after = Repo::new(&[
            ("src/lib.rs", "mod helpers; mod app;"),
            ("src/helpers.rs", source),
            ("src/app.rs", "use crate::helpers::run;"),
        ]);
        let out = compare(
            &before,
            &after,
            serde_json::json!([{"path":"src/helpers.rs","status":"modified"}]),
        );
        assert!(out
            .impact
            .findings
            .iter()
            .any(|f| f.code == "public_symbol_changed" && f.evidence.contains("run")));
    }
}

#[test]
fn external_imports_and_unrelated_changes_are_not_findings() {
    let before = Repo::new(&[("app.py", "import serde\nimport os\n"), ("other.py", "")]);
    let after = Repo::new(&[
        ("app.py", "import serde\nimport os\n"),
        ("other.py", "x=1\n"),
    ]);
    assert!(compare(
        &before,
        &after,
        serde_json::json!([{"path":"other.py","status":"modified"}])
    )
    .impact
    .findings
    .is_empty());
    let legacy = after.run(&["other.py"]);
    assert!(legacy.impact.findings.is_empty());
    assert!(legacy
        .impact
        .limitations
        .iter()
        .any(|l| l.contains("No baseline")));
}

#[test]
fn incomplete_snapshot_reports_possible_and_invalid_changes_fail() {
    let before = Repo::new(&[
        ("app.py", "import helpers\n"),
        ("broken.py", "def ?"),
        ("helpers.py", ""),
    ]);
    let after = Repo::new(&[("app.py", "import helpers\n")]);
    let out = compare(
        &before,
        &after,
        serde_json::json!([{"path":"helpers.py","status":"deleted"}]),
    );
    assert_eq!(out.impact.findings[0].confidence, "possible");
    for changes in [
        serde_json::json!([{"path":"x","status":"renamed"}]),
        serde_json::json!([{"path":"../x","status":"added"}]),
    ] {
        let input = serde_json::from_value(
            serde_json::json!({"protocol_version":1,"repo_path":after.0,"changes":changes}),
        )
        .unwrap();
        assert!(analyze(input).is_err());
    }
}

struct Repo(PathBuf);
impl Repo {
    fn new(files: &[(&str, &str)]) -> Self {
        let path = std::env::temp_dir().join(format!(
            "impactmap-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        for (name, text) in files {
            let file = path.join(name);
            fs::create_dir_all(file.parent().unwrap()).unwrap();
            fs::write(file, text).unwrap();
        }
        Self(path)
    }
    fn input(&self, changed: &[&str]) -> Input {
        Input {
            protocol_version: 1,
            baseline_repo_path: None,
            changes: vec![],
            repo_path: self.0.to_str().unwrap().into(),
            changed_files: changed.iter().map(|p| (*p).into()).collect(),
        }
    }
    fn run(&self, changed: &[&str]) -> Output {
        analyze(self.input(changed)).unwrap()
    }
}
impl Drop for Repo {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn edge(output: &Output, from: &str, to: &str) -> bool {
    output
        .edges
        .iter()
        .any(|e| e.from == from && e.to == to && e.relation == "imports")
}
fn distance(output: &Output, path: &str) -> Option<usize> {
    output
        .affected_files
        .iter()
        .find(|a| a.path == path)
        .map(|a| a.distance)
}

#[test]
fn rust_direction_and_transitive_distance() {
    let repo = Repo::new(&[
        ("src/lib.rs", "mod a; mod b; mod c;"),
        ("src/a.rs", "use super::b::Thing;"),
        ("src/b.rs", "use crate::c::Thing;"),
        ("src/c.rs", "pub struct Thing;"),
    ]);
    let out = repo.run(&["src/c.rs"]);
    assert!(edge(&out, "src/b.rs", "src/c.rs"));
    assert!(edge(&out, "src/a.rs", "src/b.rs"));
    assert!(!edge(&out, "src/c.rs", "src/b.rs"));
    assert_eq!(distance(&out, "src/b.rs"), Some(1));
    assert_eq!(distance(&out, "src/a.rs"), Some(2));
    assert_eq!(distance(&out, "src/lib.rs"), Some(1));
    assert_eq!(distance(&out, "src/c.rs"), None);
    assert!(out.warnings.is_empty(), "{:?}", out.warnings);
}

#[test]
fn rust_inline_groups_aliases_and_glob() {
    let repo = Repo::new(&[
        ("src/lib.rs", "mod a; mod b; mod caller;"),
        ("src/a.rs", "pub struct Thing;"),
        ("src/b.rs", "pub struct Thing;"),
        (
            "src/caller.rs",
            "mod inner { use crate::{a::Thing as A, b::{self, *}}; }",
        ),
    ]);
    let out = repo.run(&["src/a.rs"]);
    assert!(edge(&out, "src/caller.rs", "src/a.rs"));
    assert!(edge(&out, "src/caller.rs", "src/b.rs"));
    assert!(out.warnings.iter().any(|w| w.contains("wildcard")));
}

#[test]
fn rust_nested_modules_and_repeated_super() {
    let repo = Repo::new(&[
        ("src/lib.rs", "mod outer; mod data;"),
        ("src/data.rs", "pub struct Value;"),
        ("src/outer.rs", "mod leaf;"),
        ("src/outer/leaf.rs", "use super::super::data::Value;"),
    ]);
    let out = repo.run(&["src/data.rs"]);
    assert!(edge(&out, "src/outer.rs", "src/outer/leaf.rs"));
    assert!(edge(&out, "src/outer/leaf.rs", "src/data.rs"));
    assert_eq!(distance(&out, "src/outer.rs"), Some(2));
}

#[test]
fn rust_mod_rs_and_self() {
    let repo = Repo::new(&[
        ("src/lib.rs", "mod outer;"),
        ("src/outer/mod.rs", "mod leaf; use self::leaf::Value;"),
        ("src/outer/leaf.rs", "pub struct Value;"),
    ]);
    let out = repo.run(&["src/outer/leaf.rs"]);
    assert!(edge(&out, "src/outer/mod.rs", "src/outer/leaf.rs"));
    assert_eq!(distance(&out, "src/lib.rs"), Some(2));
    assert!(out.warnings.is_empty(), "{:?}", out.warnings);
}

#[test]
fn rust_path_attribute_is_relative_to_containing_file() {
    let repo = Repo::new(&[
        ("src/lib.rs", "mod outer;"),
        ("src/outer.rs", "#[path = \"shared.rs\"] mod inner;"),
        ("src/shared.rs", "pub struct Value;"),
    ]);
    let out = repo.run(&["src/shared.rs"]);
    assert!(edge(&out, "src/outer.rs", "src/shared.rs"));
    assert_eq!(distance(&out, "src/lib.rs"), Some(2));
}

#[test]
fn rust_custom_cargo_targets_and_local_library_name() {
    let repo = Repo::new(&[
        ("Cargo.toml", "[package]\nname='demo-lib'\nversion='0.1.0'\n[lib]\npath='core/root.rs'\n[[bin]]\nname='app'\npath='cli/start.rs'\n"),
        ("core/root.rs", "pub mod model;"), ("core/model.rs", "pub struct Value;"),
        ("cli/start.rs", "use demo_lib::model::Value; fn main() {}"),
    ]);
    let out = repo.run(&["core/model.rs"]);
    assert!(edge(&out, "cli/start.rs", "core/model.rs"));
    assert!(edge(&out, "core/root.rs", "core/model.rs"));
}

#[test]
fn rust_ambiguity_and_external_imports_warn() {
    let repo = Repo::new(&[
        (
            "src/lib.rs",
            "mod data; use std::collections::HashMap; use crate::missing::Thing;",
        ),
        ("src/data.rs", ""),
        ("src/data/mod.rs", ""),
    ]);
    let out = repo.run(&[]);
    assert!(!edge(&out, "src/lib.rs", "src/data.rs"));
    assert!(out
        .warnings
        .iter()
        .any(|w| w.contains("2 local source files")));
    assert!(out.warnings.iter().any(|w| w.contains("std::collections")));
    assert!(out.warnings.iter().any(|w| w.contains("crate::missing")));
}

#[test]
fn rust_macros_cfg_and_reexports_warn() {
    let repo = Repo::new(&[
        (
            "src/lib.rs",
            "#[cfg(feature=\"x\")] mod data; pub use data::Value; include!(\"generated.rs\");",
        ),
        ("src/data.rs", "pub struct Value;"),
    ]);
    let out = repo.run(&[]);
    for word in ["Conditional", "re-export", "macro"] {
        assert!(out.warnings.iter().any(|w| w.contains(word)));
    }
}

#[test]
fn comments_and_strings_are_not_imports() {
    let repo = Repo::new(&[
        (
            "src/lib.rs",
            "// mod ghost;\nconst S: &str = \"use crate::ghost::X;\";",
        ),
        (
            "app.py",
            "# import ghost\ns = '''from ghost import thing'''\n",
        ),
    ]);
    let out = repo.run(&[]);
    assert!(out.edges.is_empty());
    assert!(out.warnings.is_empty(), "{:?}", out.warnings);
}

#[test]
fn python_import_aliases_multiline_and_nested_imports() {
    let repo = Repo::new(&[
        ("app.py", "import pkg.model as m, util\ndef run():\n    from pkg import (\n        model as x,\n    )\n"),
        ("pkg/__init__.py", ""), ("pkg/model.py", ""), ("util.py", ""),
    ]);
    let out = repo.run(&["pkg/model.py"]);
    for target in ["pkg/model.py", "pkg/__init__.py", "util.py"] {
        assert!(edge(&out, "app.py", target));
    }
    assert_eq!(distance(&out, "app.py"), Some(1));
    assert!(out.warnings.iter().any(|w| w.contains("may shadow")));
}

#[test]
fn rust_unknown_symbol_does_not_fall_back_to_crate_root() {
    let repo = Repo::new(&[
        ("src/lib.rs", "mod caller; pub struct Value;"),
        ("src/caller.rs", "use crate::missing;"),
    ]);
    let out = repo.run(&["src/lib.rs"]);
    assert!(!edge(&out, "src/caller.rs", "src/lib.rs"));
    assert!(out.warnings.iter().any(|w| w.contains("crate::missing")));
    assert!(out.affected_files.is_empty());
}

#[test]
fn rust_crate_root_symbols_are_resolved() {
    let repo = Repo::new(&[
        ("src/lib.rs", "mod caller; pub struct Value;"),
        ("src/caller.rs", "use crate::Value;"),
    ]);
    let out = repo.run(&["src/lib.rs"]);
    assert!(edge(&out, "src/caller.rs", "src/lib.rs"));
    assert_eq!(distance(&out, "src/caller.rs"), Some(1));
}

#[test]
fn python_relative_packages_and_parent_imports() {
    let repo = Repo::new(&[
        ("pkg/__init__.py", ""),
        ("pkg/data.py", ""),
        ("pkg/sub/__init__.py", ""),
        (
            "pkg/sub/worker.py",
            "from .. import data\nfrom . import sibling\n",
        ),
        ("pkg/sub/sibling.py", ""),
    ]);
    let out = repo.run(&["pkg/data.py"]);
    assert!(edge(&out, "pkg/sub/worker.py", "pkg/data.py"));
    assert!(edge(&out, "pkg/sub/worker.py", "pkg/sub/sibling.py"));
    assert!(edge(&out, "pkg/sub/worker.py", "pkg/__init__.py"));
    assert_eq!(distance(&out, "pkg/sub/worker.py"), Some(1));
}

#[test]
fn python_symbols_and_wildcards_record_base_and_warn() {
    let repo = Repo::new(&[
        ("app.py", "from data import Value\nfrom pkg import *\n"),
        ("data.py", "Value = 1\n"),
        ("pkg/__init__.py", ""),
    ]);
    let out = repo.run(&[]);
    assert!(edge(&out, "app.py", "data.py"));
    assert!(edge(&out, "app.py", "pkg/__init__.py"));
    assert!(out.warnings.iter().any(|w| w.contains("re-export")));
    assert!(out.warnings.iter().any(|w| w.contains("wildcard")));
}

#[test]
fn python_src_layout_and_namespace_packages() {
    let repo = Repo::new(&[
        ("src/pkg/worker.py", "from . import data\n"),
        ("src/pkg/data.py", ""),
        ("app.py", "import pkg.worker\n"),
    ]);
    let out = repo.run(&["src/pkg/data.py"]);
    assert!(edge(&out, "src/pkg/worker.py", "src/pkg/data.py"));
    assert_eq!(distance(&out, "app.py"), Some(2));
    assert!(out.warnings.iter().any(|w| w.contains("namespace")));
}

#[test]
fn python_source_root_and_module_collisions_are_not_guessed() {
    let repo = Repo::new(&[
        ("app.py", "import data\nimport pkg\n"),
        ("data.py", ""),
        ("src/data.py", ""),
        ("pkg.py", ""),
        ("pkg/__init__.py", ""),
    ]);
    let out = repo.run(&[]);
    assert!(out.edges.is_empty());
    assert_eq!(
        out.warnings
            .iter()
            .filter(|w| w.contains("Ambiguous Python"))
            .count(),
        2
    );
}

#[test]
fn python_unresolved_and_dynamic_imports_warn() {
    let repo = Repo::new(&[("app.py", "import absent\nimportlib.import_module('data')\n__import__('other')\nfrom .. import outside\n")]);
    let out = repo.run(&[]);
    for word in ["absent", "Dynamic", "beyond"] {
        assert!(out.warnings.iter().any(|w| w.contains(word)));
    }
    assert!(out.edges.is_empty());
}

#[test]
fn cycles_and_multiple_changes_use_minimum_distance() {
    let repo = Repo::new(&[
        ("a.py", "import b\n"),
        ("b.py", "import c\n"),
        ("c.py", "import a\n"),
        ("d.py", "import a, c\n"),
        ("isolated.py", ""),
    ]);
    let out = repo.run(&["a.py", "c.py"]);
    assert_eq!(distance(&out, "a.py"), None);
    assert_eq!(distance(&out, "c.py"), None);
    assert_eq!(distance(&out, "b.py"), Some(1));
    assert_eq!(distance(&out, "d.py"), Some(1));
    assert_eq!(distance(&out, "isolated.py"), None);
}

#[test]
fn normalizes_and_deduplicates_paths_and_results() {
    let repo = Repo::new(&[("pkg/data.py", ""), ("app.py", "import pkg.data\n")]);
    let out = repo.run(&["./pkg\\data.py", "pkg/x/../data.py"]);
    assert_eq!(out.changed_files, ["pkg/data.py"]);
    assert!(out
        .edges
        .iter()
        .all(|e| !e.from.contains('\\') && !e.to.contains('\\')));
    let again = repo.run(&["pkg/data.py"]);
    assert_eq!(
        serde_json::to_value(out).unwrap(),
        serde_json::to_value(again).unwrap()
    );
}

#[test]
fn rejects_escaping_and_absolute_changed_paths() {
    let repo = Repo::new(&[]);
    for path in [
        "../secret.py",
        "a/../../secret.py",
        "/etc/a.py",
        "C:\\data.py",
        "\\\\server\\file.py",
        "",
    ] {
        assert!(analyze(repo.input(&[path])).is_err(), "{path}");
    }
}

#[test]
fn rust_path_attributes_cannot_escape_repository_or_be_absolute() {
    let repo = Repo::new(&[
        (
            "src/lib.rs",
            "#[path=\"../../outside.rs\"] mod bad; #[path=\"/absolute.rs\"] mod absolute;",
        ),
        ("src/absolute.rs", ""),
    ]);
    let out = repo.run(&[]);
    assert!(out.edges.is_empty());
    assert!(out.warnings.iter().any(|w| w.contains("escapes")));
    assert!(out.warnings.iter().any(|w| w.contains("Absolute source")));
}

#[test]
fn broken_sources_missing_files_and_excluded_dirs_warn() {
    let repo = Repo::new(&[
        ("src/lib.rs", "mod broken {"),
        ("app.py", "from ??? import ???\n"),
        ("target/generated.py", "import app\n"),
        (".venv/site.py", "import app\n"),
    ]);
    let out = repo.run(&["deleted.py"]);
    assert!(out.edges.is_empty());
    assert!(out.warnings.iter().any(|w| w.contains("Rust syntax")));
    assert!(out.warnings.iter().any(|w| w.contains("Python syntax")));
    assert!(out.warnings.iter().any(|w| w.contains("deleted.py")));
}

#[test]
fn empty_changes_still_return_complete_graph() {
    let repo = Repo::new(&[("a.py", "import b\n"), ("b.py", "")]);
    let out = repo.run(&[]);
    assert!(out.changed_files.is_empty());
    assert!(out.affected_files.is_empty());
    assert!(edge(&out, "a.py", "b.py"));
}

#[test]
fn unicode_filenames_and_utf8_bom() {
    let repo = Repo::new(&[("app.py", "\u{feff}import данные\n"), ("данные.py", "")]);
    let out = repo.run(&["данные.py"]);
    assert!(edge(&out, "app.py", "данные.py"));
    assert_eq!(distance(&out, "app.py"), Some(1));
}

fn cli(input: &str) -> (bool, serde_json::Value) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_impactmap-analyzer"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    let result = child.wait_with_output().unwrap();
    assert!(result.stderr.is_empty(), "{:?}", result.stderr);
    (
        result.status.success(),
        serde_json::from_slice(&result.stdout).unwrap(),
    )
}

#[test]
fn cli_outputs_exact_protocol_with_no_extra_stdout() {
    let repo = Repo::new(&[("a.py", "import b\n"), ("b.py", "")]);
    let request =
        serde_json::json!({"protocol_version":1,"repo_path":repo.0,"changed_files":["b.py"]});
    let (success, response) = cli(&request.to_string());
    assert!(success);
    let mut keys: Vec<_> = response
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort();
    assert_eq!(
        keys,
        [
            "affected_files",
            "changed_files",
            "edges",
            "impact",
            "protocol_version",
            "warnings"
        ]
    );
    assert_eq!(
        response["affected_files"][0],
        serde_json::json!({"path":"a.py","distance":1,"reason":"depends on changed file","chain":["a.py","b.py"]})
    );
    assert_eq!(
        response["edges"][0],
        serde_json::json!({"from":"a.py","to":"b.py","relation":"imports"})
    );
}

#[test]
fn cli_errors_preserve_response_shape_and_fail_exit_status() {
    for input in [
        "{",
        "{}",
        r#"{"protocol_version":2,"repo_path":".","changed_files":[]}"#,
        r#"{"protocol_version":1,"repo_path":"missing-repo-does-not-exist","changed_files":[]}"#,
    ] {
        let (success, response) = cli(input);
        assert!(!success);
        assert_eq!(response["protocol_version"], 1);
        assert_eq!(response.as_object().unwrap().len(), 6);
        assert!(!response["warnings"].as_array().unwrap().is_empty());
    }
}

#[cfg(unix)]
#[test]
fn symlinks_are_not_followed() {
    use std::os::unix::fs::symlink;
    let repo = Repo::new(&[("a.py", "import b\n")]);
    let outside = Repo::new(&[("b.py", "import a\n")]);
    symlink(outside.0.join("b.py"), repo.0.join("b.py")).unwrap();
    symlink(&repo.0, repo.0.join("loop")).unwrap();
    let out = repo.run(&[]);
    assert!(out.edges.is_empty());
    assert!(out.warnings.iter().any(|w| w.contains("Symlink")));
}
