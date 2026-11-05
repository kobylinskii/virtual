#!/usr/bin/env python3
"""Minimal VM manager: HTTP API over QEMU/KVM, stdlib only.

State lives in $DATA/state.json, each VM's files (disk, cloud-init seed, pid,
monitor socket, serial log) in $DATA/vms/<name>/.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.environ.get("DATA", "/data"))
LIMIT_CPUS = int(os.environ.get("LIMIT_CPUS", "4"))
LIMIT_MEMORY = int(os.environ.get("LIMIT_MEMORY", "4096"))  # MiB
SSH_PORTS = range(2201, 2221)  # published 1:1 in docker-compose.yml
DEFAULT_IMAGE = {"name": "ubuntu-24.04", "description": "Ubuntu 24.04 LTS cloud image",
                 "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"}
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")  # VM name, also a valid hostname
IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,31}$")

state = {"images": {}, "vms": {}}
lock = threading.Lock()  # ponytail: one lock for every state change, fine for a handful of VMs


class HTTPError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def save():
    tmp = DATA / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(DATA / "state.json")  # atomic: a crash never leaves half-written state


def run(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def vm_dir(name):
    return DATA / "vms" / name


def alive(name):
    try:
        pid = int((vm_dir(name) / "pid").read_text())
        # pids restart from 1 in a new container, so check the process is really this VM's qemu
        return str(vm_dir(name)).encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return False


def monitor(name, cmd):
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(str(vm_dir(name) / "monitor.sock"))
            s.sendall(cmd.encode() + b"\n")
            s.shutdown(socket.SHUT_WR)
            while s.recv(4096):  # QEMU runs the command, then closes on our EOF
                pass
    except OSError:
        pass  # qemu already gone


def start_vm(name):
    vm, d = state["vms"][name], vm_dir(name)
    run("qemu-system-x86_64", "-name", name, "-enable-kvm", "-cpu", "host",
        "-smp", str(vm["cpus"]), "-m", str(vm["memory"]),
        "-drive", f"file={d}/disk.qcow2,if=virtio",
        "-drive", f"file={d}/seed.qcow2,if=virtio,readonly=on",
        "-nic", f"user,model=virtio-net-pci,hostfwd=tcp::{vm['port']}-:22",
        "-display", "none", "-serial", f"file:{d}/serial.log",
        "-monitor", f"unix:{d}/monitor.sock,server=on,wait=off",
        "-pidfile", f"{d}/pid", "-daemonize")


def stop_vm(name, timeout=60):
    """ACPI power button (clean guest shutdown), hard quit if still up after `timeout` s."""
    for i in range(timeout * 2):
        if not alive(name):
            return
        if i % 10 == 0:
            monitor(name, "system_powerdown")  # re-press every 5 s: a press during early boot is lost
        time.sleep(0.5)
    monitor(name, "quit")
    for _ in range(10):
        if not alive(name):
            return
        time.sleep(0.5)


def write_seed(d, name, hostname, ssh_keys, password, user_data):
    """cloud-init NoCloud seed: FAT disk labelled cidata with meta-data + multipart user-data."""
    seed = d / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "meta-data").write_text(f"instance-id: {name}-{time.time_ns()}\nlocal-hostname: {hostname}\n")
    msg = MIMEMultipart()
    cfg = {"ssh_authorized_keys": ssh_keys}
    if password:  # only a SHA-512 hash goes to the seed disk, which the guest can read
        pw_hash = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password,
                                 check=True, capture_output=True, text=True).stdout.strip()
        cfg["ssh_pwauth"] = True
        cfg["chpasswd"] = {"expire": False, "users": [{"name": "ubuntu", "password": pw_hash, "type": "hash"}]}
    msg.attach(MIMEText("#cloud-config\n" + json.dumps(cfg), "cloud-config"))
    if user_data:
        part = MIMEText(user_data)  # text/plain: cloud-init detects #cloud-config / #! by itself
        part["Merge-Type"] = "list(append)+dict(recurse_array)+str()"  # extend our keys, don't replace
        msg.attach(part)
    (seed / "user-data").write_text(msg.as_string())
    # qemu-img's vvfat reads the dir correctly; a live fat: drive gives the guest zeros on QEMU 8.2
    run("qemu-img", "convert", "-O", "qcow2",
        "json:" + json.dumps({"driver": "vvfat", "dir": str(seed), "label": "cidata"}), str(d / "seed.qcow2"))


def view(name):
    vm = state["vms"][name]
    return {"name": name, **vm, "state": "running" if alive(name) else "stopped",
            "ssh": f"ssh -p {vm['port']} ubuntu@localhost"}


def quota():
    vms = state["vms"].values()
    return {"limit_cpus": LIMIT_CPUS, "limit_memory": LIMIT_MEMORY,
            "used_cpus": sum(v["cpus"] for v in vms), "used_memory": sum(v["memory"] for v in vms)}


def create_vm(b):
    name, hostname = b.get("name", ""), b.get("hostname") or b.get("name", "")
    cpus, memory = b.get("cpus", 1), b.get("memory", 1024)
    ssh_keys, password, user_data = b.get("ssh_keys", []), b.get("password", ""), b.get("user_data", "")
    if not NAME.match(name) or not NAME.match(hostname):
        raise HTTPError(400, "name/hostname: 1-32 chars of a-z, 0-9, '-'")
    if type(cpus) is not int or cpus < 1 or type(memory) is not int or memory < 256:
        raise HTTPError(400, "cpus >= 1 and memory >= 256 (MiB) must be integers")
    if not isinstance(ssh_keys, list) or not all(isinstance(k, str) for k in ssh_keys) \
            or not isinstance(user_data, str):
        raise HTTPError(400, "ssh_keys must be a list of strings, user_data a string")
    if not isinstance(password, str) or "\n" in password:
        raise HTTPError(400, "password must be a one-line string")
    with lock:
        if name in state["vms"]:
            raise HTTPError(409, f"VM {name} already exists")
        image = state["images"].get(b.get("image", DEFAULT_IMAGE["name"]))
        if not image:
            raise HTTPError(400, "unknown image, see GET /images")
        q = quota()
        if q["used_cpus"] + cpus > LIMIT_CPUS or q["used_memory"] + memory > LIMIT_MEMORY:
            raise HTTPError(409, f"quota exceeded: {q}, requested cpus={cpus} memory={memory}")
        port = next((p for p in SSH_PORTS if p not in {v["port"] for v in state["vms"].values()}), None)
        if port is None:
            raise HTTPError(409, "no free SSH ports")
        d = vm_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        run("qemu-img", "create", "-q", "-f", "qcow2", "-F", image["format"], "-b", image["path"],
            str(d / "disk.qcow2"), "10G")
        write_seed(d, name, hostname, ssh_keys, password, user_data)
        state["vms"][name] = {"image": b.get("image", DEFAULT_IMAGE["name"]), "cpus": cpus,
                              "memory": memory, "hostname": hostname, "port": port, "status": "running"}
        save()
        start_vm(name)
        return view(name)


def add_image(b):
    name, path, url, desc = b.get("name", ""), b.get("path"), b.get("url"), b.get("description", "")
    if not IMAGE_NAME.match(name):
        raise HTTPError(400, "name: 1-32 chars of a-z, 0-9, '.', '-'")
    if url and not url.startswith(("http://", "https://")):
        raise HTTPError(400, "url must be http(s)")
    if name in state["images"]:
        raise HTTPError(409, f"image {name} already exists")
    path = path or str(DATA / "images" / f"{name}.img")
    if url and not os.path.exists(path):
        urllib.request.urlretrieve(url, path + ".part")
        os.replace(path + ".part", path)
    if not os.path.isfile(path):
        raise HTTPError(400, f"no such file: {path}")
    fmt = json.loads(run("qemu-img", "info", "--output=json", path))["format"]
    with lock:
        state["images"][name] = {"path": path, "url": url, "description": desc, "format": fmt}
        save()
    return {"name": name, **state["images"][name]}


def delete_image(name):
    with lock:
        if name not in state["images"]:
            raise HTTPError(404, f"no image {name}")
        if any(v["image"] == name for v in state["vms"].values()):
            raise HTTPError(409, f"image {name} is used by a VM")
        del state["images"][name]
        save()
    return {"deleted": name}


def vm_action(name, action):
    with lock:
        if name not in state["vms"]:
            raise HTTPError(404, f"no VM {name}")
        if action == "delete":
            del state["vms"][name]
        else:
            state["vms"][name]["status"] = "running" if action == "start" else "stopped"
        save()
        if action == "start" and not alive(name):
            start_vm(name)
    if action == "stop":
        stop_vm(name)
    if action == "delete":
        stop_vm(name, timeout=0)
        shutil.rmtree(vm_dir(name), ignore_errors=True)
        return {"deleted": name}
    return view(name)


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self, method):
        p = self.path.rstrip("/") or "/"
        m = re.fullmatch(r"/(vms|images)/([^/]+)(?:/(start|stop))?", p)
        if method == "GET" and p == "/":
            return 200, (Path(__file__).parent / "index.html").read_bytes()
        if method == "GET" and p == "/vms":
            return 200, [view(n) for n in state["vms"]]
        if method == "GET" and p == "/images":
            return 200, [{"name": n, **i} for n, i in state["images"].items()]
        if method == "GET" and p == "/quota":
            return 200, quota()
        if method == "GET" and m and m[1] == "vms" and not m[3]:
            if m[2] not in state["vms"]:
                raise HTTPError(404, f"no VM {m[2]}")
            return 200, view(m[2])
        if method == "POST" and p in ("/vms", "/images"):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            if not isinstance(body, dict):
                raise HTTPError(400, "body must be a JSON object")
            return 201, (create_vm if p == "/vms" else add_image)(body)
        if method == "POST" and m and m[1] == "vms" and m[3]:
            return 200, vm_action(m[2], m[3])
        if method == "DELETE" and m and not m[3]:
            return 200, vm_action(m[2], "delete") if m[1] == "vms" else delete_image(m[2])
        raise HTTPError(404, f"no route {method} {self.path}")

    def handle_method(self, method):
        try:
            code, body = self.route(method)
            self.reply(code, body, "text/html; charset=utf-8" if isinstance(body, bytes) else "application/json")
        except HTTPError as e:
            self.reply(e.code, {"error": str(e)})
        except json.JSONDecodeError as e:
            self.reply(400, {"error": f"bad JSON: {e}"})
        except subprocess.CalledProcessError as e:
            self.reply(500, {"error": e.stderr.strip()})

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")

    def do_DELETE(self):
        self.handle_method("DELETE")


def shutdown(*_):
    # docker stop/restart: power guests off cleanly; their "status" stays, so they come back on start
    threads = [threading.Thread(target=stop_vm, args=(n, 45)) for n in state["vms"] if alive(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    raise SystemExit(0)


def main():
    (DATA / "images").mkdir(parents=True, exist_ok=True)
    (DATA / "vms").mkdir(exist_ok=True)
    if (DATA / "state.json").exists():
        state.update(json.loads((DATA / "state.json").read_text()))
    if DEFAULT_IMAGE["name"] not in state["images"]:
        # in background: API and UI must answer while ~600 MB downloads
        print("downloading default image...", flush=True)
        threading.Thread(target=lambda: (add_image(DEFAULT_IMAGE), print("default image ready", flush=True)),
                         daemon=True).start()
    for name, vm in state["vms"].items():  # restore: start every VM that should be running
        if vm["status"] == "running" and not alive(name):
            print(f"restoring VM {name}", flush=True)
            try:
                start_vm(name)
            except subprocess.CalledProcessError as e:
                print(f"failed to restore {name}: {e.stderr}", flush=True)
    signal.signal(signal.SIGTERM, shutdown)
    print("listening on :8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
