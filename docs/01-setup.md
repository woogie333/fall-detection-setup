# RDK X3 처음부터 다시 세팅하기 — 전체 순서

메모리카드를 새로 굽는 시점부터 AI 모델 실행까지 순서대로. 이전에 막혔던 지점들을
전부 반영했습니다.

**예상 소요 시간**: 2~3시간 (torch 설치가 대부분)

---

## 0. 시작 전 — SD 카드 선택

이번에 용량 부족을 겪으셨으니 이것부터 짚습니다.

| 용량 | 판단 |
|---|---|
| 16GB | **부족합니다.** torch만 3GB 가까이 먹습니다 |
| 32GB | 최소한. 여유가 빠듯합니다 |
| **64GB** | **권장.** 학습 데이터나 녹화 영상을 넣을 여지가 생깁니다 |

속도 등급은 **A2 / UHS-I 이상**을 고르세요. 열화상 프레임을 계속 쓰고 읽는
작업이라 느린 카드는 체감 성능을 크게 떨어뜨립니다.

**용량을 먹는 주범**은 이렇습니다. 참고해두시면 나중에 공간이 부족할 때
어디를 지울지 판단이 섭니다.

```
torch + torchvision      약 2.5 ~ 3 GB
OS 기본                  약 6 ~ 8 GB
pip 캐시                 수백 MB (지워도 됨: pip cache purge)
apt 캐시                 수백 MB (지워도 됨: sudo apt clean)
```

---

## 1. 이미지 굽기

D-Robotics 공식 이미지를 받아 balenaEtcher나 Raspberry Pi Imager로 굽습니다.
**Ubuntu 22.04(Jammy) 계열 데스크톱 또는 서버 이미지**를 고르세요.

굽고 나면 SD 카드에 `boot` 파티션이 보입니다. 여기서 두 가지를 미리 해두면
모니터·키보드 없이 바로 SSH로 들어갈 수 있습니다.

**SSH 미리 켜기** — `boot` 파티션 최상위에 `ssh`라는 **빈 파일**을 만듭니다
(확장자 없음).

**WiFi 미리 설정** — 보드가 유선 연결이 어렵다면, 굽고 나서 모니터를 한 번
연결해 WiFi를 잡는 게 가장 확실합니다. RDK X3는 이미지 버전에 따라
`wpa_supplicant.conf` 사전 설정 방식이 다를 수 있어, 첫 부팅만 모니터로
처리하시는 걸 권합니다.

---

## 2. 첫 부팅

카드를 꽂고 전원을 넣습니다. **5V 3A 어댑터**를 쓰세요. 전력이 모자라면
부팅 중 리부팅이 반복됩니다.

### 2-1. 접속

모니터가 있으면 직접, 없으면 공유기 관리 페이지에서 새로 잡힌 IP를 찾아 SSH로
접속합니다.

```bash
ssh sunrise@192.168.0.xxx
```

기본 계정은 이미지에 따라 `sunrise` 또는 `root`입니다. 문서에 적힌 초기 비밀번호를
쓰고, 접속되면 **바로 바꾸세요.**

```bash
passwd
```

### 2-2. 파일시스템 확장

이미지가 카드 전체를 쓰지 않는 경우가 있습니다. 확인하세요.

```bash
df -h /
```

64GB 카드인데 8GB로 나오면 확장이 필요합니다. D-Robotics 이미지는 대개 첫 부팅 시
자동 확장되지만, 안 됐다면 `sudo raspi-config`(있는 경우) 또는 `parted`로
루트 파티션을 늘립니다.

**여기서 확인 안 하고 넘어가면 나중에 torch 설치할 때 또 공간이 부족합니다.**

### 2-3. 시간 동기화

```bash
timedatectl
sudo timedatectl set-timezone Asia/Seoul
```

시간이 틀리면 apt 인증서 검증이 실패하고 로그 분석도 어려워집니다.

---

## 3. 네트워크 고정

IP가 바뀌면 VS Code, VNC, SmartThings 설정이 전부 어긋납니다. 지금 잡아두세요.

### 방법 A — 공유기 DHCP 예약 (권장)

