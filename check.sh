#!/usr/bin/env bash
# Checks every implemented feature end to end. Usage: ./check.sh   Cleanup afterwards: ./cleanup.sh
set -uo pipefail
cd "$(dirname "$0")"

API=http://127.0.0.1:8080
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0; PORT=

check()  { if "${@:2}" >/dev/null 2>&1; then echo "PASS  $1"; ((pass++)); else echo "FAIL  $1"; ((fail++)); fi; }
api()    { curl -s -X "$1" -H 'Content-Type: application/json' -d "${3:-}" "$API$2"; }
code()   { curl -s -o /dev/null -w '%{http_code}' -X "$1" -H 'Content-Type: application/json' -d "${3:-}" "$API$2"; }
has()    { api GET "$1" | grep -q "$2"; }
vm_ssh() { ssh -i "$TMP/id" -p "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
               -o LogLevel=ERROR -o ConnectTimeout=5 ubuntu@127.0.0.1 "$@"; }
is()     { [[ "$(vm_ssh "$1")" == "$2" ]]; }
pw_login() { SSH_ASKPASS="$TMP/askpass" SSH_ASKPASS_REQUIRE=force ssh -p "$PORT" -o StrictHostKeyChecking=no \
               -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PubkeyAuthentication=no \
               ubuntu@127.0.0.1 hostname | grep -qx check-host; }
retry()  { for _ in $(seq 60); do "$@" >/dev/null 2>&1 && return; sleep 3; done; return 1; }  # up to 3 min

echo "== starting container (first run builds it and downloads Ubuntu image, a few minutes)"
docker compose up -d --build || exit 1
retry has /images '"name": "ubuntu-24.04"' || { echo "API or default image not ready"; docker compose logs --tail 30; exit 1; }
api DELETE /vms/check-vm >/dev/null; api DELETE /images/check-img >/dev/null   # leftovers of a previous run
ssh-keygen -q -t ed25519 -N '' -f "$TMP/id"
printf '#!/bin/sh\necho check-pass-123\n' > "$TMP/askpass"; chmod +x "$TMP/askpass"   # feeds password to ssh

echo "== 7. images"
check "7   register image (name, path, description)" \
  test "$(code POST /images '{"name":"check-img","path":"/data/images/ubuntu-24.04.img","description":"copy for check"}')" = 201
check "7   image is listed" has /images '"description": "copy for check"'

echo "== 1, 2, 4. create VM with cloud-init, running inside the container"
BODY=$(printf '{"name":"check-vm","image":"check-img","cpus":1,"memory":1024,"hostname":"check-host","ssh_keys":["%s"],"password":"check-pass-123","user_data":"#cloud-config\\nwrite_files:\\n- path: /etc/check\\n  content: hello from user_data\\n"}' "$(cat "$TMP/id.pub")")
check "1   POST /vms creates VM" test "$(code POST /vms "$BODY")" = 201
PORT=$(api GET /vms/check-vm | grep -o '"port": [0-9]*' | grep -o '[0-9]*$')
check "1   VM boots, SSH with key from API works" retry vm_ssh true
check "2   hostname from API" is hostname check-host
check "2   user_data applied" is 'cat /etc/check' 'hello from user_data'
check "2   login by password" retry pw_login
check "4   guest runs on KVM" is systemd-detect-virt kvm
check "4   qemu process lives in the container" sh -c 'docker compose top | grep -q qemu-system'

echo "== 9. quota"
check "9   VM over CPU limit is rejected (409)" test "$(code POST /vms '{"name":"too-big","cpus":4,"memory":512}')" = 409
check "9   VM over RAM limit is rejected (409)" test "$(code POST /vms '{"name":"too-big","cpus":1,"memory":4096}')" = 409

echo "== 1. stop / start"
check "1   stop" sh -c "curl -s -X POST $API/vms/check-vm/stop | grep -q '\"state\": \"stopped\"'"
check "1   start" sh -c "curl -s -X POST $API/vms/check-vm/start | grep -q '\"state\": \"running\"'"
check "1   VM is reachable after start" retry vm_ssh 'echo persisted > marker'

echo "== 6. state restored after container restart"
docker compose restart >/dev/null 2>&1
check "6   API back" retry curl -sf "$API/vms"
check "6   VM running again" retry has /vms/check-vm '"state": "running"'
check "6   VM disk kept data" retry is 'cat marker' persisted

echo "== 10. web UI"
check "10  web UI is served at /" has / '<title>VM manager</title>'

echo "== 1, 7. delete"
check "1   DELETE /vms/check-vm" test "$(code DELETE /vms/check-vm)" = 200
check "1   VM is gone" test "$(code GET /vms/check-vm)" = 404
check "7   DELETE /images/check-img" test "$(code DELETE /images/check-img)" = 200

echo "== $pass passed, $fail failed. Remove everything: ./cleanup.sh"
exit $((fail > 0))
