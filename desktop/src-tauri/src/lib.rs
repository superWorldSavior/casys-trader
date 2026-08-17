use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

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

#[tauri::command]
fn read_snapshot() -> Result<Value, String> {
    let root = repo_root();
    let script = root.join("desktop/bridge/snapshot.py");
    if !script.is_file() {
        return Err(format!("snapshot bridge missing: {}", script.display()));
    }

    let mut child = Command::new("uv")
        .args(["run", "python", "desktop/bridge/snapshot.py"])
        .current_dir(&root)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|err| format!("failed to spawn uv: {err}"))?;

    let started = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if started.elapsed() > Duration::from_secs(25) => {
                let _ = child.kill();
                return Err("snapshot timed out after 25s".into());
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(40)),
            Err(err) => return Err(format!("failed to wait for snapshot: {err}")),
        }
    }

    let output = child
        .wait_with_output()
        .map_err(|err| format!("failed to read snapshot output: {err}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "snapshot exited {}: {stderr}",
            output.status.code().unwrap_or(-1)
        ));
    }

    serde_json::from_slice(&output.stdout).map_err(|err| format!("invalid snapshot JSON: {err}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![read_snapshot])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