공유기 관리 페이지에서 보드 MAC 주소에 IP를 고정 할당합니다.
보드 설정을 안 건드리니 실패해도 복구가 쉽습니다.

```bash
ip link show wlan0 | grep ether     # MAC 확인
```

### 방법 B — 보드에서 직접

```bash
nmcli con show                       # DEVICE가 wlan0인 행의 NAME 확인
ip route | grep default              # 게이트웨이 확인

sudo nmcli con mod "프로필이름" \
  ipv4.addresses 192.168.0.100/24 \
  ipv4.gateway 192.168.0.1 \
  ipv4.dns "8.8.8.8,1.1.1.1" \
  ipv4.method manual

sudo nmcli con down "프로필이름" && sudo nmcli con up "프로필이름"
```

**주의**: SSH로 작업 중이면 이 순간 연결이 끊깁니다. 새 IP로 다시 접속하세요.
설정을 잘못하면 못 들어가니 모니터를 옆에 두고 하시는 게 안전합니다.

DHCP 범위 밖의 주소를 고르세요. 보통 100~200번대를 DHCP가 쓰니 `.50`이나
`.220` 같은 값이 안전합니다.

**WiFi 절전 끄기** — 간헐적 끊김을 예방합니다.

```bash
sudo iw dev wlan0 set power_save off
```

---

## 4. 자동 설치 스크립트 실행

여기까지 됐으면 나머지는 스크립트가 처리합니다.

```bash
# PC에서 보드로 스크립트 전송
scp setup_rdk.sh sunrise@192.168.0.100:~/

# 보드에서
chmod +x setup_rdk.sh
./setup_rdk.sh
```

**sudo로 실행하지 마세요.** 필요할 때 스크립트가 알아서 sudo를 부릅니다.

부분 설치도 됩니다.

```bash
./setup_rdk.sh base      # 기본 패키지만
./setup_rdk.sh vnc       # VNC만
./setup_rdk.sh bridge    # edgebridge만
./setup_rdk.sh model     # AI 모델 환경만
./setup_rdk.sh check     # 상태 점검 (언제든 실행 가능)
```

스크립트가 하는 일은 이렇습니다.

- 기본 패키지 설치 (git, python3-opencv, v4l-utils, python3-requests 등)
- 카메라 접근 권한 부여 (`video` 그룹)
- VNC 서버 + XFCE + **dbus-x11** 설치, xstartup 생성, systemd 등록
- edgebridge 설치 및 systemd 등록 (부팅 지연 15초 포함)
- thermal-fall-detection 저장소 clone + venv + torch 설치 + 모델 로드 검증
- 전체 상태 점검

설치가 끝나면 **한 번 재로그인**하세요. 카메라 접근 권한이 그때 적용됩니다.

```bash
exit
ssh sunrise@192.168.0.100
```

---

## 5. SmartThings 연동 (휴대폰 작업)

**이 단계가 가장 실수하기 쉽습니다.** 순서를 지켜주세요.

### 5-1. 드라이버 설치

