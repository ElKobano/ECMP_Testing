# ECMP Source‑IP Hashing Test Suite

Набор функциональных тестов для проверки ECMP‑маршрутизации с хешированием
по Source IP. Тесты запускаются внутри контейнера `stepanenko/mgmt_container`,
который через проброшенный `docker.sock` разворачивает топологию из шести
контейнеров и прогоняет pytest.

## Топология

```
client (генератор трафика)
   │  203.0.113.0/24
   │
 [ dut ]  — FRR-роутер под тестом (staticd / ospfd / bgpd)
   │
   ├── 10.0.1.0/24 ── r1  (FRR + захват трафика)
   ├── 10.0.2.0/24 ── r2
   ├── 10.0.3.0/24 ── r3
   └── 10.0.4.0/24 ── r4
```

Тестовый префикс: `198.51.100.0/24` (IPv4), `2001:db8::/64` (IPv6).
Хеш-политика: sysctl `fib_multipath_hash_policy` / `fib_multipath_hash_fields`.

## Запуск (Ubuntu 24.04)

### Системные требования

- **ОС:** Ubuntu 24.04
- **Ядро Linux:** >= 4.13 (необходимо для `fib_multipath_hash_fields`)
- **Docker:** установленный и запущенный демон
- **Интернет:** требуется для `docker pull networkop/cx:5.3.0`

### Установка Docker

```bash
# Установка Docker Engine
sudo apt update && sudo apt install -y docker.io

# Добавить текущего пользователя в группу docker (чтобы не использовать sudo)
sudo usermod -aG docker $USER
# Перезайти в сессию или выполнить:
newgrp docker

# Проверить, что Docker работает
docker info > /dev/null 2>&1 || echo "Docker не запущен"
```

### Запуск тестов

```bash
./run_tests.sh
```

### Ограничения

- Тестовые контейнеры запускаются в **privileged**-режиме (требуется для
  управления сетевыми интерфейсами, настройки sysctl, raw-сокетов).
- Управляющий контейнер монтирует `/var/run/docker.sock` хоста для
  создания и управления тестовой топологией — это даёт контейнеру полный
  доступ к Docker-демону.
- Тестовая сеть полностью изолирована (`internal: true`), трафик не выходит
  за пределы Docker-сетей.
- **IPv6 должен быть включён** на хосте (по умолчанию в Ubuntu 24.04 включён).

Скрипт проверяет наличие образов с префиксом `stepanenko` в локальном Docker,
при необходимости достраивает недостающие, запускает контейнер `mgmt` и внутри
него выполняет `pytest -v -ra --junitxml=...`. После завершения контейнер
автоматически удаляется.

**Результаты** сохраняются в:
- `reports/<YYYYmmdd_HHMMSS>/junit.xml` — JUnit-отчёт (xUnit2, совместим с CI)
- `allure-results/<YYYYmmdd_HHMMSS>/` — сырые результаты Allure (JSON, для загрузки в
  Allure TestOps / ReportPortal или генерации локального HTML-отчёта)
- `artifacts/<YYYYmmdd_HHMMSS>/` — для каждого упавшего теста: конфигурация
  FRR, таблица маршрутизации, параметры хеширования, логи FRR и JSON-статистика
  по каждому sink-узлу

**Просмотр Allure-отчёта на хосте** (требуется `allure` CLI; Ubuntu: `sudo apt install allure`):

```bash
allure serve allure-results/<YYYYmmdd_HHMMSS>/
# или сгенерировать статический HTML:
allure generate allure-results/<YYYYmmdd_HHMMSS>/ -o allure-report
```

**Загрузка в Allure TestOps** (при наличии развёрнутого сервера):

```bash
ALLURE_ENDPOINT=https://allure.example.com ALLURE_PROJECT_ID=1 \
  allurectl upload allure-results/<YYYYmmdd_HHMMSS>/
```

**Запуск отдельной группы тестов** по маркерам pytest:

```bash
cd tests
python3 -m pytest -m routes        # только установка маршрутов
python3 -m pytest -m "not e2e"     # исключить end-to-end сценарии
```

**Запуск одного файла напрямую** (для отладки; на хосте потребуются
`pytest`, `docker-py`, `pyyaml`, т.к. тесты управляют Docker-демоном):

```bash
cd tests
python3 -m pytest test_01_routes.py -v
```

## Сценарии

### 1. Установка ECMP-маршрутов — `test_01_routes.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| ECMP-001  | Два equal‑cost статических пути — оба в FIB, оба форвардят         | `test_ecmp_001_two_equal_paths`       |
| ECMP-002  | Четыре равнозначных пути — все в FIB, все используются             | `test_ecmp_002_n_equal_paths`         |
| ECMP-003  | Разная admin‑distance: лучший путь в FIB, худший — в RIB (резерв)  | `test_ecmp_003_different_metric_single_fib` |
| ECMP-004  | OSPF: два соседа с одинаковой стоимостью → оба в FIB               | `test_ecmp_004_ospf_equal_cost`       |
| ECMP-005  | eBGP: два пира с равной AS‑path length → multipath, оба в FIB      | `test_ecmp_005_bgp_multipath`         |

