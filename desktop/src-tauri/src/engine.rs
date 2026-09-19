use serde_json::{json, Value};
use std::{
    fs::OpenOptions,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc, Mutex},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const RPC_TIMEOUT: Duration = Duration::from_secs(180);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(30);

#[cfg(windows)]
#[link(name = "user32")]
extern "system" {
    fn AllowSetForegroundWindow(process_id: u32) -> i32;
}

enum Slot {
    NotStarted,
    Running(Service),
    Failed(String),
    Stopped,
}

pub struct Engine {
    workspace: PathBuf,
    data_dir: PathBuf,
    slot: Mutex<Slot>,
}

impl Engine {
    pub fn new(data_dir: PathBuf) -> Self {
        let workspace = std::env::var_os("ROCK_MUSIC_HOME")
            .filter(|path| !path.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                let executable = std::env::current_exe().ok()?;
                let directory = executable.parent()?;
                directory
                    .join("engine/rock-music-engine.exe")
                    .is_file()
                    .then(|| directory.to_path_buf())
            })
            .unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .and_then(|path| path.parent())
                    .expect("src-tauri must live inside desktop in the workspace")
                    .to_path_buf()
            });
        Self {
            workspace,
            data_dir,
            slot: Mutex::new(Slot::NotStarted),
        }
    }

    pub fn log(&self, message: &str) {
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.data_dir.join("engine.log"))
        {
            let timestamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            let _ = writeln!(file, "[host {timestamp}] {message}");
        }
    }

    pub fn request(&self, request: Value) -> Result<Value, String> {
        let method = request
            .as_object()
            .and_then(|object| object.get("method"))
            .and_then(Value::as_str)
            .filter(|method| !method.trim().is_empty())
            .ok_or("RPC request must be an object containing a non-empty method string.")?;
        if method == "shutdown" {
            return self.shutdown();
        }
        // The lock covers write + reply, not just spawning: the wire protocol has no IDs.
        let mut slot = self.slot.lock().map_err(|_| "Engine state lock failed.")?;
        if matches!(*slot, Slot::NotStarted) {
            *slot = match self.start() {
                Ok(service) => Slot::Running(service),
                Err(error) => Slot::Failed(error),
            };
        }
        match &mut *slot {
            Slot::Running(service) => {
                #[cfg(windows)]
                if matches!(method, "play" | "resume" | "next" | "previous")
                    && request["params"]["activate_target"] == true
                {
                    // The venv launcher PID differs from the process running Python.
                    // Grant only that engine process the foreground rights from this click.
                    let identity =
                        service.request(&json!({ "method": "get_process_id", "params": {} }))?;
                    let pid = identity["pid"]
                        .as_u64()
                        .and_then(|pid| u32::try_from(pid).ok())
                        .filter(|pid| *pid > 0)
                        .ok_or("Invalid engine PID for focus handoff")?;
                    if unsafe { AllowSetForegroundWindow(pid) } == 0 {
                        self.log(&format!(
                            "Foreground handoff to engine {pid} was not granted: {}",
                            std::io::Error::last_os_error()
                        ));
                    } else {
                        self.log(&format!("Foreground handoff granted to engine {pid}."));
                    }
                }
                service.request(&request)
            }
            Slot::Failed(error) => Err(error.clone()),
            Slot::Stopped => Err("The Python engine has already stopped.".into()),
            Slot::NotStarted => unreachable!(),
        }
    }

    pub fn shutdown(&self) -> Result<Value, String> {
        let mut slot = self.slot.lock().map_err(|_| "Engine state lock failed.")?;
        match &mut *slot {
            Slot::Running(service) => service.shutdown(SHUTDOWN_TIMEOUT),
            // No child was ever created; there is no driver state owned by this host.
            Slot::NotStarted | Slot::Failed(_) | Slot::Stopped => {
                // Latch the stopped state so an already-queued RPC cannot spawn after close.
                *slot = Slot::Stopped;
                Ok(json!({ "closed": true }))
            }
        }
    }

    fn start(&self) -> Result<Service, String> {
        let python = self.workspace.join(".venv/Scripts/python.exe");
        let script = self.workspace.join("music_service.py");
        let bundled = self.workspace.join("engine/rock-music-engine.exe");
        let required = if bundled.is_file() {
            vec![&bundled]
        } else {
            vec![&python, &script]
        };
        for path in required {
            if !path.is_file() {
                return Err(format!(
                    "Python engine file not found: {}. This is a workspace build; set ROCK_MUSIC_HOME to the project root. Restart the player after fixing it.",
                    path.display()
                ));
            }
        }
        let stderr = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.data_dir.join("engine.log"))
            .map_err(|error| format!("Cannot open engine.log: {error}"))?;
        let mut command = if bundled.is_file() {
            Command::new(bundled)
        } else {
            let mut command = Command::new(python);
            command.arg("-u").arg(script);
            command
        };
        let driver = self.workspace.join("drivers/dd/ddhid.63340.dll");
        if std::env::var_os("DD_DLL_PATH").is_none() && driver.is_file() {
            command.env("DD_DLL_PATH", driver);
        }
        command
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .arg("--data-dir")
            .arg(&self.data_dir)
            .current_dir(&self.workspace)
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUTF8", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(stderr));
        if std::env::var("ROCK_MUSIC_SMOKE").as_deref() == Ok("1")
            || std::env::var("ROCK_MUSIC_NO_HOTKEYS").as_deref() == Ok("1")
        {
            command.arg("--no-hotkeys");
        }
        #[cfg(windows)]
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        let child = command
            .spawn()
            .map_err(|error| format!("Cannot start the Python engine: {error}"))?;
        self.log(&format!("Python engine started (pid {}).", child.id()));
        Ok(Service::new(child))
    }
}

