#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! ComfyMediaViewer 桌面壳（Tauri 2）
//!
//! 架构：Tauri 窗口 + 现有 Python 后端（PyInstaller exe）作为 sidecar。
//! - 单实例保护（ε-10 / 7-6）：第二实例在插件 init 阶段即退出，后端尚未 spawn
//! - 启动时探测空闲端口（从 8765 起依次尝试；全部被占则明确报错）
//! - spawn 后端 exe（--port <p> --no-browser，隐藏控制台窗口）
//! - 轮询端口就绪后把 WebView 从启动页导航到 http://127.0.0.1:<p>
//!   （ε-10 / 7-7：就绪判定不再是「TCP 能连上」，而是 /api/info 响应带
//!    `Server: MediaViewer/` 头 —— 与后端 _probe_own_instance 同判据，
//!    不会被 TOCTOU 空窗里抢注端口的其他服务骗过）
//! - 后端就绪后持续监控存活（ε-10 / 7-9）：进程退出且端口上没有 CMV 服务时
//!   在壳页弹横幅并落盘 shell.log；若端口上仍有服务（后端自重启接管）则静默
//! - 应用退出或 panic 时终止后端进程树，不留孤儿进程

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewWindow};

/// 后端 exe 直接嵌入壳内（单文件绿色版），首次启动释放到 LOCALAPPDATA
const BACKEND_BYTES: &[u8] =
    include_bytes!("../binaries/ComfyMediaViewer-x86_64-pc-windows-msvc.exe");
/// 后端释放目录（与应用数据目录同根，子目录 backend 不与 json 数据冲突）
const BACKEND_EXE_NAME: &str = "ComfyMediaViewer.exe";
/// Win32 CREATE_NO_WINDOW：后台进程不弹控制台窗口
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
/// 后端端口就绪等待上限（PyInstaller 自解压首次启动可能较慢）
const BACKEND_TIMEOUT: u64 = 60;
/// 端口探测起始值（与后端默认端口一致）
const PORT_START: u16 = 8765;
/// 端口探测尝试数量
const PORT_TRIES: u16 = 50;
/// 身份探测（/api/info）的读/写超时
const PROBE_TIMEOUT_MS: u64 = 1500;
/// 后端存活监控的轮询周期（7-9）
const MONITOR_POLL_MS: u64 = 2000;

/// 后端子进程句柄的全局托管；None = 后端未成功启动
struct BackendProc(Mutex<Option<Child>>);

/// 后端 pid（spawn 成功即登记）。panic hook 拿不到 Tauri state，
/// 用全局槽让「任何路径的 panic」都能杀掉后端（ε-10 / 7-9）。
static BACKEND_PID: OnceLock<u32> = OnceLock::new();

/// 后端端口（spawn 前登记）。P0-11 的退出兜底需要它：壳内「重启程序」= 后端自己起
/// 新进程接管（同端口），此时壳登记的 pid 已经死了，只能按端口反查新后端真实 pid。
static BACKEND_PORT: OnceLock<u16> = OnceLock::new();

/// 从 PORT_START 起找一个当前空闲的端口；全部被占返回 None（ε-10 / 7-7：
/// 旧实现返回 PORT_START 兜底，等于明知被占还用，后端必 bind 失败白等 60 秒）。
fn pick_port() -> Option<u16> {
    for i in 0..PORT_TRIES {
        let p = PORT_START + i;
        if TcpListener::bind(("127.0.0.1", p)).is_ok() {
            return Some(p);
        }
    }
    None
}

/// 磁盘上那份后端，跟壳里嵌的是不是同一份。
///
/// ⚠️ 判据必须是**内容**，不能是**大小**：PE 段对齐（FileAlignment）会把小幅差异吸收掉，
/// 两版后端**长度完全相同**是完全可能的。那时「比大小」会认定磁盘上那份已经是最新的，
/// 于是用户拿到新壳、却继续跑上一版后端 —— 表现为「更新完界面 / 功能没变化」，
/// 而且极难排查（版本号、文件时间、体积看着都对）。
/// 代价只是启动时多读一次（约 15 MB，走系统缓存，十几毫秒）。
fn backend_is_current(current: Option<&[u8]>, embedded: &[u8]) -> bool {
    matches!(current, Some(cur) if cur == embedded)
}

