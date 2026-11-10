use crate::{join, parent, Files, Graph};
use rustpython_ast::{self as ast, Visitor};
use rustpython_parser::Parse;
use std::collections::BTreeSet;

#[derive(Clone)]
enum Resolution {
    Missing,
    Ambiguous,
    Found {
        files: Vec<String>,
        package: bool,
        namespace: bool,
    },
}

struct Analyzer<'a> {
    files: &'a Files,
    graph: &'a mut Graph,
    file: &'a str,
    roots: &'a [&'a str],
    evidence: String,
    external: &'a BTreeSet<String>,
}

impl Analyzer<'_> {
    fn under(&self, root: &str, module: &str) -> Resolution {
        if module.is_empty() {
            return Resolution::Found {
                files: vec![],
                package: true,
                namespace: false,
            };
        }
        let mut dependencies = Vec::new();
        let mut namespace = false;
        let parts: Vec<_> = module.split('.').collect();
        for (index, _) in parts.iter().enumerate() {
            let prefix = join(root, &parts[..=index].join("/"));
            let file = format!("{prefix}.py");
            let init = format!("{prefix}/__init__.py");
            let has_file = self.files.contains_key(&file);
            let has_init = self.files.contains_key(&init);
            if has_file && has_init {
                return Resolution::Ambiguous;
            }
            if has_file {
                if index + 1 != parts.len() {
                    return Resolution::Missing;
                }
                dependencies.push(file);
                return Resolution::Found {
                    files: dependencies,
                    package: false,
                    namespace,
                };
            }
            if has_init {
                dependencies.push(init);
            } else if self
                .files
                .keys()
                .any(|p| p.starts_with(&format!("{prefix}/")) && p.ends_with(".py"))
            {
                namespace = true;
            } else {
                return Resolution::Missing;
            }
        }
        Resolution::Found {
            files: dependencies,
            package: true,
            namespace,
        }
    }

    fn absolute(&self, module: &str) -> Resolution {
        let mut found = None;
        for root in self.roots {
            match self.under(root, module) {
                Resolution::Missing => (),
                Resolution::Ambiguous => return Resolution::Ambiguous,
                resolution => {
                    if found.is_some() {
                        return Resolution::Ambiguous;
                    }
                    found = Some(resolution);
                }
            }
        }
        found.unwrap_or(Resolution::Missing)
    }

    fn record(&mut self, module: &str, resolution: &Resolution) {
        match resolution {
            Resolution::Found { files, namespace, .. } => {
                for target in files { self.graph.reference(self.file, target, None, &self.evidence); }
                if *namespace {
                    self.graph.limit(self.file, format!("Python namespace package {module}: local portion inferred; runtime search paths are unknown"));
                }
            }
            Resolution::Missing if self.external.contains(&module.split('.').next().unwrap_or("").to_lowercase().replace('-', "_"))
                && matches!(self.absolute(module.split('.').next().unwrap_or("")), Resolution::Missing) => (),
            Resolution::Missing => self.graph.warn(self.file, format!("Cannot resolve Python import {module} locally (external/missing module or unsupported search path)")),
            Resolution::Ambiguous => self.graph.warn(self.file, format!("Ambiguous Python import {module}; multiple local candidates, no dependency inferred")),
        }
    }

    fn from(&mut self, node: ast::StmtImportFrom) {
        let level = node.level.map_or(0, |n| n.to_usize());
        let module = node.module.as_ref().map_or("", |n| n.as_str());
        self.evidence = format!(
            "from {}{} import {}",
            ".".repeat(level),
            module,
            node.names
                .iter()
                .map(|a| a.name.to_string())
                .collect::<Vec<_>>()
                .join(", ")
        );
        self.graph.import(self.file, &self.evidence);
        let (root, base) = if level > 0 {
            let root = self
                .roots
                .iter()
                .copied()
                .filter(|root| !root.is_empty() && self.file.starts_with(&format!("{root}/")))
                .max_by_key(|root| root.len())
                .unwrap_or("");
            let local = if root.is_empty() {
                self.file
            } else {
                &self.file[root.len() + 1..]
            };
            let directory = parent(local);
            let mut parts: Vec<_> = if directory.is_empty() {
                vec![]
            } else {
                directory.split('/').collect()
            };
            if level > parts.len() {
                self.graph.warn(
                    self.file,
                    "Relative Python import goes beyond the known package root",
                );
                return;
            }
            parts.truncate(parts.len() - (level - 1));
            if !module.is_empty() {
                parts.extend(module.split('.'));
            }
            (Some(root), parts.join("."))
        } else {
            (None, module.into())
        };
        let resolve = |this: &Self, name: &str| match root {
            Some(root) => this.under(root, name),
            None => this.absolute(name),
        };
        let resolution = resolve(self, &base);
        self.record(&format!("{}{module}", ".".repeat(level)), &resolution);
        let Resolution::Found { package, .. } = resolution else {
            return;
        };
        for alias in node.names {
            if alias.name.as_str() == "*" {
                self.graph.warn(self.file, format!("Python wildcard import from {base}: base dependency recorded; __all__ and re-exports are not evaluated"));
                continue;
            }
            if let Resolution::Found { files, .. } = &resolution {
                if let Some(target) = files.last() {
                    self.graph.reference(
                        self.file,
                        target,
                        Some(alias.name.to_string()),
                        &self.evidence,
                    );
                }
            }
            let name = if base.is_empty() {
                alias.name.to_string()
            } else {
                format!("{base}.{}", alias.name)
            };
            if package {
                let child = resolve(self, &name);
                match child {
                    Resolution::Missing => self.graph.limit(self.file, format!("Python symbol {name}: base dependency recorded; dynamic attributes and re-export targets are unknown")),
                    _ => {
                        self.record(&name, &child);
                        self.graph.limit(self.file, format!("Python from-import {name}: local submodule candidate recorded; package attributes/re-exports may shadow it"));
                    }
                }
            } else {
                self.graph.limit(self.file, format!("Python symbol {name}: defining module dependency recorded; re-export targets are unknown"));
            }
        }
    }
}