struct Service {
    child: Child,
    stdin: ChildStdin,
    replies: mpsc::Receiver<Result<Value, String>>,
    fault: Option<String>,
    shutdown_sent: bool,
    shutdown_reply: Option<Value>,
}

impl Service {
    fn new(mut child: Child) -> Self {
        let stdin = child.stdin.take().expect("child stdin is piped");
        let stdout = child.stdout.take().expect("child stdout is piped");
        let (sender, replies) = mpsc::channel();
        // A dedicated reader permits bounded waits without blocking the WebView event loop.
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let mut line = String::new();
                let response = match reader.read_line(&mut line) {
                    Ok(0) => {
                        let _ =
                            sender.send(Err("Python engine closed stdout. See engine.log.".into()));
                        break;
                    }
                    Ok(_) => parse_frame(&line),
                    Err(error) => {
                        let _ = sender.send(Err(format!("Cannot read Python stdout: {error}")));
                        break;
                    }
                };
                if sender.send(response).is_err() {
                    break;
                }
            }
        });
        Self {
            child,
            stdin,
            replies,
            fault: None,
            shutdown_sent: false,
            shutdown_reply: None,
        }
    }

    fn send(&mut self, request: &Value) -> Result<(), String> {
        let mut bytes = serde_json::to_vec(request).map_err(|error| error.to_string())?;
        bytes.push(b'\n');
        self.stdin
            .write_all(&bytes)
            .and_then(|_| self.stdin.flush())
            .map_err(|error| format!("Cannot write to the Python engine: {error}. See engine.log."))
    }

    fn request(&mut self, request: &Value) -> Result<Value, String> {
        if self.shutdown_sent {
            return Err("The Python engine is shutting down or has already stopped.".into());
        }
        if let Some(error) = &self.fault {
            return Err(format!("Python RPC stream is unusable: {error}. Close the player to request a safe shutdown."));
        }
        let response = self.send(request).and_then(|_| {
            self.replies
                .recv_timeout(RPC_TIMEOUT)
                .map_err(|error| format!("Python engine did not reply: {error}. See engine.log."))?
        });
        match response {
            Ok(reply) => unpack_reply(reply),
            Err(error) => {
                // Never match later responses to new requests after a transport error.
                self.fault = Some(error.clone());
                Err(error)
            }
        }
    }

    fn shutdown(&mut self, timeout: Duration) -> Result<Value, String> {
        if !self.shutdown_sent {
            self.send(&json!({ "method": "shutdown", "params": {} }))?;
            self.shutdown_sent = true;
        }
        let deadline = Instant::now() + timeout;
        let mut stream_error: Option<String> = None;
        loop {
            if self.shutdown_reply.is_none() && stream_error.is_none() {
                match self.replies.recv_timeout(Duration::from_millis(100)) {
                    Ok(Ok(reply)) => match unpack_reply(reply) {
                        Ok(result) if result.get("closed") == Some(&Value::Bool(true)) => {
                            self.shutdown_reply = Some(result);
                        }
                        Ok(_) => {
                            self.fault = Some(
                                "Ignoring a non-shutdown reply while stopping the engine.".into(),
                            );
                        }
                        Err(error) => {
                            self.shutdown_sent = false;
                            return Err(format!("Python refused shutdown: {error}. The host and engine have not been killed; retry after resolving the error."));
                        }
                    },
                    Ok(Err(error)) => {
                        self.fault = Some(error.clone());
                        // A polluted line is not an acknowledgement. Drain subsequent lines.
                    }
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        stream_error =
                            Some("Python stdout disconnected before a shutdown reply.".into());
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                }
            } else {
                thread::sleep(Duration::from_millis(50));
            }
            if let Some(status) = self.child.try_wait().map_err(|error| error.to_string())? {
                if !status.success() {
                    return Err(format!(
                        "Python exited with {status}; key release is unconfirmed. See engine.log."
                    ));
                }
                if let Some(result) = &self.shutdown_reply {
                    return Ok(result.clone());
                }
                if let Some(error) = stream_error {
                    return Err(format!(
                        "Python exited without a confirmed shutdown: {error}"
                    ));
                }
            }
            if Instant::now() >= deadline {
                return Err("Timed out waiting for Python shutdown and normal exit. The host is staying open and has NOT killed the engine; retry closing to wait again. See engine.log.".into());
            }
        }
    }
}

