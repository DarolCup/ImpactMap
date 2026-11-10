use crate::{join, local_join, parent, relative, Files, Graph};
use quote::ToTokens;
use std::collections::{BTreeMap, BTreeSet};
use syn::visit::Visit;
use syn::{Attribute, Expr, ItemMod, ItemUse, Lit, Meta, UseTree};

#[derive(Clone)]
struct Import {
    file: String,
    module: Vec<String>,
    path: Vec<String>,
    absolute: bool,
    evidence: String,
}

struct Unit {
    root: String,
    library_name: Option<String>,
    modules: BTreeMap<Vec<String>, String>,
    symbols: BTreeMap<Vec<String>, BTreeSet<String>>,
    imports: Vec<Import>,
}

fn ident(id: &syn::Ident) -> String {
    id.to_string().trim_start_matches("r#").into()
}

fn use_paths(tree: &UseTree, prefix: Vec<String>, paths: &mut Vec<Vec<String>>) {
    match tree {
        UseTree::Path(path) => {
            let mut prefix = prefix;
            prefix.push(ident(&path.ident));
            use_paths(&path.tree, prefix, paths);
        }
        UseTree::Group(group) => {
            for item in &group.items {
                use_paths(item, prefix.clone(), paths);
            }
        }
        UseTree::Name(name) => {
            let mut p = prefix;
            p.push(ident(&name.ident));
            paths.push(p);
        }
        UseTree::Rename(rename) => {
            let mut p = prefix;
            p.push(ident(&rename.ident));
            paths.push(p);
        }
        UseTree::Glob(_) => {
            let mut p = prefix;
            p.push("*".into());
            paths.push(p);
        }
    }
}

fn use_bindings(tree: &UseTree, parent_name: Option<String>, names: &mut BTreeSet<String>) {
    match tree {
        UseTree::Path(path) => use_bindings(&path.tree, Some(ident(&path.ident)), names),
        UseTree::Group(group) => {
            for tree in &group.items {
                use_bindings(tree, parent_name.clone(), names);
            }
        }
        UseTree::Name(name) => {
            if name.ident == "self" {
                if let Some(name) = parent_name {
                    names.insert(name);
                }
            } else {
                names.insert(ident(&name.ident));
            }
        }
        UseTree::Rename(rename) => {
            names.insert(ident(&rename.rename));
        }
        UseTree::Glob(_) => (),
    }
}

struct Collector<'a> {
    files: &'a Files,
    graph: &'a mut Graph,
    unit: &'a mut Unit,
    file: String,
    module: Vec<String>,
    module_dir: String,
    explicit_dir: String,
    active: BTreeSet<String>,
}

impl Collector<'_> {
    fn file(&mut self) {
        if !self.active.insert(self.file.clone()) {
            self.graph
                .warn(&self.file, "Recursive #[path] module declaration skipped");
            return;
        }
        match syn::parse_file(&self.files[&self.file]) {
            Ok(ast) => self.visit_file(&ast),
            Err(e) => self
                .graph
                .warn(&self.file, format!("Rust syntax could not be parsed: {e}")),
        }
        self.active.remove(&self.file);
    }

    fn attributes(&mut self, attrs: &[Attribute]) {
        if attrs
            .iter()
            .any(|a| a.path().is_ident("cfg") || a.path().is_ident("cfg_attr"))
        {
            self.graph.limit(&self.file, "Conditional Rust dependencies are analyzed conservatively; cfg/build features are not evaluated");
        }
    }
}

