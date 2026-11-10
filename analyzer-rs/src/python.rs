use crate::{join, parent, Files, Graph};
use rustpython_ast::{self as ast, Visitor};
use rustpython_parser::Parse;

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
                    self.graph.warn(self.file, format!("Python namespace package {module}: local portion inferred; runtime search paths are unknown"));
                }
            }
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
            let root = if self.roots.contains(&"src") && self.file.starts_with("src/") {
                "src"
            } else {
                ""
            };
            let local = if root.is_empty() {
                self.file
            } else {
                self.file.strip_prefix("src/").unwrap()
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
                    Resolution::Missing => self.graph.warn(self.file, format!("Python symbol {name}: base dependency recorded; dynamic attributes and re-export targets are unknown")),
                    _ => {
                        self.record(&name, &child);
                        self.graph.warn(self.file, format!("Python from-import {name}: local submodule candidate recorded; package attributes/re-exports may shadow it"));
                    }
                }
            } else {
                self.graph.warn(self.file, format!("Python symbol {name}: defining module dependency recorded; re-export targets are unknown"));
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
    let has_src = files
        .keys()
        .any(|p| p.starts_with("src/") && p.ends_with(".py"));
    let roots = if has_src { vec!["", "src"] } else { vec![""] };
    if has_src {
        graph.warn("src", "Python absolute imports use repository root and src/ as candidate source roots; runtime sys.path is not evaluated");
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
                };
                for stmt in suite {
                    analyzer.visit_stmt(stmt);
                }
            }
            Err(e) => graph.warn(path, format!("Python syntax could not be parsed: {e}")),
        }
    }
}
