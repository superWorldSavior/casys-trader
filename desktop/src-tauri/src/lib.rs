use serde_json::Value;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn repo_root() -> PathBuf {
    if let Ok(path) = std::env::var("CASYS_TRADER_ROOT") {
        return PathBuf::from(path);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|desktop| desktop.parent())
        .expect("desktop/src-tauri should sit two levels under the repo root")
        .to_path_buf()
}

fn uv_command() -> Command {
    let candidates = [
        std::env::var("UV_BIN").ok().map(PathBuf::from),
        Some(PathBuf::from("/opt/homebrew/bin/uv")),
        Some(PathBuf::from("/usr/local/bin/uv")),
        Some(PathBuf::from("uv")),
    ];
    let bin = candidates
        .into_iter()
        .flatten()
        .find(|path| path == Path::new("uv") || path.is_file())
        .unwrap_or_else(|| PathBuf::from("uv"));

    let mut cmd = Command::new(bin);
    if let Ok(path) = std::env::var("PATH") {
        if !path.split(':').any(|part| part == "/opt/homebrew/bin") {
            cmd.env("PATH", format!("/opt/homebrew/bin:/usr/local/bin:{path}"));
        }
    }
    cmd
}

#[tauri::command]
fn read_snapshot() -> Result<Value, String> {
    let root = repo_root();
    let script = root.join("desktop/bridge/snapshot.py");
    if !script.is_file() {
        return Err(format!("snapshot bridge missing: {}", script.display()));
    }

    let mut child = uv_command()
        .args(["run", "python", "desktop/bridge/snapshot.py"])
        .current_dir(&root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("failed to spawn uv: {err}"))?;

    let mut stdout_pipe = child
        .stdout
        .take()
        .ok_or_else(|| "snapshot stdout pipe missing".to_string())?;
    let mut stderr_pipe = child
        .stderr
        .take()
        .ok_or_else(|| "snapshot stderr pipe missing".to_string())?;

    let stdout_thread = std::thread::spawn(move || {
        let mut buf = Vec::new();
        stdout_pipe.read_to_end(&mut buf).ok();
        buf
    });
    let stderr_thread = std::thread::spawn(move || {
        let mut buf = Vec::new();
        stderr_pipe.read_to_end(&mut buf).ok();
        buf
    });

    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() > Duration::from_secs(25) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("snapshot timed out after 25s".into());
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(40)),
            Err(err) => return Err(format!("failed to wait for snapshot: {err}")),
        }
    };

    let stdout = stdout_thread
        .join()
        .map_err(|_| "snapshot stdout thread panicked".to_string())?;
    let stderr = stderr_thread
        .join()
        .map_err(|_| "snapshot stderr thread panicked".to_string())?;

    if !status.success() {
        return Err(format!(
            "snapshot exited {}: {}",
            status.code().unwrap_or(-1),
            String::from_utf8_lossy(&stderr)
        ));
    }

    serde_json::from_slice(&stdout).map_err(|err| {
        format!(
            "invalid snapshot JSON: {err} ({})",
            String::from_utf8_lossy(&stderr)
        )
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![read_snapshot])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
