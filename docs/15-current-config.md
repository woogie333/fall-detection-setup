# 현재 설정 기준 — 코드가 무엇을 가정하고 있는가

2026-09 기준. 코드를 만질 때 이 전제들을 먼저 확인하세요.

---

## 1. 센서 노드 (`firmware/sensor_node/sensor_node.ino`)

**전제: 배터리 + 딥슬립 모드.** USB 상시 전원이 아닙니다.

| 설정 | 값 | 의미 |
|---|---|---|
| `DEBUG_MODE` | **0** | 딥슬립 사용. 충격이 와야 깨어남 |
| `USE_BROADCAST` | **0** | 수신기 MAC 으로만 전송 |
| `MONITOR_HZ` | **0** | 실시간 그래프 **꺼짐** (자는 동안 못 보냄) |
| `HEARTBEAT_SEC` | **0** | 주기 전송 꺼짐 |
| `HAVE_BATT_SENSE` | **0** | **분압 저항 미장착.** 잔량이 항상 0 |
| `BATT_REPORT_SEC` | 3600 | 1시간마다 타이머로 깨어나 잔량 보고 |
| `ACT_THRESHOLD` | **3** | 0.19g 이상 흔들려야 기상 (AC 커플링, 중력 제외) |
| `SMOOTH_N` | 4 | 4샘플 이동평균 — 잡음 0.09g → 0.04g |
| `CAPTURE_MS` | 400 | 깨어난 뒤 파형 관측 시간 |
| `ESPNOW_CHANNEL` | 1 | 수신기와 일치해야 함 |
| `DEVICE_ID` | `vib-01` | |
| `RECEIVER_MAC` | `1C:DB:D4:F0:DE:44` | |
| ADXL 레인지 | ±16g, full-res (3.9 mg/LSB) | |
| ADXL ODR | 800Hz (`BW_RATE 0x0D`) | |
| ACT_INACT | `0xF0` | **AC 커플링** — 칩이 중력을 자동으로 뺌 |

**g 값의 의미가 바뀌었습니다.** 지금 보고되는 값은 절대 가속도가 아니라
**정지 기준선을 뺀 변화량**입니다. 가만히 두면 0 근처가 정상입니다.
예전(절대값) 기준으로 임계값을 비교하면 안 됩니다.

**측정 순서** — 깨어나자마자 측정, 그 다음 WiFi 초기화, 그 다음 전송.
순서를 바꾸면 20~50ms 짜리 충격을 통째로 놓칩니다.

### 대시보드 실시간 그래프를 다시 보려면

```cpp
#define DEBUG_MODE 1
#define MONITOR_HZ 5
```

USB 전원이 필요합니다. 배터리로는 원리상 불가능합니다.

---

## 2. 수신기 (`firmware/receiver/receiver.ino`)

USB 로 RDK X3 에 연결. 세 종류의 줄을 시리얼로 내보냅니다.

| kind | 출력 형식 | 언제 |
|---|---|---|
| 0 | `IMPACT device= seq= peak= dur= rssi= batt=` | 충격 발생 |
| 1 | `LEVEL device= peak= rssi=` | 실시간 수준 (MONITOR_HZ 켜져 있을 때) |
| 2 | `BATT device= mv= rssi=` | 배터리 잔량 |

**패킷 구조가 양쪽에서 같아야 합니다.** `kind` 필드를 추가했으므로
한쪽만 업로드하면 크기 불일치로 전부 버려집니다.

---

## 3. 융합 실행 (`scripts/fusion_run.py`)

| 옵션 | 기본값 | 의미 |
|---|---|---|
| `--fusion-mode` | **soft** | 진동이 판정에 개입하는 방식 |
| `--escalate-g` | **0.8** | 이 이상 충격이면 '누움'도 낙상으로 승격 |
| `--impact-min-g` | 0.25 | 이 미만은 충격으로 세지 않음 |
| `--fusion-window` | 10초 | 낙상 전후 이 시간 안의 충격을 같은 사건으로 |
| `--no-impact-delay` | 6초 | 충격 없는 낙상은 이만큼 지켜본 뒤 판단 |
| `--alarm-cooldown` | 180초 | 검증할 때는 15로 줄일 것 |
| `--deghost` | 꺼짐 | 켜야 잔상 제거가 동작 |
| `--deghost-window` | 90초 | 배경 추정 시간 창 |
| `--ffc-interval` | 0 | 이 보드는 FFC 컨트롤이 없어 무의미 |
| `--web-port` | 8090 | |
| `--jpeg-quality` | 80 | |
| `--impact-baud` | 115200 | |
| `--device` | `falldetect` | SmartThings LAN Device Name |
| 백엔드 | `https://cherry-fall.duckdns.org/api/device/data` | `--webhook none` 으로 끔 |