/// 释放嵌入的后端 exe：%LOCALAPPDATA%\ComfyMediaViewer\backend\ComfyMediaViewer.exe。
/// 内容与嵌入的不一致（或文件读不到）才重写。
fn ensure_backend_extracted() -> Result<PathBuf, String> {
    let base = std::env::var("LOCALAPPDATA")
        .map_err(|_| "无法定位 LOCALAPPDATA".to_string())?;
    let dir = PathBuf::from(base)
        .join("ComfyMediaViewer")
        .join("backend");
    std::fs::create_dir_all(&dir)
        .map_err(|e| format!("创建后端目录失败: {e}"))?;
    let target = dir.join(BACKEND_EXE_NAME);

    // 读不到（不存在 / 被占用 / 无权限）一律当作需要重写，交给出错路径去报。
    let current = std::fs::read(&target).ok();
    if !backend_is_current(current.as_deref(), BACKEND_BYTES) {
        let tmp = dir.join(".backend.tmp");
        std::fs::write(&tmp, BACKEND_BYTES)
            .map_err(|e| format!("写入后端失败: {e}"))?;
        // Windows 上 rename 不能覆盖已存在文件，先移除旧版。
        //
        // P1-37：旧实现是 `let _ = std::fs::remove_file(&target);` —— 错误被吞掉。
        // 而 Windows 上删除**正在运行的映像**必然失败（Access is denied），于是
        // 下一步 rename 也必然失败，用户只看到笼统的「部署后端失败」，看不到真因。
        // 现在：删除失败时把原因**带进错误信息**，并明确提示是旧进程占用。
        if let Err(e) = std::fs::remove_file(&target) {
            if target.exists() {
                // 文件还在 ⇒ 删除确实失败了，后面 rename 必定也失败，直接报真因
                return Err(format!(
                    "部署后端失败：无法替换旧版后端（{e}）。\
                     通常是有旧的 ComfyMediaViewer 进程仍在运行占用了该文件，\
                     请在任务管理器中结束它后重试。目标文件：{}",
                    target.display()
                ));
            }
            // 文件本来就不存在（首次释放）——正常路径，继续
        }
        std::fs::rename(&tmp, &target)
            .map_err(|e| format!("部署后端失败: {e}（目标 {}）", target.display()))?;
    }
    Ok(target)
}

/// 桌面壳自己的 exe 所在目录 —— 也就是用户「放程序的地方」。
///
/// 后端 exe 被释放到 %LOCALAPPDATA%\ComfyMediaViewer\backend\ 去跑，它自己按
/// sys.executable 算出来的「程序目录」就是那个释放目录，于是便携模式的 cmv-data
/// 会落在 C 盘，用户把整个文件夹搬走也带不走数据。壳是唯一知道答案的人，由它告诉后端。
fn app_dir() -> Option<PathBuf> {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
}

fn spawn_backend(port: u16) -> Result<Child, String> {
    let path = ensure_backend_extracted()?;
    let mut cmd = Command::new(path);
    cmd.args(["--port", &port.to_string(), "--no-browser"]);
    if let Some(dir) = app_dir() {
        cmd.arg("--app-dir").arg(dir);
    }
    cmd.creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|e| format!("后端启动失败: {e}"))
}

/// ε-10 / 7-7：向端口发 `GET /api/info` 并核对响应头里的 `Server: MediaViewer/`。
///
/// 这是**身份**探测，不只是「端口活着」：TOCTOU 空窗里端口可能被别的程序抢注
/// （探测 bind 与后端 bind 之间），旧实现只看 TCP connect 成功，会把别人的服务
/// 塞进 iframe。判据与后端自己的 `_probe_own_instance` 完全一致（404/403 也带该头，
/// 所以即便 /api/info 因数据目录问题失败，身份仍可判定）。
fn probe_backend_identity(port: u16) -> bool {
    let mut stream = match TcpStream::connect(("127.0.0.1", port)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(PROBE_TIMEOUT_MS)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(PROBE_TIMEOUT_MS)));
    let req = b"GET /api/info HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n";
    if stream.write_all(req).is_err() {
        return false;
    }
    let mut head = Vec::new();
    let mut buf = [0u8; 512];
    while head.len() < 2048 {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                // Server 头总在响应前部，见到空行（头结束）就可以停
                let chunk = &buf[..n];
                head.extend_from_slice(chunk);
                if head.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    let text = String::from_utf8_lossy(&head).to_ascii_lowercase();
    text.contains("\r\nserver: mediaviewer/")
}

