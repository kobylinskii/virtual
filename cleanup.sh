#!/usr/bin/env bash
# Removes everything the solution leaves on the host: VMs, disks, images, network, container image.
cd "$(dirname "$0")"
docker compose down --volumes --rmi local --remove-orphans   # container, its VMs, volume with disks/images, network
./vm.sh stop                                                 # base task VM (vm.sh), if running
rm -rf noble-server-cloudimg-amd64.img* vm.qcow2 seed seed.qcow2 id_vm id_vm.pub serial.log monitor.sock vm.pid
echo "cleaned"