### `--fusion-mode soft` 가 하는 일

| 상황 | 동작 |
|---|---|
| DANGER + 최근 충격 | 즉시 알람 (신뢰도 **높음**) |
| WARNING(누움) + 0.8g 이상 충격 | **낙상으로 승격** → 알람 |
| DANGER + 충격 없음 | 6초 지켜봄 → 계속 쓰러져 있으면 알람(**보통**), 일어나면 취소 |
| SAFE 로 복귀 | 대기 중이던 판정 취소 |

`off` = 예전처럼 참고용, `strict`(=`--require-impact`) = 충격 없으면 알람 없음.

### 잔상 제거(`--deghost`)의 전제

- 최근 90초 프레임의 픽셀별 중앙값을 고정 패턴으로 보고 뺍니다
- **WARNING/DANGER 동안에는 배경 갱신을 멈춥니다** — 안 그러면 쓰러진
  사람이 배경으로 흡수되어 화면에서 지워집니다
- 단, **첫 배경은 상태와 무관하게 만듭니다.** 잔상 때문에 오판이 나면
  상태가 DANGER 에 머물러 배경을 영영 못 만드는 교착에 빠지기 때문
- 시작 후 약 12초, 사람이 없는 상태여야 정확합니다

---

## 4. 친구분 저장소 (`~/fall-detection`)

**브랜치: `experiment-bbox`** 를 써야 합니다.

### ⚠ 2026-09 확인 — 옵션이 바뀌었습니다

| 예전 | 지금 |
|---|---|
| `--fall-min-hold 5` | **없어짐** → `--lie-commit`(기본 2.0) |
| — | `--mac-iface` **추가** (MAC 을 device_id 로) |
| `--thr` 기본 0.35 | 기본 **0.47** |
| `camera_source(idx, y16, reconnect_wait)` | **4인자** (+`tick`) |

`fusion_run.py` 의 `lepton_frames` 를 `*args/**kwargs` 로 열어 두어
앞으로 인자가 더 늘어도 깨지지 않게 했습니다.

`--mac-iface wlan0` 을 쓰면 `--device-id auto` 는 필요 없습니다.

---

## 5. 표준 실행 명령

```bash
cd ~/fall-detection && source .venv/bin/activate

python3 fusion_run.py --camera 8 --y16 --deghost \
  --thr 0.35 --lie-hyst 0.3 --lie-commit 2 --mac-iface wlan0 \
  --impact-port /dev/ttyACM0 \
  --bridge $(hostname -I | awk '{print $1}'):8088 --device falldetect
```

검증할 때는 `--alarm-cooldown 15` 를 추가하세요.
백엔드가 죽어 있으면 `--webhook none` 을 추가하세요.

---

## 6. 아직 정해지지 않은 것

| 항목 | 상태 |
|---|---|
| `--escalate-g` 최종값 | 0.8 은 임시. **통합 테스트에서 실제 낙상 g 를 재고 확정** |
| `ACT_THRESHOLD` 최종값 | 배터리 모드 실측이 필요 (USB 모드보다 작게 나옴) |
| 재현율 / 오탐 | **한 번도 측정 안 함.** `docs/12-validation-sheet.md` |
| 배터리 잔량 | 분압 저항 미장착 |
| LD2450 | 사망, 교체 미정. 구성에서 제외 |
| 카펫 환경 | 미측정 (마루만 측정함) |
| 백엔드 서버 | 간헐적 타임아웃 |
| SmartThings 허브 | 무선 연결 불안정 — 유선 권장 |
