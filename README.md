# QEMU/KVM без GUI

Нужно: Linux с KVM (доступ к `/dev/kvm`), `qemu-system-x86 qemu-utils`, `docker compose`, `curl`, `ssh`.

## Реализовано

- Базовое задание — `vm.sh`
- 1 — HTTP API (`app.py`)
- 2 — cloud-init: hostname, ssh_keys, password, user_data, cpus, memory
- 4 — API и ВМ внутри контейнера, `/dev/kvm` хоста
- 6 — восстановление ВМ после рестарта контейнера
- 7 — регистрация образов (name, path/url, description)
- 9 — квоты на суммарные CPU/RAM (`LIMIT_CPUS`, `LIMIT_MEMORY` в `docker-compose.yml`)
- 10 — веб-интерфейс: http://127.0.0.1:8080/
- 11 — `check.sh`
- 12 — `cleanup.sh`

## Базовое задание

`vm.sh` качает Ubuntu 24.04 cloud image, делает qcow2-диск поверх него, собирает seed для cloud-init с SSH-ключом и запускает `qemu-system-x86_64 -enable-kvm -display none -daemonize`. SSH проброшен на порт 2222, управление идёт через монитор QEMU.

```bash
./vm.sh start
./vm.sh ssh systemd-detect-virt   # kvm
./vm.sh status                    # VM status: running, kvm support: enabled
./vm.sh stop                      # stopped
```

## Доп. задания

Запуск:

```bash
docker compose up -d --build   # UI отвечает сразу, образ Ubuntu при первом запуске качается в фоне ~1 мин
```

Открыть http://127.0.0.1:8080/ и создать ВМ в форме. То же через API:

```bash
curl -X POST 127.0.0.1:8080/vms -d "{\"name\":\"demo\",\"ssh_keys\":[\"$(cat ~/.ssh/id_ed25519.pub)\"]}"
ssh -p 2201 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ubuntu@127.0.0.1   # порт из поля "port"
curl -X DELETE 127.0.0.1:8080/vms/demo
docker compose down
```

Проверка и очистка:

```bash
./check.sh     # поднимает контейнер и проверяет все пункты, в конце: 21 passed, 0 failed
./cleanup.sh   # удаляет контейнер, ВМ, диски, образы, сеть
```

API: `GET/POST /vms`, `GET/DELETE /vms/<name>`, `POST /vms/<name>/start|stop`, `GET/POST /images`, `DELETE /images/<name>`, `GET /quota`.
