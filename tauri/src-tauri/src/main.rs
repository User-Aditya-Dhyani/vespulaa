#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Deserialize;
use std::{
  io::{BufRead, BufReader, Write},
  sync::{Arc, Mutex},
  fs, path::PathBuf
};
use ::std::collections::HashMap;
use directories::ProjectDirs;

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
  // Keep the whole child so it doesn't get dropped, and so we can wait() and log exit.
  child: Arc<Mutex<std::process::Child>>,
  stdin: Arc<Mutex<std::process::ChildStdin>>,
}

// ---------- nodes.json location ----------
fn nodes_db_path() -> PathBuf {
  let proj = ProjectDirs::from("org", "vespulaa", "Vespulaa")
    .expect("cannot resolve project dirs");
  let dir = proj.config_dir();
  let _ = fs::create_dir_all(&dir);
  dir.join("nodes.json")
}

// ---------- Read/write helpers for nodes.json ----------
fn read_nodes_value() -> Result<serde_json::Value, String> {
  let p = nodes_db_path();
  let s = fs::read_to_string(&p).map_err(|e| format!("read nodes.json failed: {e}"))?;
  serde_json::from_str::<serde_json::Value>(&s).map_err(|e| format!("parse nodes.json failed: {e}"))
}

fn write_nodes_value(v: &serde_json::Value) -> Result<(), String> {
  let p = nodes_db_path();
  let s = serde_json::to_string_pretty(v).map_err(|e| e.to_string())?;
  fs::write(&p, s).map_err(|e| format!("write nodes.json failed: {e}"))
}

// ---------- Commands ----------

/// Full read of Python-maintained nodes.json (read-only to the UI)
#[tauri::command]
async fn read_nodes_db() -> Result<serde_json::Value, String> {
  read_nodes_value()
}

/// Return a {uuid -> name} map derived from nodes.json entries that have "name".
#[tauri::command]
async fn get_node_names() -> Result<HashMap<String, String>, String> {
  let mut out: HashMap<String, String> = HashMap::new();
  let v = read_nodes_value().unwrap_or(serde_json::json!({}));
  if let Some(obj) = v.as_object() {
    for (uuid, entry) in obj {
      if let Some(name) = entry.get("name").and_then(|x| x.as_str()) {
        let clean: String = uuid.chars().filter(|c| c.is_ascii_hexdigit()).collect::<String>().to_lowercase();
        if clean.len() == 32 && !name.trim().is_empty() {
          out.insert(clean, name.trim().to_string());
        }
      }
    }
  }
  Ok(out)
}

/// Update only the "name" field for an entry in nodes.json (create entry if missing).
/// Performs a no-op if the value is unchanged, to avoid unnecessary writes.
#[tauri::command]
async fn set_node_name(uuid: String, name: String) -> Result<(), String> {
  let clean: String = uuid.chars().filter(|c| c.is_ascii_hexdigit()).collect::<String>().to_lowercase();
  if clean.len() != 32 {
    return Err("uuid must be 32 hex chars (16 bytes)".into());
  }
  let mut root = match read_nodes_value() {
    Ok(v) => v,
    Err(_) => serde_json::json!({}), // if missing, start fresh
  };

  let obj = root.as_object_mut().ok_or("nodes.json root is not an object")?;
  if !obj.contains_key(&clean) {
    obj.insert(clean.clone(), serde_json::json!({}));
  }

  let entry = obj.get_mut(&clean).and_then(|v| v.as_object_mut()).ok_or("entry is not an object")?;
  let trimmed = name.trim();
  let current = entry.get("name").and_then(|v| v.as_str()).unwrap_or("");

  if trimmed.is_empty() {
    // remove only if present; otherwise skip write
    if entry.get("name").is_some() {
      entry.remove("name");
    } else {
      return Ok(());
    }
  } else {
    if current == trimmed {
      return Ok(()); // no change
    }
    entry.insert("name".to_string(), serde_json::Value::String(trimmed.to_string()));
  }

  write_nodes_value(&root)
}

// ---------- Python bridge passthrough commands ----------
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
  let mut guard = py.stdin
    .lock()
    .map_err(|_| "bridge stdin lock poisoned".to_string())?;
  guard
    .write_all(line.as_bytes())
    .and_then(|_| guard.write_all(b"\n"))
    .map_err(|e| format!("write failed: {e}"))
}

