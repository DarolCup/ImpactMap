//! Offline, conservative file dependency analysis. Never executes repository code.
mod impact;
mod python;
mod rust;

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs;
use std::path::Path;

#[derive(Debug, Deserialize)]
pub struct Input {
    pub protocol_version: u32,
    pub repo_path: String,
    #[serde(default)]
    pub changed_files: Vec<String>,
    #[serde(default)]
    pub baseline_repo_path: Option<String>,
    #[serde(default)]
    pub changes: Vec<impact::Change>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Output {
    pub protocol_version: u32,
    pub analyzer_version: String,
    /// Readable source files, including isolated files without dependency edges.
    #[serde(default)]
    pub files: Vec<String>,
    /// Current snapshot only: excludes historical edges restored by comparison.
    #[serde(default)]
    pub current_edges: Vec<Edge>,
    pub changed_files: Vec<String>,
    pub affected_files: Vec<AffectedFile>,
    pub edges: Vec<Edge>,
    pub warnings: Vec<String>,
    #[serde(default)]
    pub impact: impact::Impact,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct AffectedFile {
    pub path: String,
    pub distance: usize,
    pub reason: String,
    #[serde(default)]
    pub chain: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct Edge {
    pub from: String,
    pub to: String,
    pub relation: String,
}

impl Output {
    pub fn failure(message: String) -> Self {
        Self {
            protocol_version: 1,
            analyzer_version: env!("CARGO_PKG_VERSION").into(),
            files: vec![],
            current_edges: vec![],
            changed_files: vec![],
            affected_files: vec![],
            edges: vec![],
            warnings: vec![message],
            impact: impact::Impact::default(),
        }
    }
}

#[derive(Default)]
struct Graph {
    edges: BTreeSet<Edge>,
    warnings: BTreeSet<String>,
    references: Vec<Reference>,
    imports: BTreeSet<(String, String)>,
}

#[derive(Clone)]
struct Reference {
    from: String,
    to: String,
    symbol: Option<String>,
    evidence: String,
}

impl Graph {
    fn reference(&mut self, from: &str, to: &str, symbol: Option<String>, evidence: &str) {
        self.edge(from, to);
        self.references.push(Reference {
            from: from.into(),
            to: to.into(),
            symbol,
            evidence: evidence.into(),
        });
    }

    fn import(&mut self, from: &str, evidence: &str) {
        self.imports.insert((from.into(), evidence.into()));
    }
    fn edge(&mut self, from: &str, to: &str) {
        if from != to {
            self.edges.insert(Edge {
                from: from.into(),
                to: to.into(),
                relation: "imports".into(),
            });
        }
    }

    fn warn(&mut self, file: &str, message: impl AsRef<str>) {
        self.warnings
            .insert(format!("{file}: {}", message.as_ref()));
    }
}

type Files = BTreeMap<String, String>;

/// Accept either path separator, but never permit absolute paths or repository escape.
fn relative(path: &str) -> Result<String, String> {
    let path = path.replace('\\', "/");
    if path.starts_with('/') || path.contains(':') || path.contains('\0') {
        return Err(format!("Path must be relative to repository: {path}"));
    }
    let mut parts = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => (),
            ".." => {
                if parts.pop().is_none() {
                    return Err(format!("Path escapes repository: {path}"));
                }
            }
            _ => parts.push(part),
        }
    }
    if parts.is_empty() {
        return Err("Empty file path".into());
    }
    Ok(parts.join("/"))
}

fn parent(path: &str) -> &str {
    path.rsplit_once('/').map_or("", |(dir, _)| dir)
}
fn join(dir: &str, name: &str) -> String {
    if dir.is_empty() {
        name.into()
    } else {
        format!("{dir}/{name}")
    }
}

fn local_join(dir: &str, path: &str) -> Result<String, String> {
    if path.starts_with(['/', '\\']) || path.contains(':') {
        return Err(format!("Absolute source path is unsupported: {path}"));
    }
    relative(&join(dir, path))
}

fn scan(root: &Path, dir: &Path, files: &mut Files, graph: &mut Graph) {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) => {
            graph.warn(
                &dir.strip_prefix(root)
                    .unwrap_or(dir)
                    .to_string_lossy()
                    .replace('\\', "/"),
                format!("Cannot read directory: {e}"),
            );
            return;
        }
    };
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(e) => {
                graph.warn("repository", format!("Cannot read entry: {e}"));
                continue;
            }
        };
        let path = entry.path();
        let Some(rel) = path
            .strip_prefix(root)
            .ok()
            .and_then(|p| p.to_str())
            .map(|p| p.replace('\\', "/"))
        else {
            graph.warn("repository", "Non-UTF-8 path skipped");
            continue;
        };
        let metadata = match fs::symlink_metadata(&path) {
            Ok(m) => m,
            Err(e) => {
                graph.warn(&rel, format!("Cannot read metadata: {e}"));
                continue;
            }
        };
        // Never follow symlinks or Windows junctions out of the requested tree.
        if metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
            graph.warn(
                &rel,
                "Symlink/reparse point skipped; dependencies through it are unknown",
            );
            continue;
        }
        if metadata.is_dir() {
            if ![
                ".git",
                "target",
                "node_modules",
                ".venv",
                "venv",
                "__pycache__",
            ]
            .contains(&entry.file_name().to_string_lossy().as_ref())
            {
                scan(root, &path, files, graph);
            }
        } else if path.extension().is_some_and(|e| e == "rs" || e == "py")
            || entry.file_name() == "Cargo.toml"
            || rel == ".impactmap.json"
        {
            match fs::read_to_string(&path) {
                Ok(text) => {
                    files.insert(rel, text.trim_start_matches('\u{feff}').into());
                }
                Err(e) => graph.warn(&rel, format!("Cannot read UTF-8 source: {e}")),
            }
        }
    }
}