impl<'ast> Visit<'ast> for Collector<'_> {
    fn visit_item(&mut self, item: &'ast syn::Item) {
        let name = match item {
            syn::Item::Const(i) => Some(&i.ident),
            syn::Item::Enum(i) => Some(&i.ident),
            syn::Item::Fn(i) => Some(&i.sig.ident),
            syn::Item::Static(i) => Some(&i.ident),
            syn::Item::Struct(i) => Some(&i.ident),
            syn::Item::Trait(i) => Some(&i.ident),
            syn::Item::TraitAlias(i) => Some(&i.ident),
            syn::Item::Type(i) => Some(&i.ident),
            syn::Item::Union(i) => Some(&i.ident),
            _ => None,
        };
        if let Some(name) = name {
            self.unit
                .symbols
                .entry(self.module.clone())
                .or_default()
                .insert(ident(name));
        }
        syn::visit::visit_item(self, item);
    }

    fn visit_attribute(&mut self, attr: &'ast Attribute) {
        self.attributes(std::slice::from_ref(attr));
        syn::visit::visit_attribute(self, attr);
    }

    fn visit_item_mod(&mut self, item: &'ast ItemMod) {
        let evidence = format!(
            "Rust module {}: mod {} {}",
            self.module.join("::"),
            item.ident,
            item.attrs
                .iter()
                .map(|a| a.to_token_stream().to_string())
                .collect::<Vec<_>>()
                .join(" ")
        );
        self.graph.import(&self.file, &evidence);
        self.attributes(&item.attrs);
        let name = ident(&item.ident);
        let mut module = self.module.clone();
        module.push(name.clone());
        let previous_file = self.file.clone();
        let previous_dir = self.module_dir.clone();
        let previous_module = self.module.clone();
        let previous_explicit_dir = self.explicit_dir.clone();
        if let Some((_, items)) = &item.content {
            self.unit.modules.insert(module.clone(), self.file.clone());
            self.module = module;
            self.module_dir = join(&self.module_dir, &name);
            self.explicit_dir = self.module_dir.clone();
            if item.attrs.iter().any(|a| a.path().is_ident("path")) {
                self.graph.warn(
                    &self.file,
                    "#[path] on an inline module is unsupported; nested declarations skipped",
                );
            } else {
                for item in items {
                    self.visit_item(item);
                }
            }
        } else {
            let explicit = item.attrs.iter().find(|a| a.path().is_ident("path"));
            let candidates = if let Some(attr) = explicit {
                match &attr.meta {
                    Meta::NameValue(value) => match &value.value {
                        Expr::Lit(value) => match &value.lit {
                            Lit::Str(path) => match local_join(&self.explicit_dir, &path.value()) {
                                Ok(path) => vec![path],
                                Err(e) => {
                                    self.graph.warn(&self.file, e);
                                    vec![]
                                }
                            },
                            _ => vec![],
                        },
                        _ => vec![],
                    },
                    _ => vec![],
                }
            } else {
                vec![
                    join(&self.module_dir, &format!("{name}.rs")),
                    join(&self.module_dir, &format!("{name}/mod.rs")),
                ]
            };
            let found: Vec<_> = candidates
                .into_iter()
                .filter(|p| self.files.contains_key(p))
                .collect();
            if found.len() == 1 {
                self.file = found[0].clone();
                self.graph
                    .reference(&previous_file, &self.file, None, &evidence);
                self.unit.modules.insert(module.clone(), self.file.clone());
                self.module = module;
                self.explicit_dir = parent(&self.file).into();
                self.module_dir = if self.file.ends_with("/mod.rs") {
                    parent(&self.file).into()
                } else {
                    self.file.strip_suffix(".rs").unwrap().into()
                };
                self.file();
            } else {
                self.graph.warn(
                    &previous_file,
                    format!(
                        "Cannot reliably resolve mod {name}: {} local source files found",
                        found.len()
                    ),
                );
            }
        }
        self.file = previous_file;
        self.module_dir = previous_dir;
        self.module = previous_module;
        self.explicit_dir = previous_explicit_dir;
    }

    fn visit_item_use(&mut self, item: &'ast ItemUse) {
        self.attributes(&item.attrs);
        use_bindings(
            &item.tree,
            None,
            self.unit.symbols.entry(self.module.clone()).or_default(),
        );
        if !matches!(item.vis, syn::Visibility::Inherited) {
            self.graph.limit(&self.file, "Rust re-export recorded at its source; downstream symbol/re-export chains are not expanded");
        }
        let mut paths = Vec::new();
        use_paths(&item.tree, vec![], &mut paths);
        for path in paths {
            let evidence = format!(
                "Rust module {}: use {}{}",
                self.module.join("::"),
                if item.leading_colon.is_some() {
                    "::"
                } else {
                    ""
                },
                path.join("::")
            );
            self.graph.import(&self.file, &evidence);
            if path.last().is_some_and(|p| p == "*") {
                self.graph.warn(&self.file, "Rust wildcard import: module dependency recorded, re-export targets are unknown");
            }
            self.unit.imports.push(Import {
                evidence,
                file: self.file.clone(),
                module: self.module.clone(),
                path,
                absolute: item.leading_colon.is_some(),
            });
        }
    }

    fn visit_macro(&mut self, item: &'ast syn::Macro) {
        self.graph.limit(&self.file, "Rust macro contents/expansion are not analyzed; generated imports and include! dependencies are unknown");
        syn::visit::visit_macro(self, item);
    }

    fn visit_item_extern_crate(&mut self, _: &'ast syn::ItemExternCrate) {
        self.graph.warn(
            &self.file,
            "extern crate aliases and Rust 2015 import semantics are unsupported",
        );
    }
}

fn add_root(
    roots: &mut BTreeMap<String, Option<String>>,
    files: &Files,
    path: String,
    name: Option<String>,
    graph: &mut Graph,
) {
    match relative(&path) {
        Ok(path) if files.contains_key(&path) => {
            roots.entry(path).or_insert(name);
        }
        Ok(path) => graph.warn(&path, "Declared Rust target is missing or excluded"),
        Err(e) => graph.warn("Cargo.toml", e),
    }
}

