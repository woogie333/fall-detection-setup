# thermal-fall-detection — RDK X3 실행 가이드 및 코드 분석

레포: https://github.com/ehgus06-alt/thermal-fall-detection

---

## 요약

**결론부터: RDK X3에서 실시간으로 돌아갈 가능성이 높습니다.** 모델이 매우 가볍고,
Lepton이 9fps라 요구 처리량 자체가 낮습니다.

그리고 **친구분 코드에 `--webhook` 옵션이 이미 있습니다.** 우리가 구축한 SmartThings
연동에 코드 수정 없이 바로 붙습니다. 이게 가장 큰 수확이에요.

```bash
python3 lepton_live.py --camera 0 --webhook http://192.168.0.100:8088/falldetect/trigger
```

---

## 1. 코드 구조 — 실제로 뭘 하는가

```
Lepton 프레임 (9fps)
   ↓ prep_gray128()      그레이스케일 → 2~98% 백분위 스트레치 → 128×128 정사각
   ↓ 24프레임 버퍼        약 2.7초 분량
   ↓ make_window_feat()  3채널 생성: 원본 / MHI(모션이력) / MEI(모션에너지)
   ↓ MobileNetV3-small   1.52M 파라미터, 이진 분류
   ↓ EMA 평활 + persist  연속 3윈도우 이상 임계 초과 시 알람
   → 콘솔 출력 + 스냅샷 저장 + (선택) 웹훅 POST
```

**설계가 잘 되어 있는 부분** 두 가지를 짚어두면, EMA 평활과 `persist` 카운터로 순간
오탐을 억제하는 구조가 우리 설계 문서의 스코어링과 같은 철학이고, `--min-descent`
옵션에 "이 값은 카메라마다 달라서 일반화되지 않는다"고 정직하게 적어둔 점도 좋습니다.

**FALL / LIED 구분**도 영리합니다. 누운 자세 자체가 아니라 *어떻게 그 자세가 되었는가*로
구분해요. 천천히 누우면 LIED, 급격히 쓰러지면 FALL. 우리 설계와 같은 방향입니다.

---

## 2. 성능 — 실측 벤치마크

컨테이너 CPU(Xeon 2.1GHz, 2스레드)에서 실제로 측정한 값입니다.

| 단계 | 시간 |
|---|---|
| `prep_gray128` (프레임 전처리) | 0.70 ms |
| `make_window_feat` (MHI/MEI 생성) | 1.88 ms |
| MobileNetV3-small 추론 | 5.40 ms |
| **합계** | **약 8 ms/윈도우 → 125 win/s** |

레포의 "100+ win/s" 주장과 일치합니다.

**RDK X3 예상치**: Cortex-A53 1.5GHz는 Xeon 대비 코어당 3~5배 느리지만 4코어입니다.
보수적으로 잡아 **30~60 ms/윈도우 (17~33 win/s)** 정도로 예상합니다.

Lepton이 9fps이므로 **`--stride 1`로도 여유가 있을 것**으로 봅니다.
느리면 `--stride 2`를 쓰면 되고요.

> 이건 어디까지나 추정입니다. 실제 보드에서 돌려보면 마지막 줄에
> `processed N windows @ X win/s`가 찍히니 그 값으로 확정하세요.

**BPU는 안 씁니다.** RDK X3의 5 TOPS NPU를 쓰려면 모델을 `.bin`으로 변환해야 하는데,
CPU로 충분하다면 굳이 할 필요 없습니다. 나중에 여유가 없으면 그때 고려하세요.

---

## 3. 설치 절차 (RDK X3)

### 3-1. 먼저 파이썬 버전 확인

```bash
python3 -V
```

레포는 3.10+를 요구하지만 실제 코드는 3.8에서도 돌 겁니다. 다만 **torch aarch64 휠이
파이썬 버전에 따라 없을 수 있어** 여기가 첫 관문입니다.

### 3-2. 시스템 패키지

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv python3-opencv libatlas-base-dev v4l-utils
sudo usermod -aG video $USER      # 재로그인 필요
```

**opencv는 반드시 apt로 받으세요.** `pip install opencv-python`은 arm64에서 소스
빌드로 넘어가 한 시간 넘게 걸리거나 실패합니다.

### 3-3. 저장소 + 가상환경

```bash
cd ~
git clone https://github.com/ehgus06-alt/thermal-fall-detection
cd thermal-fall-detection