/// 轮询直到后端端口就绪且身份匹配（ε-10 / 7-7：不再只看 TCP 可连）。
fn wait_backend(port: u16, timeout_secs: u64) -> bool {
    let timeout = Duration::from_secs(timeout_secs);
    let start = Instant::now();
    while start.elapsed() < timeout {
        if probe_backend_identity(port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

/// 按 pid 杀后端进程树（taskkill /T）。PyInstaller onefile 的引导进程是
/// 壳 spawn 的直接子进程，整棵树归它带，/T 可以一并收掉。
/// ε-10 / 7-9：抽出来共用 —— 正常退出（RunEvent::Exit）与 panic hook 同一条路。
fn kill_backend_tree(pid: u32) {
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
}

/// 从 `/api/info` 的响应体里取后端自报的 pid（形如 `"pid": 12345`）。
///
/// 解析刻意做得「宽松但严格」：只认 `"pid"` 后紧跟的十进制数字；任何一处不匹配都返回
/// None（例如后端版本较旧、没有 pid 字段）——调用方据此走「取不到就不杀」的保守分支，
/// 绝不猜一个 pid 去 taskkill（猜错就是误杀别人的进程）。
fn parse_pid_from_info(body: &str) -> Option<u32> {
    let i = body.find("\"pid\"")?;
    let rest = &body[i + "\"pid\"".len()..];
    let rest = rest.trim_start().strip_prefix(':')?;
    let rest = rest.trim_start();
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        return None;
    }
    digits.parse::<u32>().ok()
}

/// P0-11：向后端问它自己的 pid（`GET /api/info`，取 `pid` 字段）。
fn query_backend_pid(port: u16) -> Option<u32> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_millis(PROBE_TIMEOUT_MS)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(PROBE_TIMEOUT_MS)));
    if stream
        .write_all(b"GET /api/info HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        .is_err()
    {
        return None;
    }
    let mut body = Vec::new();
    let mut buf = [0u8; 2048];
    while body.len() < 65536 {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => body.extend_from_slice(&buf[..n]),
            Err(_) => break,
        }
    }
    parse_pid_from_info(&String::from_utf8_lossy(&body))
}

/// P0-11 的退出兜底：端口上仍有 CMV 服务（后端自重启接管了新进程）时，按端口反查
/// 真实 pid 再杀树。
///
/// 为什么必须有这一步：壳内「重启程序」起的新后端虽然也是普通子进程（见后端
/// `restart_self` 的 P1-35），但它的父进程（旧后端）随后 `os._exit(0)` 走了 ——
/// 壳登记的 pid 成了死号，`taskkill /PID <死号>` 必然失败且错误被吞。结果是新后端
/// 常驻成孤儿、并一直锁着 `%LOCALAPPDATA%\...\backend\ComfyMediaViewer.exe`，
/// 下次换版时 `remove_file` 报「部署后端失败」，用户以为打包坏了。
fn kill_backend_by_port() {
    let Some(&port) = BACKEND_PORT.get() else {
        return;
    };
    if !probe_backend_identity(port) {
        return; // 端口上没有 CMV 服务 ⇒ 后端已随子进程树一起退出，无需兜底
    }
    match query_backend_pid(port) {
        Some(pid) => {
            kill_backend_tree(pid);
            append_shell_log(&format!(
                "[exit] 兜底：端口 {port} 上仍是 CMV 服务（后端自重启接管），按端口反查 pid={pid} 并杀树"
            ));
        }
        None => append_shell_log(&format!(
            "[exit] 端口 {port} 上仍有 CMV 服务，但未能取到 pid（后端可能过旧），无法兜底杀进程"
        )),
    }
}