// Spawn python bridge: we use vesp.tauri_bridge
fn spawn_python(handle: tauri::AppHandle) -> Result<PyProc, Box<dyn std::error::Error>> {
  use std::path::{Path, PathBuf};
  use std::process::{Command, Stdio};

  // 1) Try packaged locations inside the app resources dir.
  let mut py_module_root: Option<PathBuf> = None;
  if let Ok(res_root) = handle.path().resource_dir() {
    let cand1 = res_root.join("vesp");
    let cand2 = res_root.join("resources").join("vesp");
    let _ = handle.emit("log:app", format!("[RUST] resource_dir={}", res_root.display()));
    let _ = handle.emit("log:app", format!("[RUST] check {}", cand1.display()));
    let _ = handle.emit("log:app", format!("[RUST] check {}", cand2.display()));
    if cand1.is_dir() { py_module_root = Some(cand1); }
    else if cand2.is_dir() { py_module_root = Some(cand2); }
  }

  // 2) Dev fallback: walk up to find a folder that contains "vesp/"
  if py_module_root.is_none() {
    fn looks_like_root(p: &Path) -> bool { p.join("vesp").is_dir() }
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
      if let Some(p0) = exe.parent().and_then(|d| d.parent()).and_then(|d| d.parent()) {
        candidates.push(p0.to_path_buf());
      }
    }

    if let Some(dev_root) = candidates.into_iter().find(|p| looks_like_root(p)) {
      py_module_root = Some(dev_root.join("vesp"));
    }
  }

  let py_root = py_module_root.ok_or("Could not locate Python package 'vesp'")?;
  let py_parent = py_root.parent().ok_or("bad vesp path")?;

  let _ = handle.emit("log:app", format!("[RUST] using py_root={}", py_root.display()));
  let _ = handle.emit("log:app", format!("[RUST] PYTHONPATH will include {}", py_parent.display()));

  let mut cmd = Command::new("python3");
  cmd.arg("-u")
     .arg("-m").arg("vesp.tauri_bridge")
     .env("PYTHONPATH", {
        // prepend our module root to PYTHONPATH so `import vesp` works
        let old = std::env::var("PYTHONPATH").unwrap_or_default();
        if old.is_empty() { py_parent.display().to_string() }
        else { format!("{}:{}", py_parent.display(), old) }
     })
     .current_dir(py_parent)
     .stdin(Stdio::piped())
     .stdout(Stdio::piped())
     .stderr(Stdio::piped());

  let mut child = cmd.spawn()?;

  // Keep handles + spawn I/O threads
  let stdin = Arc::new(Mutex::new(child.stdin.take().expect("stdin")));
  let stdout = child.stdout.take().expect("stdout");
  let stderr = child.stderr.take().expect("stderr");
  let child_arc = Arc::new(Mutex::new(child));

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
      let _ = h.emit("log:app", "[PY] stdout closed");
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
      let _ = h.emit("log:app", "[PY] stderr closed");
    });
  }

  // Waiter thread to log child exit status clearly
  {
    let h = handle.clone();
    let child_for_wait = Arc::clone(&child_arc);
    std::thread::spawn(move || {
      let status = child_for_wait.lock().ok()
        .and_then(|mut c| c.wait().ok());
      let _ = h.emit("log:app", format!("[PY] exited with status {:?}", status));
    });
  }

  Ok(PyProc { child: child_arc, stdin })
}

use std::{fs::File, io::{Seek, SeekFrom}, time::{Duration, SystemTime}};

fn tail_file_to_event(handle: tauri::AppHandle, path: PathBuf, event: &'static str, read_last_kb: u64) {
  std::thread::spawn(move || {
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

          loop {
            buf.clear();
            match reader.read_line(&mut buf) {
              Ok(0) => break,      // EOF
              Ok(_) => { let _ = handle.emit(event, buf.trim_end().to_owned()); }
              Err(_) => break,
            }
          }

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

// Watch nodes.json mtime and notify UI only when it actually changes
fn watch_nodes_file(handle: tauri::AppHandle, path: PathBuf) {
  std::thread::spawn(move || {
    let mut last_mod: Option<SystemTime> = None;
    loop {
      match fs::metadata(&path) {
        Ok(meta) => {
          if let Ok(modt) = meta.modified() {
            let changed = match last_mod {
              None => true,
              Some(prev) => modt > prev,
            };
            if changed {
              last_mod = Some(modt);
              // small debounce to allow the writer to finish
              std::thread::sleep(Duration::from_millis(60));
              let _ = handle.emit("nodes:changed", "nodes.json updated");
            }
          }
        }
        Err(_) => {
          // If file disappears (purge), still notify once
          if last_mod.take().is_some() {
            let _ = handle.emit("nodes:changed", "nodes.json removed");
          }
        }
      }
      std::thread::sleep(Duration::from_millis(250));
    }
  });
}

fn main() {
  tauri::Builder::default()
    .setup(|app| {
      let handle = app.handle().clone();
      let _ = handle.emit("log:app", "[RUST] starting python bridge");
      let py = spawn_python(handle.clone())?;  // spawn bridge
      app.manage(py);                          // store stdin in State<PyProc>

      // start log tailers
      let home = std::env::var_os("HOME").map(std::path::PathBuf::from).unwrap_or_else(|| ".".into());
      let logdir = std::env::var_os("VESP_LOG_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or(home.join(".config/vespulaa/logs"));

      let app_log    = logdir.join("mesh_app.log");
      let devkey_log = logdir.join("mesh_devkey.log");

      tail_file_to_event(app.handle().clone(), app_log,    "log:nodes",  64);
      tail_file_to_event(app.handle().clone(), devkey_log, "log:devkey", 64);

      // watch nodes.json for true changes (mtime) and notify UI
      let nodes_path = nodes_db_path();
      watch_nodes_file(app.handle().clone(), nodes_path);

      Ok(())
    })
    .invoke_handler(tauri::generate_handler![
      // python bridge passthrough
      create_network,
      attach,
      detach,
      leave,
      purge,
      config_local_client,
      scan_start,
      scan_stop,
      provision_uuid,
      reset_node,
      // nodes.json integration
      read_nodes_db,
      get_node_names,
      set_node_name,
    ])
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}