#[cfg(windows)]
fn is_reparse_point(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    metadata.file_attributes() & 0x400 != 0
}
#[cfg(not(windows))]
fn is_reparse_point(_: &fs::Metadata) -> bool {
    false
}

pub use impact::{Change, ChangeStatus, Finding, Impact};

pub fn analyze(input: Input) -> Result<Output, String> {
    if input.protocol_version != 1 {
        return Err(format!(
            "Unsupported protocol_version {}; expected 1",
            input.protocol_version
        ));
    }
    let root =
        fs::canonicalize(&input.repo_path).map_err(|e| format!("Cannot open repo_path: {e}"))?;
    if !root.is_dir() {
        return Err("repo_path must be a directory".into());
    }
    let mut changed: BTreeSet<String> = input
        .changed_files
        .iter()
        .map(|p| relative(p))
        .collect::<Result<_, _>>()?;
    let mut renames = BTreeMap::new();
    for change in &input.changes {
        let path = relative(&change.path)?;
        changed.insert(path.clone());
        if change.status == ChangeStatus::Renamed {
            let old = relative(
                change
                    .old_path
                    .as_deref()
                    .ok_or("renamed change requires old_path")?,
            )?;
            if old == path || renames.insert(old.clone(), path).is_some() {
                return Err("Conflicting or duplicate rename paths".into());
            }
            changed.insert(old);
        } else if change.old_path.is_some() {
            return Err("old_path is only valid for renamed changes".into());
        }
    }
    let mut graph = Graph::default();
    let mut files = Files::new();
    scan(&root, &root, &mut files, &mut graph);
    rust::analyze(&files, &mut graph);
    python::analyze(&files, &mut graph);
    let current_edges = graph.edges.iter().cloned().collect();
    let mut impact = Impact::default();
    if let Some(baseline) = input.baseline_repo_path {
        if !Path::new(&baseline).is_absolute() {
            return Err("baseline_repo_path must be absolute".into());
        }
        let baseline = fs::canonicalize(baseline)
            .map_err(|e| format!("Cannot open baseline_repo_path: {e}"))?;
        if !baseline.is_dir() {
            return Err("baseline_repo_path must be a directory".into());
        }
        let mut previous = Graph::default();
        let mut before = Files::new();
        scan(&baseline, &baseline, &mut before, &mut previous);
        rust::analyze(&before, &mut previous);
        python::analyze(&before, &mut previous);
        impact = impact::compare(&before, &files, &previous, &mut graph, &changed, &renames);
        impact
            .limitations
            .extend(previous.warnings.iter().map(|w| format!("Baseline: {w}")));
        if baseline == root {
            impact.limitations.push("Baseline and current repository are the same directory; no independent previous snapshot is available".into());
        }
        for change in &input.changes {
            let path = relative(&change.path)?;
            let consistent = match change.status {
                ChangeStatus::Added => !before.contains_key(&path) && files.contains_key(&path),
                ChangeStatus::Modified => before.contains_key(&path) && files.contains_key(&path),
                ChangeStatus::Deleted => before.contains_key(&path) && !files.contains_key(&path),
                ChangeStatus::Renamed => {
                    before.contains_key(&relative(change.old_path.as_deref().unwrap())?)
                        && !files.contains_key(&relative(change.old_path.as_deref().unwrap())?)
                        && files.contains_key(&path)
                }
            };
            if !consistent {
                impact.limitations.push(format!("{path}: reported change status does not match readable snapshot files; status is not treated as proof"));
            }
        }
    } else {
        impact.limitations.push("No baseline_repo_path: deleted/renamed dependencies and public symbol changes cannot be verified".into());
    }
    for path in &changed {
        if !files.contains_key(path) {
            graph.warn(path, "Changed file is absent, excluded or unsupported; baseline may be needed to recover dependencies");
        }
    }
    impact.limitations.extend(graph.warnings.iter().cloned());
    impact.limitations.sort();
    impact.limitations.dedup();
    let mut reverse: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    let mut forward: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for edge in &graph.edges {
        reverse.entry(&edge.to).or_default().push(&edge.from);
        forward.entry(&edge.from).or_default().push(&edge.to);
    }
    let mut distances: BTreeMap<String, usize> = changed.iter().map(|p| (p.clone(), 0)).collect();
    let mut queue: VecDeque<String> = changed.iter().cloned().collect();
    while let Some(path) = queue.pop_front() {
        let distance = distances[&path] + 1;
        for dependent in reverse.get(path.as_str()).into_iter().flatten() {
            if !distances.contains_key(*dependent) {
                distances.insert((*dependent).into(), distance);
                queue.push_back((*dependent).into());
            }
        }
    }
    // Distances strictly decrease along a chain. Lexicographic tie-breaking is
    // independent of BFS queue order and cannot loop, even inside a cycle.
    let mut order: Vec<_> = distances.iter().map(|(p, d)| (*d, p.clone())).collect();
    order.sort();
    let mut chains: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (distance, path) in order {
        let chain = if distance == 0 {
            vec![path.clone()]
        } else {
            let tail = forward
                .get(path.as_str())
                .into_iter()
                .flatten()
                .filter(|p| distances.get(**p) == Some(&(distance - 1)))
                .filter_map(|p| chains.get(*p))
                .min()
                .unwrap();
            std::iter::once(path.clone())
                .chain(tail.iter().cloned())
                .collect()
        };
        chains.insert(path, chain);
    }
    let affected_files = distances
        .into_iter()
        .filter(|(path, d)| *d > 0 && files.contains_key(path))
        .collect::<Vec<_>>();
    let affected_files = affected_files
        .into_iter()
        .map(|(path, distance)| AffectedFile {
            chain: chains.remove(&path).unwrap(),
            path,
            distance,
            reason: "depends on changed file".into(),
        })
        .collect();
    Ok(Output {
        protocol_version: 1,
        analyzer_version: env!("CARGO_PKG_VERSION").into(),
        files: files.keys().filter(|p| p.ends_with(".rs") || p.ends_with(".py")).cloned().collect(),
        current_edges,
        changed_files: changed.into_iter().collect(),
        affected_files,
        edges: graph.edges.into_iter().collect(),
        warnings: graph.warnings.into_iter().collect(),
        impact,
    })
}
