#!/usr/bin/env bash
# Usage: ./vm.sh start | ssh [cmd...] | status | stop
set -euo pipefail
cd "$(dirname "$0")"

IMG_URL=https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
BASE=noble-server-cloudimg-amd64.img   # read-only base image (downloaded once)
DISK=vm.qcow2                          # VM's own disk, copy-on-write over BASE
SSH_PORT=${SSH_PORT:-2222}

running() { [[ -f vm.pid ]] && kill -0 "$(cat vm.pid)" 2>/dev/null; }
mon()     { printf '%s\n' "$1" | nc -U -N monitor.sock | tr -d '\r' | grep -av '(qemu)\|^QEMU' || true; }
ssh_vm()  { ssh -i id_vm -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                -o LogLevel=ERROR -o ConnectTimeout=5 ubuntu@127.0.0.1 "$@"; }

start() {
  running && { echo "already running (pid $(cat vm.pid))"; return; }
  [[ -f $BASE ]] || { wget -q --show-progress -O "$BASE.part" "$IMG_URL" && mv "$BASE.part" "$BASE"; }
  [[ -f $DISK ]] || qemu-img create -q -f qcow2 -F qcow2 -b "$BASE" "$DISK" 10G
  [[ -f id_vm ]] || ssh-keygen -q -t ed25519 -N '' -C vm -f id_vm

  mkdir -p seed
  printf 'instance-id: vm-1\nlocal-hostname: vm\n' > seed/meta-data
  printf '#cloud-config\nssh_authorized_keys:\n  - %s\n' "$(cat id_vm.pub)" > seed/user-data
  qemu-img convert -O qcow2 'json:{"driver":"vvfat","dir":"seed","label":"cidata"}' seed.qcow2

  qemu-system-x86_64 -name vm -enable-kvm -cpu host -smp 2 -m 2048 \
    -drive file="$DISK",if=virtio \
    -drive file=seed.qcow2,if=virtio,readonly=on \
    -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 \
    -display none -serial file:serial.log \
    -monitor unix:monitor.sock,server=on,wait=off \
    -pidfile vm.pid -daemonize
  echo "started (pid $(cat vm.pid)), waiting for ssh on port $SSH_PORT..."

  for _ in $(seq 60); do ssh_vm true 2>/dev/null && { echo "ready: ./vm.sh ssh"; return; }; sleep 3; done
  echo "ssh not up after 3 min, see serial.log" >&2; return 1
}

stop() {
  running || { echo "not running"; return; }
  for i in $(seq 60); do
    running || { echo "stopped"; return; }
    (( i % 5 == 1 )) && mon system_powerdown >/dev/null
    sleep 1
  done
  mon quit >/dev/null; echo "guest ignored ACPI, killed"
}

case "${1:-}" in
  start)  start ;;
  stop)   stop ;;
  status) if running; then echo "pid $(cat vm.pid)"; mon 'info status'; mon 'info kvm'; else echo "not running"; fi ;;
  ssh)    shift; ssh_vm "$@" ;;
  *)      echo "usage: $0 start|ssh [cmd...]|status|stop" >&2; exit 1 ;;
esac