python3 -m venv .venv --system-site-packages   # apt opencv를 재사용하려면 이 옵션 필수
source .venv/bin/activate

pip install --upgrade pip
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install numpy pillow scikit-learn
```

torch 설치는 200MB 이상 받으므로 시간이 걸립니다. 실패하면 버전을 낮춰보세요.

```bash
pip install "torch==2.2.*" "torchvision==0.17.*" --index-url https://download.pytorch.org/whl/cpu
```

### 3-4. 설치 검증 (카메라 없이)

```bash
python3 -c "
import torch, torchvision, cv2, numpy
print('torch', torch.__version__)
print('opencv', cv2.__version__)
"
```

모델이 로드되는지도 확인하세요.

```bash
python3 -c "
import torch, torchvision, torch.nn as nn
ck = torch.load('runs/best.pt', map_location='cpu', weights_only=False)
m = torchvision.models.mobilenet_v3_small()
m.classifier[-1] = nn.Linear(m.classifier[-1].in_features, 1)
m.load_state_dict(ck['model'])
print('모델 로드 성공, W =', ck['args']['W'])
"
```

`모델 로드 성공, W = 24`가 나오면 소프트웨어 준비는 끝입니다.

---

## 4. 카메라 연결 및 실행

```bash
ls /dev/video*
python3 list_cameras.py                        # 160x120으로 뜨는 인덱스가 Lepton
v4l2-ctl --list-formats-ext -d /dev/video0     # Y16이 있으면 --y16 사용 가능
```

```bash
# 헤드리스(SSH) — 기본
python3 lepton_live.py --camera 0

# 16비트 원본으로 잡히는 경우
python3 lepton_live.py --camera 0 --y16
```

**`--display`는 쓰지 마세요.** SSH 환경에서는 창을 못 띄웁니다. 대신 우리가 만든
`camera_test.py --stream`으로 브라우저에서 영상을 확인하면 됩니다(별도 터미널).

단, **카메라는 한 번에 한 프로세스만 열 수 있습니다.** 두 스크립트를 동시에 돌리면
충돌하니, 영상 확인과 감지 실행은 번갈아 하세요.

---

## 5. SmartThings 연동 — 코드 수정 불필요

`--webhook` 옵션이 알람 시 JSON을 POST합니다. 우리 edgebridge 엔드포인트를 그대로
넣으면 됩니다.

```bash
python3 lepton_live.py --camera 0 \
  --webhook http://192.168.0.100:8088/falldetect/trigger \
  --device-id thermal-01
```

**IP는 반드시 보드의 LAN IP**를 쓰세요. `127.0.0.1`은 edgebridge가 거부합니다.

```bash
python3 lepton_live.py --camera 0 \
  --webhook http://$(hostname -I | awk '{print $1}'):8088/falldetect/trigger
```

웹훅 전송은 별도 스레드에서 처리되므로 감지 루프를 막지 않습니다. 이 부분도 잘 짜여
있네요.

성공하면 콘솔에 `webhook -> ... [200]`이 찍히고 폰에 알림이 옵니다.

---

## 6. 자동 실행 등록

```bash
sudo tee /etc/systemd/system/fall.service > /dev/null <<EOF
[Unit]
Description=Thermal Fall Detection
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/thermal-fall-detection
ExecStartPre=/bin/sleep 15
ExecStart=$HOME/thermal-fall-detection/.venv/bin/python lepton_live.py \\
  --camera 0 --stride 1 \\
  --webhook http://$(hostname -I | awk '{print $1}'):8088/falldetect/trigger
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now fall.service
journalctl -u fall.service -f
```

---

## 7. 검증 시 반드시 알아야 할 것

여기가 이 문서에서 제일 중요한 부분입니다.

### 7-1. 이 모델은 검증 지표가 없습니다

체크포인트에 담긴 메트릭을 열어보면 이렇습니다.

```json
{"thr": 0.5, "n_train": 391, "note": "full-train, no val"}
```

**검증셋 없이 전체 데이터로 학습했고, 정확도·재현율 수치가 없습니다.** 학습 샘플도
391개로 적습니다. 즉 이 모델이 얼마나 맞는지는 **아무도 모르는 상태**입니다.

친구분도 README에 "누수 미검증, 지표가 낙관적일 수 있음"이라고 정직하게 적어두셨는데,
실제로는 지표 자체가 없습니다. 나쁜 코드라는 뜻이 아니라, **여러분이 직접 측정해야
한다**는 뜻입니다.

### 7-2. 학습 데이터와 실제 하드웨어가 다릅니다

모델은 FLIR One 계열 고해상도 열화상으로 학습됐습니다. 우리 Lepton 3.0은 160×120이고
**non-radiometric**이라 화질과 특성이 다릅니다. 도메인 갭 때문에 임계값 재보정이
거의 확실히 필요합니다.

### 7-3. 그래서 이렇게 테스트하세요

```bash
# 1단계: 임계값 기본값으로 실행하고 로그만 관찰
python3 lepton_live.py --camera 0