fn parse_frame(line: &str) -> Result<Value, String> {
    let value: Value = serde_json::from_str(line).map_err(|_| {
        let preview: String = line.trim().chars().take(160).collect();
        format!("Python stdout is not a JSON reply: {preview:?}. Send diagnostics to stderr, not stdout. See engine.log.")
    })?;
    match value.get("ok").and_then(Value::as_bool) {
        Some(true) if value.get("result").is_some() => Ok(value),
        Some(false) if value.get("error").and_then(Value::as_str).is_some() => Ok(value),
        _ => Err("Python stdout contains an invalid RPC envelope; expected {ok:true,result:...} or {ok:false,error:string}. Send diagnostics to stderr.".into()),
    }
}

fn unpack_reply(reply: Value) -> Result<Value, String> {
    if reply["ok"] == true {
        Ok(reply["result"].clone())
    } else {
        Err(reply["error"]
            .as_str()
            .unwrap_or("Unknown Python error")
            .to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mock_service(mode: &str) -> Service {
        let python =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.venv/Scripts/python.exe");
        let code = r#"
import json, os, sys, time
mode = sys.argv[1]
stops = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if method == 'shutdown':
        stops += 1
        if mode == 'refuse_once' and stops == 1:
            print(json.dumps({'ok': False, 'error': 'mock keys still held'}), flush=True)
            continue
        print(json.dumps({'ok': True, 'result': {'closed': True}}), flush=True)
        if mode == 'delayed_exit':
            time.sleep(0.5)
        break
    if method == 'pollute':
        print('accidental diagnostic on stdout', flush=True)
    print(json.dumps({'ok': True, 'result': {'pid': os.getpid(), 'params': request.get('params')}}), flush=True)
"#;
        let mut command = Command::new(python);
        command
            .args(["-u", "-c", code, mode])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        #[cfg(windows)]
        command.creation_flags(0x08000000);
        Service::new(
            command
                .spawn()
                .expect("workspace Python required for IPC tests"),
        )
    }

    #[test]
    fn requests_are_serialized_and_reuse_one_child() {
        let mut service = mock_service("normal");
        // A Windows venv launcher can have a different PID from the interpreter it hosts.
        let pid = service.request(&json!({"method": "echo"})).unwrap()["pid"]
            .as_u64()
            .unwrap();
        let engine = std::sync::Arc::new(Engine {
            workspace: PathBuf::new(),
            data_dir: PathBuf::new(),
            slot: Mutex::new(Slot::Running(service)),
        });
        let workers: Vec<_> = (0..12)
            .map(|number| {
                let engine = std::sync::Arc::clone(&engine);
                thread::spawn(move || {
                    let reply = engine
                        .request(json!({"method": "echo", "params": number}))
                        .unwrap();
                    assert_eq!(reply["pid"], pid);
                    assert_eq!(reply["params"], number);
                })
            })
            .collect();
        for worker in workers {
            worker.join().unwrap();
        }
        assert_eq!(engine.shutdown().unwrap()["closed"], true);
    }

    #[test]
    fn shutdown_refusal_keeps_child_alive_and_can_retry() {
        let mut service = mock_service("refuse_once");
        let error = service.shutdown(Duration::from_secs(3)).unwrap_err();
        assert!(error.contains("mock keys still held"));
        assert!(service.child.try_wait().unwrap().is_none());
        assert_eq!(
            service.shutdown(Duration::from_secs(3)).unwrap()["closed"],
            true
        );
        assert!(service.child.try_wait().unwrap().unwrap().success());
    }

    #[test]
    fn shutdown_ack_alone_does_not_close_a_live_child() {
        let mut service = mock_service("delayed_exit");
        service.request(&json!({"method": "echo"})).unwrap();
        assert!(service.shutdown(Duration::from_millis(40)).is_err());
        assert!(service.child.try_wait().unwrap().is_none());
        assert_eq!(
            service.shutdown(Duration::from_secs(3)).unwrap()["closed"],
            true
        );
    }

    #[test]
    fn polluted_stdout_is_visible_and_still_allows_safe_shutdown() {
        let mut service = mock_service("normal");
        let error = service.request(&json!({"method": "pollute"})).unwrap_err();
        assert!(error.contains("stdout is not a JSON reply"));
        assert!(service.request(&json!({"method": "echo"})).is_err());
        assert_eq!(
            service.shutdown(Duration::from_secs(3)).unwrap()["closed"],
            true
        );
    }

    #[test]
    fn closing_before_first_rpc_prevents_late_spawn() {
        let engine = Engine::new(PathBuf::new());
        assert_eq!(engine.shutdown().unwrap()["closed"], true);
        assert!(engine
            .request(json!({"method": "get_state"}))
            .unwrap_err()
            .contains("already stopped"));
    }

    #[test]
    fn protocol_accepts_unicode_and_null() {
        assert_eq!(
            unpack_reply(parse_frame("{\"ok\":true,\"result\":null}\r\n").unwrap()).unwrap(),
            Value::Null
        );
        let frame = json!({"ok": true, "result": "\u{4e2d}\u{6587}"}).to_string();
        assert_eq!(
            unpack_reply(parse_frame(&frame).unwrap()).unwrap(),
            "\u{4e2d}\u{6587}"
        );
    }

    #[test]
    fn protocol_rejects_stdout_pollution_and_bad_envelopes() {
        for frame in [
            "debug logging\n",
            "{}",
            "{\"ok\":true}",
            "{\"ok\":false,\"error\":42}",
        ] {
            assert!(parse_frame(frame).is_err());
        }
        assert_eq!(
            unpack_reply(parse_frame("{\"ok\":false,\"error\":\"stop failed\"}").unwrap()),
            Err("stop failed".into())
        );
    }
}
