//! One request per connection. Socket filesystem permissions are the access policy.
//! No shell, arbitrary paths, environment inheritance, or third-party crates.
use std::fs::{self, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const SOCKET: &str = "/var/run/cairn/control.sock";
const MAX_REQUEST: u64 = 4096;

extern "C" {
    fn flock(fd: std::os::raw::c_int, operation: std::os::raw::c_int) -> std::os::raw::c_int;
}

fn hex_decode(s: &str) -> Result<String, String> {
    if s.len() & 1 != 0 || s.len() > 2048 {
        return Err("invalid field".into());
    }
    let bytes: Result<Vec<u8>, _> = (0..s.len())
        .step_by(2)
        .map(|i| {
            s.get(i..i + 2)
                .ok_or(())
                .and_then(|v| u8::from_str_radix(v, 16).map_err(|_| ()))
        })
        .collect();
    let text = String::from_utf8(bytes.map_err(|_| "invalid hex")?).map_err(|_| "invalid UTF-8")?;
    if text.contains('\0') || text.contains('\n') || text.contains('\r') {
        return Err("invalid field".into());
    }
    Ok(text)
}
fn hex_encode(s: &str) -> String {
    s.as_bytes().iter().map(|v| format!("{v:02x}")).collect()
}

fn interface(s: &str) -> Result<(), String> {
    if s.is_empty()
        || s.len() > 15
        || !s.bytes().all(|b| b.is_ascii_alphanumeric())
        || !s.as_bytes()[0].is_ascii_alphabetic()
        || !s.as_bytes().last().unwrap().is_ascii_digit()
    {
        return Err("invalid interface".into());
    }
    Ok(())
}

// Build only fixed commands, then run them separately. This function is testable on Linux.
fn commands(line: &str) -> Result<Vec<Vec<String>>, String> {
    let fields: Vec<&str> = line.trim_end_matches('\n').split('\t').collect();
    let values: Result<Vec<_>, _> = fields.iter().skip(1).map(|s| hex_decode(s)).collect();
    let v = values?;
    let cmd = |parts: &[&str]| parts.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
    match (fields[0], v.len()) {
        ("ping", 0) => Ok(vec![]),
        ("brightness-get", 0) => Ok(vec![cmd(&["/sbin/wsconsctl", "-n", "display.brightness"])]),
        ("brightness", 1) => {
            let value = v[0].parse::<u8>().map_err(|_| "invalid brightness")?;
            if !(1..=100).contains(&value) {
                return Err("brightness must be 1..100".into());
            }
            Ok(vec![cmd(&[
                "/sbin/wsconsctl",
                &format!("display.brightness={value}"),
            ])])
        }
        ("wifi", 2) => {
            interface(&v[0])?;
            if v[1] != "up" && v[1] != "down" {
                return Err("invalid Wi-Fi state".into());
            }
            Ok(vec![cmd(&["/sbin/ifconfig", &v[0], &v[1]])])
        }
        ("scan", 1) => {
            interface(&v[0])?;
            Ok(vec![cmd(&["/sbin/ifconfig", &v[0], "scan"])])
        }
        ("connect", 3) => {
            interface(&v[0])?;
            if v[1].is_empty() || v[1].len() > 32 {
                return Err("SSID must be 1..32 bytes".into());
            }
            let key = &v[2];
            if !key.is_empty()
                && !(8..=63).contains(&key.len())
                && !(key.len() == 64 && key.bytes().all(|b| b.is_ascii_hexdigit()))
            {
                return Err("WPA key must be 8..63 bytes or 64 hex digits".into());
            }
            let mut set = cmd(&["/sbin/ifconfig", &v[0], "nwid", &v[1]]);
            if key.is_empty() {
                set.push("-wpakey".into());
            } else {
                set.extend(["wpakey".into(), key.clone()]);
            }
            Ok(vec![
                set,
                cmd(&["/sbin/ifconfig", &v[0], "inet", "autoconf"]),
                cmd(&["/sbin/ifconfig", &v[0], "up"]),
            ])
        }
        ("suspend", 0) => Ok(vec![cmd(&["/usr/sbin/zzz"])]),
        ("power", 1) => {
            let flag = match v[0].as_str() {
                "auto" => "-A",
                "low" => "-L",
                "high" => "-H",
                _ => return Err("invalid power mode".into()),
            };
            Ok(vec![cmd(&["/usr/sbin/apm", flag])])
        }
        _ => Err("unknown operation or wrong field count".into()),
    }
}

fn execute(args: &[String]) -> Result<String, String> {
    // A file avoids pipe-buffer deadlocks during scans. Unlink immediately: no
    // scan results or credentials remain on disk, and output never fills RAM.
    let path = format!("/var/run/cairn/output-{}", std::process::id());
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .map_err(|_| "cannot create command output")?;
    fs::remove_file(&path).map_err(|_| "cannot unlink command output")?;
    let mut child = Command::new(&args[0])
        .args(&args[1..])
        .env_clear()
        .env("PATH", "/bin:/sbin:/usr/bin:/usr/sbin")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stdout(file.try_clone().map_err(|_| "output setup failed")?)
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "system command unavailable")?;
    let started = Instant::now();
    loop {
        match child.try_wait().map_err(|_| "command status failed")? {
            Some(status) => {
                if !status.success() {
                    return Err("operation failed; check hardware and configuration".into());
                }
                break;
            }
            None if started.elapsed() > Duration::from_secs(10) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("system command timed out".into());
            }
            None => thread::sleep(Duration::from_millis(20)),
        }
    }
    use std::io::{Seek, SeekFrom};
    let mut file = file;
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "output read failed")?;
    let mut output = String::new();
    file.take(65536)
        .read_to_string(&mut output)
        .map_err(|_| "invalid command output")?;
    Ok(output)
}