fn roots(files: &Files, graph: &mut Graph) -> BTreeMap<String, Option<String>> {
    let mut roots = BTreeMap::new();
    for (manifest, text) in files.iter().filter(|(p, _)| p.ends_with("Cargo.toml")) {
        let value: toml::Value = match toml::from_str(text) {
            Ok(value) => value,
            Err(e) => {
                graph.warn(manifest, format!("Cannot parse Cargo manifest: {e}"));
                continue;
            }
        };
        let dir = parent(manifest);
        let package_name = value
            .get("package")
            .and_then(|p| p.get("name"))
            .and_then(|n| n.as_str());
        if package_name.is_none() {
            continue;
        } // Virtual workspace.
        let lib = value.get("lib");
        let lib_path = join(
            dir,
            lib.and_then(|v| v.get("path"))
                .and_then(|v| v.as_str())
                .unwrap_or("src/lib.rs"),
        );
        if lib.is_some() || files.contains_key(&lib_path) {
            let name = lib
                .and_then(|v| v.get("name"))
                .and_then(|v| v.as_str())
                .or(package_name)
                .map(|n| n.replace('-', "_"));
            add_root(&mut roots, files, lib_path, name, graph);
        }
        let main = join(dir, "src/main.rs");
        if files.contains_key(&main) {
            add_root(&mut roots, files, main, None, graph);
        }
        for kind in ["bin", "test", "example", "bench"] {
            if let Some(targets) = value.get(kind).and_then(|v| v.as_array()) {
                for target in targets {
                    if let Some(path) = target.get("path").and_then(|v| v.as_str()) {
                        add_root(&mut roots, files, join(dir, path), None, graph);
                    } else if let Some(name) = target.get("name").and_then(|v| v.as_str()) {
                        let folder = match kind {
                            "bin" => "src/bin",
                            "test" => "tests",
                            "example" => "examples",
                            _ => "benches",
                        };
                        let possibilities = [
                            join(dir, &format!("{folder}/{name}.rs")),
                            join(dir, &format!("{folder}/{name}/main.rs")),
                        ];
                        let found: Vec<_> = possibilities
                            .into_iter()
                            .filter(|p| files.contains_key(p))
                            .collect();
                        if found.len() == 1 {
                            add_root(&mut roots, files, found[0].clone(), None, graph);
                        } else if kind != "bin" || !files.contains_key(&join(dir, "src/main.rs")) {
                            graph.warn(
                                manifest,
                                format!("Cannot reliably resolve {kind} target {name}"),
                            );
                        }
                    }
                }
            }
        }
        if value.get("build-dependencies").is_some()
            || value.get("package").and_then(|p| p.get("build")).is_some()
            || files.contains_key(&join(dir, "build.rs"))
        {
            graph.warn(
                manifest,
                "Build scripts are not executed; generated source dependencies are unknown",
            );
        }
    }
    // Conventional crate roots also support small repositories without a manifest.
    for path in files.keys().filter(|p| p.ends_with(".rs")) {
        let filename = path.rsplit('/').next().unwrap();
        if matches!(filename, "lib.rs" | "main.rs")
            || path == "build.rs"
            || ["src/bin/", "tests/", "examples/", "benches/"]
                .iter()
                .any(|prefix| {
                    let suffix = path
                        .strip_prefix(prefix)
                        .or_else(|| path.split_once(&format!("/{prefix}")).map(|(_, rest)| rest));
                    suffix.is_some_and(|s| !s.contains('/'))
                })
        {
            roots.entry(path.clone()).or_insert(None);
        }
    }
    roots
}

fn target(unit: &Unit, path: &[String]) -> Option<String> {
    if let Some(file) = unit.modules.get(path) {
        return Some(file.clone());
    }
    // Only infer a trailing item when it was actually declared/imported there.
    if let Some((symbol, module)) = path.split_last() {
        if unit
            .symbols
            .get(module)
            .is_some_and(|names| names.contains(symbol))
        {
            return unit.modules.get(module).cloned();
        }
    }
    None
}