브라우저에서 [채널 초대 링크](https://api.smartthings.com/invitation-web/accept?id=cc2197b9-2dce-4d88-b6a1-2d198a0dfdef)를 엽니다.

로그인 → 허브 선택 → **LAN Device Trigger V2** 설치. 몇 분 걸립니다.

이미 예전에 설치하셨다면 이 단계는 건너뛰어도 됩니다. 드라이버는 허브에 남아 있어요.

### 5-2. 기존 기기 삭제

**전에 쓰던 `falldetect` 기기가 있다면 지우세요.** 보드를 새로 깔았으니
등록 정보가 어긋납니다. 지우고 새로 만드는 게 훨씬 빠릅니다.

### 5-3. 기기 생성

edgebridge가 실행 중인 상태여야 합니다. 확인:

```bash
sudo systemctl status edgebridge
ss -tlnp | grep 8088
```

앱 → 기기 추가 → **주변 기기 검색** → "LAN-Triggered Device" 생성됨

### 5-4. 설정 입력

기기 → 점 3개 → 설정:

| 항목 | 값 | 흔한 실수 |
|---|---|---|
| LAN Device Name | `falldetect` | **대소문자 정확히.** `FallDetect`는 안 됩니다 |
| LAN App/Device Address | `192.168.0.100` | 포트 없이 IP만 |
| Bridge Address | `192.168.0.100:8088` | **포트 필수** |
| Device icon | Switch | |

두 주소 칸의 형식이 다릅니다. 하나는 IP만, 하나는 IP:포트.

### 5-5. 루틴 만들기

**기기가 켜지는 것만으로는 푸시가 안 옵니다.** 루틴을 걸어야 합니다.

앱 → 루틴 → 추가:
- **If**: `falldetect` → **눌림(Pushed)**
- **Then**: 알림 보내기

이 기기는 스위치가 아니라 **버튼**이라 "눌림"만 조건으로 뜹니다. 정상입니다.

### 5-6. 확인

```bash
curl -X POST http://$(hostname -I | awk '{print $1}'):8088/falldetect/trigger
```

**반드시 LAN IP로** 쏘세요. `127.0.0.1`은 edgebridge가
`Unregistered address`로 거부합니다.

폰에 알림이 오면 완료입니다.

---

## 6. VS Code Remote-SSH (PC 작업)

보드에는 설치할 게 없습니다.

1. VS Code → 확장 → `Remote - SSH` 설치
2. `Ctrl+Shift+P` → `Remote-SSH: Connect to Host` → `sunrise@192.168.0.100`

**비밀번호 없이 접속**하도록 키를 등록하세요. 매번 입력하는 게 은근히 번거롭습니다.

```bash
# PC에서
ssh-keygen -t ed25519          # 이미 있으면 건너뜀
ssh-copy-id sunrise@192.168.0.100
```

**호스트 등록** — PC의 `~/.ssh/config`:

```
Host rdk
    HostName 192.168.0.100
    User sunrise
    ServerAliveInterval 30
    ServerAliveCountMax 6
```

이후 `ssh rdk`로 접속되고, VS Code에서도 `rdk`만 고르면 됩니다.
`ServerAliveInterval`은 WiFi가 잠깐 끊겨도 세션이 죽지 않게 해줍니다.

---

## 7. VNC 접속

스크립트가 이미 설정해뒀습니다. TigerVNC Viewer에서:

```
192.168.0.100:1
```

콜론 뒤 `1`은 디스플레이 번호(포트 5901)입니다. 이걸 빼면 5900으로 붙으려다 실패합니다.

**RealVNC Viewer도 쓸 수 있습니다.** "만료" 메시지는 클라우드 연결(Connect) 체험판
얘기지 Viewer 자체가 아닙니다. 로그인하지 말고 주소를 직접 입력하면 됩니다.

**VNC는 GUI 앱이 꼭 필요할 때만 쓰세요.** 코드 편집은 VS Code가, 영상 확인은
브라우저 스트리밍이 훨씬 낫습니다.

---

## 8. 카메라 확인

웹캠이든 Lepton이든 같은 방법입니다.

```bash
ls /dev/video*
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
```

Lepton(PureThermal)이라면 목록에 **Y16**이 있어야 raw 열화상을 쓸 수 있습니다.

브라우저로 실시간 확인:

```bash
python3 camera_test.py --stream
# → http://192.168.0.100:8090
```

`camera_test.py`도 보드로 옮겨두세요.

```bash
scp camera_test.py sunrise@192.168.0.100:~/
```

---

## 9. AI 모델 실행

스크립트가 `~/fall-detection`에 설치해뒀습니다.

```bash
cd ~/fall-detection
source .venv/bin/activate

python3 list_cameras.py                # 160x120으로 뜨는 인덱스 확인

python3 lepton_live.py --camera 0 \
  --webhook http://$(hostname -I | awk '{print $1}'):8088/falldetect/trigger
```

`--display`는 쓰지 마세요. SSH에서는 창을 못 띄웁니다.

낙상이 감지되면 콘솔에 `>>> FALL ALARM <<<`가 찍히고 웹훅이 나가며 폰에 알림이 옵니다.

**성능 확인**: 종료 시 `processed N windows @ X win/s`가 출력됩니다.
9 win/s 이상이면 실시간으로 충분합니다. 부족하면 `--stride 2`를 붙이세요.

### 자동 실행 등록 (선택)

감지 로직을 충분히 검증한 뒤에 하세요. 튜닝 중에는 수동 실행이 편합니다.

```bash
sudo tee /etc/systemd/system/fall.service > /dev/null <<EOF
[Unit]
Description=Thermal Fall Detection
After=network-online.target edgebridge.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/fall-detection
ExecStartPre=/bin/sleep 20
ExecStart=$HOME/fall-detection/.venv/bin/python lepton_live.py \\
  --camera 0 --stride 1 \\
  --webhook http://$(hostname -I | awk '{print $1}'):8088/falldetect/trigger
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now fall.service
journalctl -u fall.service -f
```

---

## 10. 최종 점검

```bash
./setup_rdk.sh check
```

전부 초록색이면 완료입니다.

---

# 이번에 겪은 함정 모음

같은 데서 두 번 막히지 않도록 정리해둡니다.

| 증상 | 원인 | 해결 |
|---|---|---|
| `pip install --break-system-packages` 실패 | 구버전 pip에 없는 옵션 | 옵션 빼고 설치, 또는 `apt install python3-*` |
| `ufw: command not found` | 방화벽 미설치 | 정상. 열 포트가 없으니 건너뛰세요 |
| VNC 회색 화면 + X 커서 | dbus-x11 없음 | `apt install dbus-x11`, xstartup에서 `dbus-launch` |
| VNC `unable to connect to socket` | 서버 미실행 또는 localhost 바인딩 | `ss -tlnp \| grep 590`, `-localhost no` |
| VNC 주소에 `:1` 누락 | 포트 5900으로 접속 시도 | `IP:1` 형식 |
| `Unregistered address` | 127.0.0.1로 요청 | **보드 LAN IP**로 요청 |
| edgebridge 로그에 아무것도 안 찍힘 | 예전 프로세스가 포트 점유 | `pgrep -af edgebridge` → `pkill` |
| `No existing registrations` | 브릿지 재시작으로 등록 유실 | 앱에서 기기 삭제 후 재생성 |
| 앱 설정 저장해도 등록 안 됨 | 값이 안 바뀌면 이벤트 미발생 | 값을 바꿨다 되돌리거나 기기 재생성 |
| `Network is unreachable` (부팅 시) | 네트워크보다 먼저 기동 | systemd에 `ExecStartPre=/bin/sleep 15` |
| 400 Bad Request | 기기 이름 불일치 | **대소문자까지** 정확히 |
| 알림이 안 옴 (기기는 켜짐) | 루틴 미생성 | 루틴 → If 눌림 / Then 알림 |
| 루틴 조건에 "켜짐"이 없음 | 버튼 타입 기기 | "눌림(Pushed)"이 정상 |
| torch 설치 실패 | 파이썬 버전 / 공간 부족 | `python3 -V`, `df -h`, torch 2.2로 낮추기 |
| opencv 설치가 끝없이 걸림 | arm64 소스 빌드 | pip 말고 `apt install python3-opencv` |
| 캡처 중 보드 재부팅 | USB 전력 부족 | 5V 3A 어댑터 확인 |

---

# 파일 체크리스트

보드로 옮겨야 할 파일들입니다.

```bash
scp setup_rdk.sh camera_test.py smartthings_notifier.py test_notify.py \
    sunrise@192.168.0.100:~/
```

| 파일 | 용도 |
|---|---|
| `setup_rdk.sh` | 자동 설치 (필수) |
| `camera_test.py` | 카메라 확인, 브라우저 스트리밍 |
| `smartthings_notifier.py` | 나중에 융합 로직 짤 때 사용 |
| `test_notify.py` | SmartThings 연동 테스트 |

`smartthings_notifier.py`와 `test_notify.py`는 지금 당장은 필요 없습니다.
AI 모델의 `--webhook` 옵션이 같은 일을 해주거든요. 나중에 3센서 융합 로직을
직접 짤 때 쓰시면 됩니다.
