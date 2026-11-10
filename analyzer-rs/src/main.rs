use impactmap_analyzer::{analyze, Input, Output};
use std::io::{self, Read, Write};

fn main() {
    let mut input = String::new();
    let result = io::stdin()
        .read_to_string(&mut input)
        .map_err(|e| format!("Cannot read stdin: {e}"))
        .and_then(|_| {
            serde_json::from_str::<Input>(&input).map_err(|e| format!("Invalid input JSON: {e}"))
        })
        .and_then(analyze);
    let (output, code) = match result {
        Ok(output) => (output, 0),
        Err(message) => (Output::failure(message), 1),
    };
    let stdout = io::stdout();
    let mut stdout = stdout.lock();
    if serde_json::to_writer(&mut stdout, &output).is_err() || writeln!(stdout).is_err() {
        std::process::exit(1);
    }
    std::process::exit(code);
}