fn external_crates(files: &Files, root: &str) -> BTreeSet<String> {
    let mut names: BTreeSet<String> = ["std", "core", "alloc", "proc_macro"]
        .map(String::from)
        .into();
    let manifest = files
        .iter()
        .filter(|(p, _)| {
            p.rsplit('/').next() == Some("Cargo.toml")
                && (parent(p).is_empty() || root.starts_with(&format!("{}/", parent(p))))
        })
        .max_by_key(|(p, _)| parent(p).len());
    fn dependencies(value: &toml::Value, names: &mut BTreeSet<String>) {
        if let Some(table) = value.as_table() {
            for (key, value) in table {
                if ["dependencies", "dev-dependencies", "build-dependencies"]
                    .contains(&key.as_str())
                {
                    if let Some(deps) = value.as_table() {
                        for (name, spec) in deps {
                            // Path dependencies are local and must retain unresolved diagnostics.
                            if spec.get("path").is_none() {
                                names.insert(name.replace('-', "_"));
                            }
                        }
                    }
                } else if key == "target" {
                    if let Some(targets) = value.as_table() {
                        for value in targets.values() {
                            dependencies(value, names);
                        }
                    }
                }
            }
        }
    }
    if let Some((_, text)) = manifest {
        if let Ok(value) = toml::from_str::<toml::Value>(text) {
            dependencies(&value, &mut names);
        }
    }
    names
}

pub(super) fn analyze(files: &Files, graph: &mut Graph) {
    let mut units = Vec::new();
    for (root, library_name) in roots(files, graph) {
        let mut unit = Unit {
            root: root.clone(),
            library_name,
            modules: BTreeMap::from([(vec![], root.clone())]),
            symbols: BTreeMap::new(),
            imports: vec![],
        };
        Collector {
            files,
            graph,
            unit: &mut unit,
            file: root.clone(),
            module: vec![],
            module_dir: parent(&root).into(),
            explicit_dir: parent(&root).into(),
            active: BTreeSet::new(),
        }
        .file();
        units.push(unit);
    }
    let covered: BTreeSet<_> = units
        .iter()
        .flat_map(|u| u.modules.values().cloned())
        .collect();
    for path in files
        .keys()
        .filter(|p| p.ends_with(".rs") && !covered.contains(*p))
    {
        graph.warn(path, "Rust source is not reachable from a known crate root; crate context and imports are unknown");
    }
    for unit in &units {
        let external = external_crates(files, &unit.root);
        for import in &unit.imports {
            let mut path = import.path.clone();
            if path.last().is_some_and(|p| p == "*" || p == "self") {
                path.pop();
            }
            let Some(first) = path.first().cloned() else {
                continue;
            };
            let mut candidates = BTreeSet::new();
            let explicit = matches!(first.as_str(), "crate" | "self" | "super");
            if explicit && !import.absolute {
                let mut base = if first == "crate" {
                    vec![]
                } else {
                    import.module.clone()
                };
                path.remove(0);
                let mut valid = true;
                if first == "super" && base.pop().is_none() {
                    valid = false;
                }
                while path.first().is_some_and(|p| p == "super") {
                    path.remove(0);
                    if base.pop().is_none() {
                        valid = false;
                    }
                }
                base.extend(path);
                if valid {
                    if let Some(file) = target(unit, &base) {
                        candidates.insert(file);
                    }
                }
            } else if !explicit
                && external.contains(&first)
                && !unit.modules.keys().any(|p| p.first() == Some(&first))
                && !unit
                    .modules
                    .contains_key(&[import.module.clone(), vec![first.clone()]].concat())
                && !units
                    .iter()
                    .any(|u| u.library_name.as_deref() == Some(&first))
            {
                // Known external imports are outside this local dependency graph.
                continue;
            } else {
                // Local library crate names (including other workspace members), if unique.
                let libs: Vec<_> = units
                    .iter()
                    .filter(|u| u.library_name.as_deref() == Some(&first))
                    .collect();
                if libs.len() == 1 {
                    graph.limit(&import.file, format!("Local crate {first} matched by manifest name; Cargo dependency aliases and workspace dependency inheritance are not evaluated"));
                    if let Some(file) = target(libs[0], &path[1..]) {
                        candidates.insert(file);
                    }
                } else if libs.len() > 1 {
                    graph.warn(
                        &import.file,
                        format!("Ambiguous local crate name {first}; dependency not inferred"),
                    );
                    continue;
                }
                if !import.absolute {
                    let mut local = import.module.clone();
                    local.extend(path.clone());
                    // Bare names must start with a declared module; do not guess aliases/items.
                    if unit.modules.contains_key(&local[..import.module.len() + 1]) {
                        if let Some(file) = target(unit, &local) {
                            candidates.insert(file);
                        }
                    }
                    if unit.modules.contains_key(&path[..1]) {
                        if let Some(file) = target(unit, &path) {
                            candidates.insert(file);
                        }
                    }
                }
            }
            if candidates.len() == 1 {
                graph.reference(
                    &import.file,
                    candidates.iter().next().unwrap(),
                    import
                        .path
                        .last()
                        .filter(|p| !["*", "self"].contains(&p.as_str()))
                        .cloned(),
                    &import.evidence,
                );
            } else {
                graph.warn(&import.file, format!("Cannot reliably resolve use {} in crate {} (external dependency, alias, re-export, or ambiguous module)", import.path.join("::"), unit.root));
            }
        }
    }
}
