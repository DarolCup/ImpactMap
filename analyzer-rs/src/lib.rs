//! Offline, conservative file dependency analysis. Never executes repository code.
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
    pub changed_files: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Output {
    pub protocol_version: u32,
    pub changed_files: Vec<String>,
    pub affected_files: Vec<AffectedFile>,
    pub edges: Vec<Edge>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct AffectedFile {
    pub path: String,
    pub distance: usize,
    pub reason: String,
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
            changed_files: vec![],
            affected_files: vec![],
            edges: vec![],
            warnings: vec![message],
        }
    }
}

#[derive(Default)]
struct Graph {
    edges: BTreeSet<Edge>,
    warnings: BTreeSet<String>,
}

impl Graph {
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
    let changed: BTreeSet<String> = input
        .changed_files
        .iter()
        .map(|p| relative(p))
        .collect::<Result<_, _>>()?;
    let mut graph = Graph::default();
    let mut files = Files::new();
    scan(&root, &root, &mut files, &mut graph);
    for path in &changed {
        if !files.contains_key(path) {
            graph.warn(path, "Changed file is missing, excluded, or unsupported; deleted-file imports may not resolve");
        }
    }
    rust::analyze(&files, &mut graph);
    python::analyze(&files, &mut graph);
    let mut reverse: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for edge in &graph.edges {
        reverse.entry(&edge.to).or_default().push(&edge.from);
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
    let affected_files = distances
        .into_iter()
        .filter(|(_, distance)| *distance > 0)
        .map(|(path, distance)| AffectedFile {
            path,
            distance,
            reason: "depends on changed file".into(),
        })
        .collect();
    Ok(Output {
        protocol_version: 1,
        changed_files: changed.into_iter().collect(),
        affected_files,
        edges: graph.edges.into_iter().collect(),
        warnings: graph.warnings.into_iter().collect(),
    })
}