### 2. Детерминированность хеширования — `test_02_hashing.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| HASH-001  | 100 пакетов с одного source IP, три прохода → все на одном пути    | `test_hash_001_same_source_same_path` |
| HASH-002  | После `clear ip route *` тот же source → тот же путь               | `test_hash_002_stable_after_route_clear` |
| HASH-003  | Source = IP интерфейса DUT — форвард без зацикливания              | `test_hash_003_source_equals_router_interface` |

### 3. Распределение по Source IP (IPv4) — `test_03_distribution.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| DIST-001  | 256 source IP → каждый на один egress-интерфейс, без пересечений   | `test_dist_001_each_source_one_interface` |
| DIST-002  | Смена порядка next-hop-ов не меняет группировку source-ов          | `test_dist_002_stable_under_nexthop_reorder` |
| DIST-003  | 1 000 source IP, 4 пути: каждый 15–35 %                            | `test_dist_003_four_paths_even`       |
| DIST-004  | 2 пути: ни один не получает >60 % при 1 000 source IP              | `test_dist_004_two_paths_not_skewed`  |

### 4. Распределение — IPv6 — `test_03_distribution.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| DIST-005  | IPv6 ECMP: один source → один path                                 | `test_dist_005_ipv6_single_source_single_path` |
| DIST-006  | IPv6, 4 пути, 400 source IP → равномерное распределение            | `test_dist_006_ipv6_four_paths_even`  |

### 5. Динамика путей — `test_04_dynamic.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| DYN-001   | Выключение интерфейса активного пути → трафик на другой             | `test_dyn_001_link_down_shifts_traffic` |
| DYN-002   | Восстановление интерфейса → один путь, без дублирования             | `test_dyn_002_link_restore_no_duplication` |
| DYN-003   | Добавление третьего equal‑cost пути → новый путь получает трафик   | `test_dyn_003_add_third_path`         |
| DYN-004   | Удаление трёх из четырёх путей → без потерь, один путь             | `test_dyn_004_remove_all_but_one`     |
| DYN-005   | Повышение admin‑distance пути → исключение из ECMP-группы          | `test_dyn_005_raise_metric_removes_path` |
| DYN-006   | Отвал BGP-соседа → его маршрут удалён из FIB                       | `test_dyn_006_bgp_neighbor_down`      |

### 6. Комбинация полей хеша — `test_06_config.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| COMBO-001 | Source‑IP only: разные dst/порты → один путь                       | `test_combo_001_source_only_ignores_dst_and_ports` |
| COMBO-002 | Source‑IP vs src‑dst‑IP: смена политики меняет набор путей          | `test_combo_002_source_only_vs_5tuple` |

### 7. Негативные сценарии — `test_05_negative.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| NEG-001   | Blackhole-маршрут → трафик дропается                               | `test_neg_001_no_route_is_dropped`    |
| NEG-002   | Некорректные source IP — роутер не падает                          | `test_neg_002_invalid_source_no_crash` |
| NEG-003   | Source = Destination → без зацикливания                            | `test_neg_003_source_equals_destination_no_loop` |
| NEG-004   | Рекурсивный next‑hop через ECMP — рекурсия разрешается             | `test_neg_004_recursive_nexthop`      |
| NEG-005   | Асимметричный MTU: oversize DF пакет дропается                     | `test_neg_005_asymmetric_mtu`         |

### 8. Конфигурация — `test_06_config.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| CFG-001   | Режим source‑ip‑only: path зависит только от source                | `test_cfg_001_source_ip_only`         |
| CFG-002   | Переключение хеш-алгоритма на лету                                 | `test_cfg_002_switch_algorithm_live`  |
| CFG-003   | `write memory` + перезапуск FRR → конфигурация восстанавливается   | `test_cfg_003_persist_across_reboot`  |

### 9. End‑to‑end — `test_07_e2e.py`

| ID        | Описание                                                           | Функция                               |
|-----------|--------------------------------------------------------------------|---------------------------------------|
| E2E-001   | Одна TCP‑сессия (5‑tuple) → все пакеты через один путь             | `test_e2e_001_tcp_session_single_path` |
| E2E-002   | Клиенты за NAT (один публичный IP) → весь трафик через один путь   | `test_e2e_002_nat_single_public_source` |
| E2E-003   | 802.1Q VLAN не влияет на хеш                                       | `test_e2e_003_vlan_does_not_affect_hash` |