# 2단계: 일부러 넘어져보며 p값이 얼마나 올라가는지 확인
#   → 낙상 시 ema가 0.47을 넘는가?
#   → 그냥 걸을 때 0.47을 넘지 않는가?

# 3단계: 임계값 조정
python3 lepton_live.py --camera 0 --thr 0.55 --persist 4
```

**반드시 기록해야 할 것 두 가지**입니다.

정상 활동에서 오탐이 몇 번 나는지. 특히 **바닥에 앉기, 눕기, 물건 줍기**를 꼭
테스트하세요. README에도 이게 약점이라고 적혀 있습니다.

실제 낙상을 몇 번 중 몇 번 잡는지. 방향을 바꿔가며(앞으로/옆으로/뒤로) 각각 10회
정도는 해보셔야 의미 있는 수치가 나옵니다.

### 7-4. 우리 융합 설계와의 관계

이 모델은 **열화상 단독** 판정입니다. 우리 설계 문서의 3센서 융합에서 Lepton 파트를
통째로 대체할 수 있어요. 좋은 출발점입니다.

다만 오탐이 많다면, 모델 출력을 최종 판정이 아니라 **점수의 한 항목**으로 쓰는 게
낫습니다. 모델이 낙상이라고 하면 +3점, 여기에 mmWave의 정지 지속과 가속도계의 충격을
더해 임계를 넘으면 알람 — 이게 우리 설계 문서의 구조고, 오탐을 크게 줄여줍니다.

---

## 8. 예상되는 문제

| 증상 | 원인과 대응 |
|---|---|
| `pip install torch` 실패 | 파이썬 버전 문제. `python3 -V` 확인 후 torch 2.2로 낮춰보기 |
| opencv 설치가 끝나지 않음 | pip 말고 `apt install python3-opencv` |
| `cannot pull frames from camera` | 다른 프로세스가 카메라 점유 중. `fuser /dev/video0`로 확인 |
| 카메라 인덱스를 못 찾음 | `list_cameras.py`로 160×120 나오는 인덱스 사용 |
| 프레임은 오는데 화면이 이상 | `--y16` 옵션 유무를 반대로 해보기 |
| `--simclip` 테스트가 안 됨 | 캐시 데이터가 레포에 없음(gitignore). 라이브 카메라로 테스트 |
| 추론이 너무 느림 | `--stride 2` 또는 3 |
| 웹훅 실패 | `127.0.0.1` 대신 보드 LAN IP 사용 |
| 캡처 중 보드 리부팅 | USB 전력 부족. 5V 3A 어댑터 확인 |

---

## 9. 권장 진행 순서

1. torch 설치 성공 확인 (여기가 제일 막히기 쉬움)
2. 모델 로드 확인 — 위의 검증 스니펫
3. 웹캠으로 `list_cameras.py` 동작 확인 (Lepton 오기 전에 가능)
4. Lepton 연결 → `camera_test.py --stream`으로 영상 먼저 확인
5. `lepton_live.py --camera N` 실행, win/s 실측
6. `--webhook` 붙여서 폰 알림까지 연결
7. 임계값 보정 — 여기가 실제 작업량의 대부분입니다
8. systemd 등록

**1~3번은 Lepton이 도착하기 전에 지금 할 수 있습니다.** 미리 해두면 카메라가 왔을 때
바로 넘어갑니다.
