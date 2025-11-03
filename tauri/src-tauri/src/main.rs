#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Deserialize;
use std::{
  io::{BufRead, BufReader, Write},
  sync::{Arc, Mutex},
};

use tauri::{Emitter, Manager, State};

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "kebab-case")]
enum PyMsg {
  Ready { detail: Option<String> },
  Log { text: String },
  Scan { uuid: String, rssi: i32 },
  Result { cmd: String, ok: bool, msg: String },
}

struct PyProc {
  stdin: Arc<Mutex<std::process::ChildStdin>>,
}

// ---------- Commands mapping 1:1 to your Controller ----------
#[tauri::command]
async fn create_network(py: State<'_, PyProc>) -> Result<(), String> {
  send_cmd(&py, r#"{"cmd":"create_network"}"#)
}

#[tauri::command]
async fn attach(py: State<'_, PyProc>) -> Result<(), String> {
  send_cmd(&py, r#"{"cmd":"attach"}"#)
}

#[tauri::command]
async fn detach(py: State<'_, PyProc>) -> Result<(), String> {
  send_cmd(&py, r#"{"cmd":"detach"}"#)
}

#[tauri::command]
async fn leave(py: State<'_, PyProc>) -> Result<(), String> {
  send_cmd(&py, r#"{"cmd":"leave"}"#)
}

#[tauri::command]
async fn purge(py: State<'_, PyProc>) -> Result<(), String> {
  send_cmd(&py, r#"{"cmd":"purge"}"#)
}

#[tauri::command]
async fn config_local_client(py: State<'_, PyProc>) -> Result<(), String> {
  send_cmd(&py, r#"{"cmd":"config_local_client"}"#)
}

#[tauri::command]
async fn scan_start(py: State<'_, PyProc>, seconds: Option<u64>) -> Result<(), String> {
  let secs = seconds.unwrap_or(15);
  send_cmd(&py, &format!(r#"{{"cmd":"scan_start","seconds":{}}}"#, secs))
}

#[tauri::command]
async fn scan_stop(py: State<'_, PyProc>) -> Result<(), String> {
  send_cmd(&py, r#"{"cmd":"scan_stop"}"#)
}

#[tauri::command]
async fn provision_uuid(py: State<'_, PyProc>, uuid: String) -> Result<(), String> {
  send_cmd(&py, &format!(r#"{{"cmd":"provision_uuid","uuid":"{}"}}"#, uuid))
}

#[tauri::command]
async fn reset_node(py: State<'_, PyProc>, unicast: String) -> Result<(), String> {
  send_cmd(&py, &format!(r#"{{"cmd":"reset_node","unicast":"{}"}}"#, unicast))
}

fn send_cmd(py: &PyProc, line: &str) -> Result<(), String> {
  let mut guard = py
    .stdin
    .lock()
    .map_err(|_| "bridge stdin lock poisoned".to_string())?;
  guard
    .write_all(line.as_bytes())
    .and_then(|_| guard.write_all(b"\n"))
  .map_err(|e| format!("write failed: {e}"))
}

// Spawn python bridge: we use vesp.tauri_bridge (see section 3)
fn spawn_python(handle: tauri::AppHandle) -> Result<PyProc, Box<dyn std::error::Error>> {
  use std::path::{Path, PathBuf};

  fn looks_like_root(p: &Path) -> bool {
    p.join("vesp").is_dir()
  }

  // gather candidate dirs to search for `vesp/`
  let mut candidates: Vec<PathBuf> = Vec::new();

  if let Ok(cwd) = std::env::current_dir() {
    candidates.push(cwd.clone());
    if let Some(p1) = cwd.parent() {
      candidates.push(p1.to_path_buf());
      if let Some(p2) = p1.parent() {
        candidates.push(p2.to_path_buf());
      }
    }
  }

  if let Ok(exe) = std::env::current_exe() {
    // target/debug/tauri-app → parent()=.../debug → parent()=.../target → parent()=repo_root
    if let Some(p0) = exe.parent()
      .and_then(|d| d.parent())
      .and_then(|d| d.parent())
    {
      candidates.push(p0.to_path_buf());
    }
  }

  // first dir that contains vesp/
  let repo_root = candidates
    .into_iter()
    .find(|p| looks_like_root(p))
    .ok_or_else(|| "Could not locate repo root containing 'vesp/'".to_string())?;

  // run Python bridge from repo root with PYTHONPATH pointing at it
  let mut cmd = std::process::Command::new("python3");
  cmd.arg("-u").arg("-m").arg("vesp.tauri_bridge")
     .stdin(std::process::Stdio::piped())
     .stdout(std::process::Stdio::piped())
     .stderr(std::process::Stdio::piped())
     .current_dir(&repo_root);

  let new_pp = match std::env::var("PYTHONPATH") {
    Ok(old) => format!("{}:{}", repo_root.display(), old),
    Err(_)  => repo_root.display().to_string(),
  };
  cmd.env("PYTHONPATH", new_pp);

  let mut child = cmd.spawn()?;

  let stdin = std::sync::Arc::new(std::sync::Mutex::new(child.stdin.take().expect("stdin")));
  let stdout = child.stdout.take().expect("stdout");
  let stderr = child.stderr.take().expect("stderr");

  // stdout → frontend events
  {
    let h = handle.clone();
    std::thread::spawn(move || {
      let reader = std::io::BufReader::new(stdout);
      for line in reader.lines().flatten() {
        if let Ok(p) = serde_json::from_str::<PyMsg>(&line) {
          match p {
            PyMsg::Ready { detail } => {
              let _ = h.emit("log:app", format!("[PY] ready {}", detail.unwrap_or_default()));
            }
            PyMsg::Log { text } => { let _ = h.emit("log:python", text); }
            PyMsg::Scan { uuid, rssi } => {
              let _ = h.emit("scan:add", serde_json::json!({ "uuid": uuid, "rssi": rssi }));
            }
            PyMsg::Result { cmd, ok, msg } => {
              let _ = h.emit("log:app", format!("[BRIDGE] {cmd} → {} | {msg}", if ok {"ok"} else {"ERR"}));
              let _ = h.emit("resp:cmd", serde_json::json!({ "cmd": cmd, "ok": ok, "msg": msg }));
            }
          }
        } else {
          let _ = h.emit("log:python", line);
        }
      }
    });
  }

  // stderr passthrough
  {
    let h = handle.clone();
    std::thread::spawn(move || {
      let reader = std::io::BufReader::new(stderr);
      for line in reader.lines().flatten() {
        let _ = h.emit("log:python", format!("[stderr] {line}"));
      }
    });
  }

  Ok(PyProc { stdin })
}

use std::{fs::File, io::{Seek, SeekFrom}, path::PathBuf, time::Duration};

fn tail_file_to_event(handle: tauri::AppHandle, path: PathBuf, event: &'static str, read_last_kb: u64) {
  std::thread::spawn(move || {
    // Try to open; if missing, keep retrying silently every 1s
    loop {
      match File::open(&path) {
        Ok(mut f) => {
          let len = f.metadata().map(|m| m.len()).unwrap_or(0);
          if len > read_last_kb * 1024 {
            let _ = f.seek(SeekFrom::End(-((read_last_kb * 1024) as i64)));
          } else {
            let _ = f.seek(SeekFrom::Start(0));
          }

          let mut reader = BufReader::new(f);
          let mut buf = String::new();

          // read existing tail immediately
          loop {
            buf.clear();
            match reader.read_line(&mut buf) {
              Ok(0) => break,      // EOF
              Ok(_) => { let _ = handle.emit(event, buf.trim_end().to_owned()); }
              Err(_) => break,
            }
          }

          // now watch for new lines by polling file size
          loop {
            buf.clear();
            match reader.read_line(&mut buf) {
              Ok(0) => { std::thread::sleep(Duration::from_millis(400)); }
              Ok(_) => { let _ = handle.emit(event, buf.trim_end().to_owned()); }
              Err(_) => { std::thread::sleep(Duration::from_secs(1)); break; }
            }
          }
        }
        Err(_) => std::thread::sleep(Duration::from_secs(1)),
      }
    }
  });
}

fn main() {
  tauri::Builder::default()
    .setup(|app| {
      let handle = app.handle().clone();       // <-- important: pass owned handle
      let py = spawn_python(handle)?;          // spawn bridge
      app.manage(py);                          // store stdin in State<PyProc>
      let home = std::env::var_os("HOME").map(std::path::PathBuf::from).unwrap_or_else(|| ".".into());
      let logdir = std::env::var_os("VESP_LOG_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or(home.join(".config/vespulaa/logs"));

      let app_log    = logdir.join("mesh_app.log");     // will feed the "nodes" tab (per your ask)
      let devkey_log = logdir.join("mesh_devkey.log");  // will feed the "devkey" tab

      // Read last 64KB on first load, then stream new lines
      tail_file_to_event(app.handle().clone(),    app_log,    "log:nodes",  64);
      tail_file_to_event(app.handle().clone(),    devkey_log, "log:devkey", 64);
      Ok(())
    })
    .invoke_handler(tauri::generate_handler![
      create_network,
      attach,
      detach,
      leave,
      purge,
      config_local_client,
      scan_start,
      scan_stop,
      provision_uuid,
      reset_node
    ])
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}

