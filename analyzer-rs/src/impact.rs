use crate::{Files, Graph};
use quote::ToTokens;
use rustpython_ast::{self as ast, Ranged};
use rustpython_parser::Parse;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Deserialize)]
pub struct Change {
    pub path: String,
    pub status: ChangeStatus,
    #[serde(default)]
    pub old_path: Option<String>,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChangeStatus {
    Added,
    Modified,
    Deleted,
    Renamed,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct Impact {
    pub findings: Vec<Finding>,
    pub limitations: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Finding {
    pub code: String,
    pub severity: String,
    pub confidence: String,
    pub paths: Vec<String>,
    pub evidence: String,
}

type Symbols = BTreeMap<String, Vec<String>>;

fn rust_symbols(items: &[syn::Item], symbols: &mut Symbols) {
    for item in items {
        let (name, visibility, signature) = match item {
            syn::Item::Fn(i) => (
                i.sig.ident.to_string(),
                &i.vis,
                i.sig.to_token_stream().to_string(),
            ),
            syn::Item::Struct(i) => (i.ident.to_string(), &i.vis, i.to_token_stream().to_string()),
            syn::Item::Enum(i) => (i.ident.to_string(), &i.vis, i.to_token_stream().to_string()),
            syn::Item::Type(i) => (i.ident.to_string(), &i.vis, i.to_token_stream().to_string()),
            syn::Item::Const(i) => (i.ident.to_string(), &i.vis, i.to_token_stream().to_string()),
            syn::Item::Static(i) => (i.ident.to_string(), &i.vis, i.to_token_stream().to_string()),
            syn::Item::Trait(i) => (i.ident.to_string(), &i.vis, i.to_token_stream().to_string()),
            syn::Item::Mod(i) => {
                if let Some((_, items)) = &i.content {
                    rust_symbols(items, symbols);
                }
                continue;
            }
            _ => continue,
        };
        if !matches!(visibility, syn::Visibility::Inherited) {
            symbols
                .entry(name.trim_start_matches("r#").into())
                .or_default()
                .push(format!("{} {signature}", visibility.to_token_stream()));
        }
    }
}

fn symbols(path: &str, text: &str) -> Result<Symbols, String> {
    let mut symbols = Symbols::new();
    if path.ends_with(".rs") {
        let file = syn::parse_file(text).map_err(|e| e.to_string())?;
        rust_symbols(&file.items, &mut symbols);
    } else if path.ends_with(".py") {
        let suite = ast::Suite::parse(text, path).map_err(|e| e.to_string())?;
        for stmt in suite {
            let entry = match &stmt {
                ast::Stmt::FunctionDef(i) => Some((i.name.as_str(), &i.body)),
                ast::Stmt::AsyncFunctionDef(i) => Some((i.name.as_str(), &i.body)),
                ast::Stmt::ClassDef(i) => Some((i.name.as_str(), &i.body)),
                _ => None,
            };
            if let Some((name, body)) = entry {
                if !name.starts_with('_') {
                    if let Some(first) = body.first() {
                        let header = &text
                            [stmt.range().start().to_usize()..first.range().start().to_usize()];
                        symbols
                            .entry(name.into())
                            .or_default()
                            .push(header.split_whitespace().collect::<Vec<_>>().join(" "));
                    }
                }
            }
        }
    }
    Ok(symbols)
}

pub(super) fn compare(
    before: &Files,
    after: &Files,
    previous: &Graph,
    current: &mut Graph,
    changed: &BTreeSet<String>,
    renames: &BTreeMap<String, String>,
) -> Impact {
    let mut findings = BTreeSet::new();
    let mut limitations = BTreeSet::from([
        "Findings describe static evidence, not proof of runtime failure. Check the reported imports, callers and tests.".into(),
        "Public symbol checks cover named Rust pub/restricted-pub functions, structs, enums, types, constants, statics and traits; Python top-level non-underscore functions/classes. Re-exports, macros, conditional definitions, methods, Python assignments/decorators and runtime binding are not resolved. Changed signatures do not imply incompatible calls.".into(),
    ]);
    let mut cache: BTreeMap<String, (Symbols, Symbols)> = BTreeMap::new();
    for path in changed {
        if let (Some(old), Some(new)) = (before.get(path), after.get(path)) {
            if old == new {
                continue;
            }
            match (symbols(path, old), symbols(path, new)) {
                (Ok(old), Ok(new)) => {
                    cache.insert(path.clone(), (old, new));
                }
                _ => {
                    limitations.insert(format!("{path}: public symbols could not be compared because a snapshot did not parse"));
                }
            }
        }
    }
    for reference in &previous.references {
        let importer = renames.get(&reference.from).unwrap_or(&reference.from);
        if !after.contains_key(importer)
            || !current
                .imports
                .contains(&(importer.clone(), reference.evidence.clone()))
        {
            continue;
        }
        if changed.contains(&reference.to) && !after.contains_key(&reference.to) {
            let renamed = renames.get(&reference.to);
            let valid_rename = renamed.filter(|p| after.contains_key(*p));
            let (code, mut paths, detail) = if let Some(new) = valid_rename {
                (
                    "renamed_dependency",
                    vec![importer.clone(), reference.to.clone(), new.clone()],
                    format!("Module moved to {new}, but the importer retains the old import"),
                )
            } else {
                (
                    "deleted_dependency",
                    vec![importer.clone(), reference.to.clone()],
                    "Previously imported local file is absent from the current snapshot".into(),
                )
            };
            paths.dedup();
            // A different current binding makes the old-path interpretation uncertain.
            let rebound = current.references.iter().any(|r| {
                r.from == *importer && r.evidence == reference.evidence && r.to != reference.to
            });
            let incomplete = current
                .warnings
                .iter()
                .chain(previous.warnings.iter())
                .any(|w| {
                    ![
                        "Cannot resolve Python import",
                        "Cannot reliably resolve use",
                        "Cannot reliably resolve mod",
                    ]
                    .iter()
                    .any(|expected| w.contains(expected))
                });
            let possible = rebound || incomplete;
            findings.insert(Finding { code: code.into(), severity: if possible { "warning" } else { "high" }.into(), confidence: if possible { "possible" } else { "confirmed" }.into(), paths,
                evidence: format!("{}: {}. {detail}. {}Check this import and dependent tests; no project code was executed.", importer, reference.evidence, if possible { "Bindings or snapshot resolution are incomplete. " } else { "Both snapshots confirm the file transition and unchanged import syntax. " }) });
            current.edge(importer, &reference.to);
            limitations.insert("Edges/chains may include baseline dependencies whose import syntax is still present but whose target was removed; these historical targets need verification.".into());
        }
        if let Some(name) = &reference.symbol {
            if let Some((old, new)) = cache.get(&reference.to) {
                let Some(old_signature) = old.get(name) else {
                    continue;
                };
                if old_signature.len() != 1 || new.get(name).is_some_and(|s| s.len() != 1) {
                    limitations.insert(format!(
                        "{}: symbol {name} has ambiguous declarations; comparison skipped",
                        reference.to
                    ));
                    continue;
                }
                if new.get(name) == Some(old_signature) {
                    continue;
                }
                findings.insert(Finding { code: "public_symbol_changed".into(), severity: "warning".into(), confidence: "possible".into(), paths: vec![importer.clone(), reference.to.clone()], evidence: format!("{importer}: {} references public symbol {name}. Before: {}. After: {}. Verify callers, argument/return expectations and tests; static declarations do not establish runtime compatibility.", reference.evidence, old_signature[0], new.get(name).map(|s| s.join(" | ")).unwrap_or_else(|| "public declaration removed or made private".into())) });
            }
        }
    }
    Impact {
        findings: findings.into_iter().collect(),
        limitations: limitations.into_iter().collect(),
    }
}