fn serve(mut stream: UnixStream) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    stream.set_write_timeout(Some(Duration::from_secs(3)))?;
    let mut line = String::new();
    let result = (|| -> Result<String, String> {
        let mut reader = BufReader::new((&stream).take(MAX_REQUEST));
        reader.read_line(&mut line).map_err(|_| "invalid request")?;
        if !line.ends_with('\n') || line.len() as u64 >= MAX_REQUEST {
            return Err("request too large or incomplete".into());
        }
        let planned = commands(&line)?;
        let mut output = String::new();
        for command in planned {
            output = execute(&command)?;
        }
        Ok(output)
    })();
    match result {
        Ok(output) => writeln!(stream, "OK\t{}", hex_encode(&output)),
        Err(message) => writeln!(stream, "ERR\t{}", hex_encode(&message)),
    }
}

fn main() -> io::Result<()> {
    if !cfg!(target_os = "openbsd") {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "cairnd live service requires OpenBSD",
        ));
    }
    // Root creates this directory once, with group cairn and mode 0750.
    // Group users may connect but cannot replace the socket or lock file.
    let parent = fs::symlink_metadata("/var/run/cairn")?;
    if !parent.is_dir()
        || parent.uid() != 0
        || parent.mode() & 0o022 != 0
        || parent.mode() & 0o007 != 0
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "unsafe /var/run/cairn ownership or mode",
        ));
    }
    // Advisory lock survives as a file and is released by the kernel at exit.
    // This permits rc.d stop/start without a stale-lock recovery step.
    let lock_path = "/var/run/cairn/daemon.lock";
    let lock = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(lock_path)?;
    use std::os::unix::io::AsRawFd;
    // LOCK_EX | LOCK_NB, identical on OpenBSD and Linux.
    if unsafe { flock(lock.as_raw_fd(), 2 | 4) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if let Ok(old) = fs::symlink_metadata(SOCKET) {
        if !old.file_type().is_socket() || old.uid() != 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "unsafe existing socket",
            ));
        }
        fs::remove_file(SOCKET)?;
    }
    let listener = UnixListener::bind(Path::new(SOCKET))?;
    // Directory setgid makes the socket inherit its restricted access group.
    fs::set_permissions(SOCKET, fs::Permissions::from_mode(0o660))?;
    eprintln!("cairnd: listening on {SOCKET}");
    // Sequential, bounded service keeps implementation and privilege surface small.
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let _ = serve(stream);
            }
            Err(e) => eprintln!("cairnd: accept: {e}"),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request(op: &str, fields: &[&str]) -> String {
        format!(
            "{}{}\n",
            op,
            fields
                .iter()
                .map(|s| format!("\t{}", hex_encode(s)))
                .collect::<String>()
        )
    }
    #[test]
    fn rejects_command_injection_and_unknown_operations() {
        assert!(commands(&request("scan", &["iwm0;id"])).is_err());
        assert!(commands(&request("scan", &["-a"])).is_err());
        assert!(commands("exec\t2f62696e2f7368\n").is_err());
        assert!(commands("scan\tzz\n").is_err());
        assert!(commands(&request("scan", &["iwm0\n"])).is_err());
        assert!(hex_decode("가").is_err());
    }
    #[test]
    fn brightness_is_bounded() {
        for v in ["0", "101", "-1", "x"] {
            assert!(commands(&request("brightness", &[v])).is_err());
        }
        assert_eq!(
            commands(&request("brightness", &["72"])).unwrap()[0],
            vec!["/sbin/wsconsctl", "display.brightness=72"]
        );
    }
    #[test]
    fn credentials_stay_in_single_argument() {
        let plan = commands(&request("connect", &["iwm0", "cafe ; wifi", "pa$$ word"])).unwrap();
        assert_eq!(
            plan[0],
            vec![
                "/sbin/ifconfig",
                "iwm0",
                "nwid",
                "cafe ; wifi",
                "wpakey",
                "pa$$ word"
            ]
        );
        assert!(commands(&request("connect", &["iwm0", "cafe", "short"])).is_err());
    }
    #[test]
    fn protocol_roundtrip() {
        let value = "작업실 / cafe";
        assert_eq!(hex_decode(&hex_encode(value)).unwrap(), value);
        assert!(commands(&request("ping", &[])).unwrap().is_empty());
        assert!(commands(&request("suspend", &["extra"])).is_err());
    }

    #[test]
    fn socket_service_frames_success_and_errors() {
        for (input, expected) in [("ping\n", "OK\t\n"), ("exec\n", "ERR\t")] {
            let (mut client, server) = UnixStream::pair().unwrap();
            let worker = thread::spawn(move || serve(server).unwrap());
            client.write_all(input.as_bytes()).unwrap();
            let mut reply = String::new();
            BufReader::new(client).read_line(&mut reply).unwrap();
            assert!(reply.starts_with(expected));
            worker.join().unwrap();
        }
    }
}