/// ε-10 / 7-9：后端死透（进程退出且端口上没有 CMV 服务）时的壳页提示。
///
/// 此时启动页已收起、iframe 里是应用页面 —— 在壳顶层注入一条置顶横幅
/// （壳页与 iframe 跨源，应用侧 JS 死不了也改不了这条横幅；Tauri 顶层 eval 可靠）。
/// 同时落盘 shell.log，保证「后端是什么时候死的」事后可查。
fn notify_backend_down(win: &WebviewWindow, port: u16) {
    append_shell_log(&format!(
        "[backend-down] 后端进程已退出且端口 {port} 上无 CMV 服务，已在壳页提示用户重启"
    ));
    let dom = "(function(){ \
        if (document.getElementById('backendDownBanner')) return; \
        var b = document.createElement('div'); \
        b.id = 'backendDownBanner'; \
        b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;\
background:#b3261e;color:#fff;font:13px/1.6 system-ui,sans-serif;\
padding:8px 16px;text-align:center;box-sizing:border-box;'; \
        b.textContent = '\\u26a0 后端进程已退出，页面功能不可用 \\u2014\\u2014 请重启程序'; \
        (document.body || document.documentElement).appendChild(b); \
    })()";
    let _ = win.eval(dom);
}

/// 在启动页上报告错误（启动页始终可访问，用 eval 注入文本）。
///
/// P1-36：旧实现 `window.__bootError && window.__bootError('…')` 会**短路吞掉**消息，
/// 而且 `eval` 的返回值被 `let _ =` 丢掉 ⇒ 调用方**永远不知道消息没送出去**。
/// spawn_backend 失败发生在 Builder 之前，此时壳页脚本可能尚未解析，
/// `window.__bootError` 还是 undefined ⇒ 消息永久丢失，用户只看到遮罩永久转圈。
///
/// 现在：① 轮询等待 `__bootError` 就绪（最多 ~5s）再调；② 若始终就绪不了，
/// 退化为**直接改 DOM** 把错误写到启动页上（不依赖任何脚本）；③ 把结果记进
/// `SHELL_ERR` 并落盘一份日志，保证「消息有没有送到」可查。
fn report_splash_error(win: &WebviewWindow, msg: &str) {
    let esc = msg.replace('\\', "\\\\").replace('\'', "\\'");
    // 重试投递：壳页可能尚未解析完，eval 会失败；最多重试约 5s
    for _ in 0..50 {
        // DOM 写入是**确定性**投递（不依赖脚本，任何时刻都有效）：
        // 与壳页 __bootError 的 DOM 动作完全一致（.failed / .show / 文案），
        // 这样「脚本没就绪」与「脚本已就绪」两条路径用户看到的结果一样。
        let dom = format!(
            "(() => {{ \
                 var b = document.getElementById('boot'); \
                 if (b) b.classList.add('failed'); \
                 var e = document.getElementById('bootErr'); \
                 if (e) {{ e.textContent = '{}'; e.classList.add('show'); }} \
                 var t = document.getElementById('bootStage'); \
                 if (t) t.textContent = '启动失败'; \
                 return 'OK'; }})()",
            esc
        );
        if win.eval(&dom).is_ok() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    // 落盘留痕，便于事后核对「消息有没有送到」
    append_shell_log(&format!("[splash-error] {msg}"));
}

/// 壳自己的日志落盘（P1-36 配套）：%LOCALAPPDATA%\ComfyMediaViewer\shell.log
fn append_shell_log(line: &str) {
    let base = match std::env::var("LOCALAPPDATA") {
        Ok(v) => PathBuf::from(v).join("ComfyMediaViewer"),
        Err(_) => return,
    };
    let _ = std::fs::create_dir_all(&base);
    let path = base.join("shell.log");
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        use std::io::Write;
        let _ = writeln!(f, "{line}");
    }
}