impl Visitor for Analyzer<'_> {
    fn visit_stmt_import(&mut self, node: ast::StmtImport) {
        for alias in node.names {
            self.evidence = format!("import {}", alias.name);
            self.graph.import(self.file, &self.evidence);
            let resolution = self.absolute(alias.name.as_str());
            self.record(alias.name.as_str(), &resolution);
        }
    }

    fn visit_stmt_import_from(&mut self, node: ast::StmtImportFrom) {
        self.from(node);
    }

    fn visit_expr_call(&mut self, node: ast::ExprCall) {
        let dynamic = match node.func.as_ref() {
            ast::Expr::Name(name) => matches!(
                name.id.as_str(),
                "__import__" | "import_module" | "exec" | "eval"
            ),
            ast::Expr::Attribute(attr) => attr.attr.as_str() == "import_module",
            _ => false,
        };
        if dynamic {
            self.graph.warn(self.file, "Dynamic Python import/code execution is not evaluated; dependencies may be incomplete");
        }
        self.generic_visit_expr_call(node);
    }
}

pub(super) fn analyze(files: &Files, graph: &mut Graph) {
    let mut external: BTreeSet<String> = include_str!("python_stdlib.txt")
        .lines()
        .filter(|s| !s.starts_with('#'))
        .map(String::from)
        .collect();
    fn requirement_name(value: &str) -> Option<String> {
        let name = value
            .trim()
            .split(['[', '<', '>', '=', '!', '~', ';', ' ', '@'])
            .next()?;
        if name.is_empty() || name.starts_with(['-', '#']) {
            return None;
        }
        Some(name.to_lowercase().replace('-', "_"))
    }
    for (path, text) in files {
        let name = path.rsplit('/').next().unwrap();
        if name.starts_with("requirements") && name.ends_with(".txt") {
            external.extend(text.lines().filter_map(requirement_name));
        } else if name == "pyproject.toml" {
            if let Ok(value) = toml::from_str::<toml::Value>(text) {
                if let Some(deps) = value
                    .get("project")
                    .and_then(|p| p.get("dependencies"))
                    .and_then(|v| v.as_array())
                {
                    external.extend(
                        deps.iter()
                            .filter_map(|v| v.as_str())
                            .filter_map(requirement_name),
                    );
                }
            }
        }
    }
    let has_src = files
        .keys()
        .any(|p| p.starts_with("src/") && p.ends_with(".py"));
    let mut root_names: Vec<String> = if has_src {
        vec!["".into(), "src".into()]
    } else {
        vec!["".into()]
    };
    if let Some(config) = files.get(".impactmap.json") {
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Config {
            python_source_roots: Vec<String>,
        }
        let configured = serde_json::from_str::<Config>(config)
            .map_err(|e| e.to_string())
            .and_then(|config| {
                config
                    .python_source_roots
                    .iter()
                    .map(|p| {
                        if p == "." {
                            Ok(String::new())
                        } else {
                            crate::relative(p)
                        }
                    })
                    .collect::<Result<Vec<_>, _>>()
            });
        match configured {
            Ok(extra) => root_names.extend(extra),
            Err(e) => graph.warn(
                ".impactmap.json",
                format!("Invalid Python source roots; using defaults: {e}"),
            ),
        }
    }
    root_names.sort();
    root_names.dedup();
    let roots: Vec<&str> = root_names.iter().map(String::as_str).collect();
    if has_src {
        graph.limit("src", "Python absolute imports use repository root and src/ as candidate source roots; runtime sys.path is not evaluated");
    }
    for (path, text) in files.iter().filter(|(p, _)| p.ends_with(".py")) {
        match ast::Suite::parse(text, path) {
            Ok(suite) => {
                let mut analyzer = Analyzer {
                    files,
                    graph,
                    file: path,
                    roots: &roots,
                    evidence: String::new(),
                    external: &external,
                };
                for stmt in suite {
                    analyzer.visit_stmt(stmt);
                }
            }
            Err(e) => graph.warn(path, format!("Python syntax could not be parsed: {e}")),
        }
    }
}
