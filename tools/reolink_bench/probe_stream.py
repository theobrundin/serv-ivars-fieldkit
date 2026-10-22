#!/usr/bin/env python3
"""Bounded RTSP decode proof on servTest; no inference or recordings.

Set credentials with --credentials (interactive hidden password prompt).
Then run without arguments to probe and decode both streams for ten seconds.
Only sanitized metadata is printed and retained in a mode-0600 report.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import resource
import signal
import socket
import stat
import subprocess
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
SECRET = ROOT / "camera-credentials.json"


def save_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(data, handle, indent=2)
        handle.write("\n")


def run_bounded(args, seconds):
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "returncode": process.returncode, "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - started, 3),
        "cpu_seconds": round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 3),
        "stdout": stdout, "stderr": stderr,
    }


def main():
    if socket.gethostname().lower() != "servtest":
        raise SystemExit("Refusing: expected the home bench host servTest")
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", action="store_true")
    args = parser.parse_args()
    if args.credentials:
        username = input("Camera username [admin]: ").strip() or "admin"
        password = getpass.getpass("Camera password: ")
        save_private(SECRET, {"username": username, "password": password})
        print("Camera credentials stored privately on this Pi.")
        return
    mode = stat.S_IMODE(SECRET.stat().st_mode)
    if mode & 0o077:
        raise SystemExit("Credential file must not be accessible by group/others")
    credentials = json.loads(SECRET.read_text())
    username, password = credentials["username"], credentials["password"]
    report = {"host": socket.gethostname(), "camera_ip": "192.168.50.2",
              "transport": "tcp", "recording": False, "inference": False,
              "streams": [], "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for stream in ("sub", "main"):
        path = f"/Preview_01_{stream}"
        url = f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}@192.168.50.2:554{path}"
        def sanitized(message):
            message = message.replace(url, f"rtsp://[redacted]@192.168.50.2:554{path}")
            for secret in sorted({password, quote(password, safe='')}, key=len, reverse=True):
                if secret:
                    message = message.replace(secret, "[redacted]")
            return message[:1200]
        common = ["-rtsp_transport", "tcp", "-timeout", "5000000", "-allowed_media_types", "video"]
        probe = run_bounded(["ffprobe", "-v", "error", *common,
                             "-select_streams", "v:0", "-show_entries",
                             "stream=codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate",
                             "-of", "json", url], 20)
        result = {"stream": stream, "path": path,
                  "probe_returncode": probe["returncode"], "probe_timed_out": probe["timed_out"]}
        if probe["returncode"] == 0:
            result["metadata"] = json.loads(probe["stdout"]).get("streams", [])
            decode = run_bounded(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                                  *common, "-threads", "2", "-i", url, "-map", "0:v:0",
                                  "-an", "-sn", "-dn", "-t", "10", "-fps_mode", "passthrough",
                                  "-enc_time_base", "-1",
                                  "-progress", "pipe:1", "-nostats", "-f", "null", "-"], 30)
            progress = {}
            for line in decode["stdout"].splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key in ("frame", "out_time_us", "speed", "progress", "fps"):
                        progress[key] = value.strip()
            result["decode"] = {key: value for key, value in decode.items() if key not in ("stdout", "stderr")}
            result["decode"]["progress"] = progress
            result["decode"]["sanitized_error"] = sanitized(decode["stderr"])
        else:
            result["sanitized_error"] = sanitized(probe["stderr"])
        report["streams"].append(result)
        save_private(ROOT / "stream-probe.json", report)
        print(json.dumps(result, indent=2), flush=True)
        if probe["returncode"] != 0 and "401" in probe["stderr"]:
            print("Authentication rejected; stopping without retrying credentials.")
            break
    for command in ("measure_temp", "get_throttled"):
        health = run_bounded(["vcgencmd", command], 3)
        report[command] = health["stdout"].strip()
    save_private(ROOT / "stream-probe.json", report)
    print("Saved sanitized report to " + str(ROOT / "stream-probe.json"))


if __name__ == "__main__":
    main()
