# 랜선 직결 접속 (맥 ↔ 보드)

공유기가 없는 곳에서 맥과 보드를 랜선으로 바로 연결해 작업하기 위한 설정.
**미리 해두면** 나중에 케이블만 꽂고 명령 한 줄로 붙습니다.

---

## 두 가지 방식

| | 인터넷 공유 | 고정 IP 직결 |
|---|---|---|
| 보드 인터넷 | **됨** | 안 됨 |
| Tailscale | 됨 | 안 됨 |
| apt / git | 됨 | 안 됨 |
| 맥 설정 | 매번 켜야 함 | 한 번만 |
| 실패 여지 | DHCP 협상 필요 | 거의 없음 |

**평소에는 인터넷 공유**를 쓰고, 그게 안 될 때를 위해 **고정 IP 프로필을 예비로** 만들어두는 조합을 권합니다.

---

## 1. 보드 설정 (한 번만)

### 1-1. mDNS

`ubuntu.local` 이름으로 찾을 수 있게 해줍니다.

```bash
sudo apt install -y avahi-daemon
sudo systemctl enable --now avahi-daemon
systemctl is-active avahi-daemon
```

### 1-2. 직결용 프로필 생성

```bash
nmcli con show          # 기존 유선 프로필 이름 확인 (보통 "Wired connection 1")

sudo nmcli con add type ethernet ifname eth0 con-name direct \
  ipv4.method manual \
  ipv4.addresses 192.168.50.2/24 \
  autoconnect no
```

`autoconnect no` 가 핵심입니다. 평소에는 활성화되지 않으므로 공유기에 꽂을 때는
기존 DHCP 프로필이 그대로 동작합니다.

확인:

```bash
nmcli con show
nmcli -f connection.autoconnect,ipv4.addresses con show direct
```

### 1-3. 전환 스크립트 (선택)

매번 긴 명령을 치기 싫으면:

```bash
sudo tee /usr/local/bin/netmode > /dev/null <<'EOF'
#!/bin/sh
case "$1" in
  direct)
    nmcli con up direct
    echo "직결 모드 — 맥에서: ssh sunrise@192.168.50.2"
    ;;
  auto)
    nmcli con down direct 2>/dev/null
    nmcli con up "Wired connection 1" 2>/dev/null || true
    echo "DHCP 모드"
    ;;
  *)
    echo "사용법: netmode [direct|auto]"
    nmcli -f NAME,DEVICE,STATE con show --active
    ;;
esac
EOF
sudo chmod +x /usr/local/bin/netmode
```

이후 `sudo netmode direct` / `sudo netmode auto` 로 전환합니다.

---

## 2. 맥 설정 (한 번만)

### 2-1. 인터페이스 확인

케이블이 꽂힌 인터페이스를 먼저 찾습니다.

```bash
ifconfig | grep -B5 "status: active"
```

`100baseTX` 나 `1000baseT` 가 보이는 항목이 유선입니다. USB 랜 어댑터라면
시스템 설정에서 이름이 `USB 10/100 LAN` 같은 식으로 보입니다.

### 2-2. 수동 IP 지정

**시스템 설정 → 네트워크 → (해당 어댑터) → 세부사항 → TCP/IP**

- IPv4 구성: **수동**
- IP 주소: `192.168.50.1`
- 서브넷 마스크: `255.255.255.0`
- 라우터: **비워둠** (인터넷은 Wi-Fi 로 나가야 하므로)

라우터를 채우면 맥이 인터넷을 이 어댑터로 내보내려다 웹이 안 됩니다.

### 2-3. 서비스 순서 확인

**시스템 설정 → 네트워크 → 우측 상단 ⋯ → 서비스 순서 설정**

**Wi-Fi 가 유선보다 위**에 있어야 합니다. 아래에 있으면 맥이 인터넷을
랜선 쪽으로 보내려 해서 웹이 끊깁니다.

---

## 3. 사용

**보드에서** (HDMI 모니터 또는 다른 경로로 접속해서):

```bash
sudo nmcli con up direct
```

**맥에서**:

```bash
ping -c 3 192.168.50.2
ssh sunrise@192.168.50.2
```

`~/.ssh/config` 에 등록해두면 편합니다.

```
Host rdk-direct
    HostName 192.168.50.2
    User sunrise
    ServerAliveInterval 30
```

끝나면 보드에서 되돌립니다.

```bash
sudo nmcli con down direct
```

---

## 4. 인터넷 공유 방식 (보드에 인터넷이 필요할 때)

**시스템 설정 → 일반 → 공유 → 인터넷 공유**

- 공유할 연결: **Wi-Fi**
- 사용하는 기기 대상: 유선 어댑터 토글 ON
- 왼쪽의 **인터넷 공유 메인 스위치도 ON** (이걸 빠뜨리면 시작되지 않습니다)

켜지면 맥에 `bridge100` 인터페이스가 생기고 `192.168.2.1` 을 갖습니다.

```bash
ifconfig bridge100 | grep "inet "
```

보드는 `192.168.2.x` 를 받습니다. 안 받으면 랜선을 뽑았다 꽂아 DHCP 를 재시도시키세요.

```bash
ping -c 3 ubuntu.local
arp -a | grep 192.168.2
```

> 이 방식을 쓰려면 보드의 eth0 이 **DHCP 프로필로 활성**이어야 합니다.
> `direct` 프로필이 올라와 있으면 먼저 내리세요: `sudo nmcli con down direct`

---

## 5. 닭과 달걀 문제

직결 설정을 하려면 보드에 접속해야 하는데, 접속하려면 설정이 되어 있어야 합니다.
**집에서 미리 해두는 것이 정답**이지만, 밖에서 막혔다면 순서는 이렇습니다.

1. **휴대폰 핫스팟** — 보드에 SSID 가 등록돼 있으면 켜기만 하면 붙습니다.
   이것도 미리 등록해두세요: `sudo nmcli dev wifi connect "핫스팟" password "비번"`
2. **HDMI 모니터 + 키보드** — 가장 확실합니다
3. **맥 인터넷 공유** — 보드가 DHCP 로 IP 를 받으면 `ubuntu.local` 로 찾을 수 있습니다
4. **SD 카드를 다른 컴퓨터에 꽂아** `/etc/NetworkManager/system-connections/` 에
   프로필 파일을 직접 넣기 (최후의 수단)

---

## 6. 문제 해결

| 증상 | 대응 |
|---|---|
| `ping 192.168.50.2` 무응답 | 보드에서 `nmcli con show --active` 로 direct 가 올라왔는지 확인 |
| 맥이 링크로컬(169.254.x.x)만 잡음 | DHCP 서버가 없는 정상 상태. 수동 IP 를 지정하세요 |
| 랜선 꽂으면 맥 웹이 안 됨 | 서비스 순서에서 Wi-Fi 를 위로. 어댑터 "라우터" 칸 비우기 |
| `ubuntu.local` 이름 해석 안 됨 | 보드에서 `systemctl status avahi-daemon` |
| 인터넷 공유 켜도 bridge100 없음 | 메인 스위치 미작동. 회사·학교 관리 맥은 정책 차단일 수 있음 |
| 직결 중 apt/git/Tailscale 실패 | 정상입니다. 인터넷이 없습니다. 인터넷 공유 방식으로 전환하세요 |
| 링크 자체가 안 붙음 | `sudo ethtool eth0 \| grep -i "link detected"` |