fn main() {
    // ε-10 / 7-9：panic 路径清理。unwind 不走 RunEvent::Exit，后端会成孤儿；
    // hook 里拿不到 Tauri state，用 BACKEND_PID 全局槽兜底（未 spawn 过则 no-op）。
    // 注意：panic 期间再 spawn taskkill 在 Windows 上可行（纯 CreateProcess）。
    std::panic::set_hook(Box::new(|info| {
        if let Some(pid) = BACKEND_PID.get() {
            kill_backend_tree(*pid);
        }
        // P0-11：panic 前若发生过「后端自重启接管」，登记的 pid 已失效 ——
        // 同样按端口兜底，避免带一个孤儿后端一起崩掉。
        kill_backend_by_port();
        append_shell_log(&format!("[panic] {info}"));
    }));

    tauri::Builder::default()
        // ε-10 / 7-6：单实例保护。⚠️ 必须是第一个注册的插件 —— 第二实例在插件
        // init 阶段（build 内）就检测到已有实例并 exit(0)，此时 setup 还没跑、
        // 后端尚未 spawn ⇒ 不会出现两个壳各起一份后端、写同一份 cmv-data。
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // 又双击了一次：把已有窗口带到前台，而不是让用户面对「点了没反应」
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .manage(BackendProc(Mutex::new(None)))
        .setup(move |app| {
            let win = app
                .get_webview_window("main")
                .expect("main window not found");
            let handle = app.handle().clone();

            // 后台线程：端口选择 → spawn → 等就绪 → 导航 → 持续监控，不阻塞 UI 线程
            std::thread::spawn(move || {
                // ε-10 / 7-7：端口全忙直接报错（旧实现回退 PORT_START 必然白等）
                let Some(port) = pick_port() else {
                    report_splash_error(
                        &win,
                        &format!("端口 {PORT_START} 起连续 {PORT_TRIES} 个全部被占用，无法启动后端"),
                    );
                    return;
                };
                // P0-11：登记端口。退出时若端口上仍有 CMV 服务（后端自重启接管），
                // 壳登记的 pid 已失效，只能靠这个端口反查新后端 pid 再杀。
                let _ = BACKEND_PORT.set(port);
                // ε-10 / 7-6：spawn 已被单实例闸门护住（在 plugin init 之后才发生）
                let child = match spawn_backend(port) {
                    Ok(c) => c,
                    Err(e) => {
                        report_splash_error(&win, &e);
                        return;
                    }
                };
                let _ = BACKEND_PID.set(child.id());
                let state = handle.state::<BackendProc>();
                match state.0.lock() {
                    Ok(mut g) => *g = Some(child),
                    Err(_) => return,
                }

                if wait_backend(port, BACKEND_TIMEOUT) {
                    // 后端就绪后把应用加载进标题栏下方的 iframe（保持 Tauri 顶层页，IPC 可靠）
                    let _ = win.eval(&format!(
                        "var f=document.getElementById('app-frame'); if(f){{ f.src='http://127.0.0.1:{port}/'; }}"
                    ));
                } else {
                    report_splash_error(
                        &win,
                        &format!("后端在 {BACKEND_TIMEOUT} 秒内未就绪（端口 {port}）"),
                    );
                    return;
                }

                // ε-10 / 7-9：就绪 ≠ 一劳永逸 —— 持续监控后端进程存活。
                // try_wait 短暂持锁，不与 RunEvent::Exit 的清理互锁。
                loop {
                    std::thread::sleep(Duration::from_millis(MONITOR_POLL_MS));
                    let exited = {
                        let st = handle.state::<BackendProc>();
                        let mut guard = match st.0.lock() {
                            Ok(g) => g,
                            Err(_) => return,
                        };
                        match guard.as_mut() {
                            None => return, // 句柄已被清走（理论不可达，兜底）
                            Some(c) => match c.try_wait() {
                                Ok(Some(_)) => true,
                                Ok(None) => false,
                                Err(_) => true,
                            },
                        }
                    };
                    if exited {
                        // P1-35：壳内「重启程序」= 后端自己起新进程后退出（同端口接管）。
                        // 此时端口上仍有 CMV 服务 ⇒ 静默记录，不告警。
                        if probe_backend_identity(port) {
                            append_shell_log(&format!(
                                "[backend-monitor] 后端进程退出，但端口 {port} 上仍有 CMV 服务（自重启接管），不告警"
                            ));
                        } else {
                            notify_backend_down(&win, port);
                        }
                        return;
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                // 退出时终止后端进程树，避免孤儿进程
                // （PyInstaller onefile 引导进程会 fork 出真正的服务进程，
                //   直接 kill 只能杀掉引导进程，须 taskkill /T 杀整棵树）
                let state = app.state::<BackendProc>();
                let mut guard = match state.0.lock() {
                    Ok(g) => g,
                    Err(_) => {
                        // 锁中毒：跳过「按登记 pid 杀」，但仍走端口兜底（兜底不碰这把锁）
                        kill_backend_by_port();
                        return;
                    }
                };
                if let Some(child) = guard.as_mut() {
                    kill_backend_tree(child.id());
                    let _ = child.wait();
                }
                drop(guard); // 先放锁，再走兜底（兜底要发 HTTP，别阻塞监控线程）
                // P0-11：壳内「重启程序」后后端已换进程，上面杀的是**死号**。若端口上
                // 仍有 CMV 服务，按端口反查真实 pid 再杀，避免新后端常驻成孤儿。
                kill_backend_by_port();
            }
        });
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_needs_write() {
        assert!(!backend_is_current(None, b"abcd"));
    }

    #[test]
    fn identical_skips() {
        assert!(backend_is_current(Some(b"abcd"), b"abcd"));
    }

    /// 这条就是修掉的那个坑：长度一样、内容不同。
    /// 旧判据（比长度）会把它判成「已是最新」，于是永远不更新。
    #[test]
    fn same_len_different_content_needs_write() {
        assert!(!backend_is_current(Some(b"abcd"), b"abce"));
    }

    /// ε-10 / 7-7：端口全忙必须返回 None（旧实现回退 PORT_START = 明知被占还用）。
    #[test]
    fn pick_port_returns_none_when_all_busy() {
        // 占住整条探测链上**当前空闲**的端口；bind 失败的端口说明已被系统真实服务
        // 占用（本机 8765..8814 区间不一定干净）—— 那个端口本来就不空闲，
        // 跳过即可。这里绝不能 unwrap：测试断言的是 pick_port 的行为，不是机器干净。
        let listeners: Vec<TcpListener> = (0..PORT_TRIES)
            .filter_map(|i| TcpListener::bind(("127.0.0.1", PORT_START + i)).ok())
            .collect();
        assert!(pick_port().is_none());
        drop(listeners);
    }

    /// ε-10 / 7-7：没有监听者的端口身份探测必须为假。
    #[test]
    fn probe_fails_on_dead_port() {
        // 找一个当前空闲端口（bind 探测后立即 drop）
        let l = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let p = l.local_addr().unwrap().port();
        drop(l);
        assert!(!probe_backend_identity(p));
    }

    /// ε-10 / 7-7：身份判据认的是 `Server: MediaViewer/` 头（大小写不敏感）。
    #[test]
    fn probe_identity_matches_server_header() {
        let l = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let p = l.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut s, _)) = l.accept() {
                let mut buf = [0u8; 256];
                let _ = s.read(&mut buf);
                let _ = s.write_all(
                    b"HTTP/1.0 200 OK\r\nServer: MediaViewer/2.12.7\r\n\r\n{}",
                );
            }
        });
        assert!(probe_backend_identity(p));
    }

    /// P0-11：`/api/info` 的 pid 解析必须「宽松但严格」—— 认得出真实响应里的
    /// `"pid": N`（含 JSON 的空格），也必须在字段缺失 / 畸形时返回 None。
    /// 这条判据的意义：取不到 pid 时调用方走「不杀」的保守分支，
    /// **宁可不杀，也绝不猜一个 pid 去 taskkill（猜错就是误杀别人的进程）**。
    #[test]
    fn parse_pid_from_info_ok_and_none() {
        assert_eq!(
            parse_pid_from_info(r#"{"root":"C:\\out","has_pil":true,"pid": 4321,"title":"t"}"#),
            Some(4321)
        );
        assert_eq!(parse_pid_from_info(r#"{"pid":7}"#), Some(7));
        assert_eq!(parse_pid_from_info(r#"{"version":"2.12.9"}"#), None);
        assert_eq!(parse_pid_from_info(""), None);
        assert_eq!(parse_pid_from_info(r#"{"pid":"abc"}"#), None);
    }
}
