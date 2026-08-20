#!/usr/bin/env bash
#
# RDK X3 낙상감지 프로젝트 — 자동 설치 스크립트
#
# 사용법:
#   chmod +x setup_rdk.sh
#   ./setup_rdk.sh              # 전체 설치
#   ./setup_rdk.sh base         # 기본 패키지만
#   ./setup_rdk.sh vnc          # VNC만
#   ./setup_rdk.sh bridge       # edgebridge만
#   ./setup_rdk.sh model        # AI 모델 환경만
#   ./setup_rdk.sh check        # 설치 상태 점검
#
# sudo로 실행하지 마세요. 필요할 때 스크립트가 알아서 sudo를 부릅니다.

set -uo pipefail

# ---------------------------------------------------------------- 출력 헬퍼
BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YEL=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'

step()  { echo; echo "${BOLD}${BLU}▶ $*${RST}"; }
ok()    { echo "  ${GRN}✓${RST} $*"; }
warn()  { echo "  ${YEL}!${RST} $*"; }
fail()  { echo "  ${RED}✗${RST} $*"; }
info()  { echo "  ${DIM}$*${RST}"; }

die() { fail "$*"; exit 1; }

# ---------------------------------------------------------------- 사전 확인
[ "$(id -u)" -eq 0 ] && die "root로 실행하지 마세요. 일반 계정으로 ./setup_rdk.sh 하세요."

USER_NAME="$(whoami)"
USER_HOME="$HOME"
PROJECT_DIR="$USER_HOME/fall-detection"

detect_ip() {
    hostname -I 2>/dev/null | awk '{print $1}'
}

check_disk() {
    local avail_mb
    avail_mb=$(df -m "$USER_HOME" | awk 'NR==2 {print $4}')
    echo "$avail_mb"
}

# ---------------------------------------------------------------- 기본 패키지
install_base() {
    step "기본 패키지 설치"

    local avail
    avail=$(check_disk)
    info "여유 공간: ${avail} MB"
    if [ "$avail" -lt 4000 ]; then
        warn "여유 공간이 4GB 미만입니다. torch 설치가 실패할 수 있습니다."
        warn "더 큰 SD 카드를 쓰거나 불필요한 파일을 정리하세요."
        read -rp "  그래도 계속할까요? [y/N] " a
        [[ "$a" =~ ^[Yy]$ ]] || exit 1
    fi

    sudo apt update || die "apt update 실패. 인터넷 연결을 확인하세요."
    sudo apt install -y \
        git curl wget nano \
        python3-pip python3-venv \
        python3-opencv v4l-utils \
        python3-requests \
        network-manager \
        || die "패키지 설치 실패"

    # 카메라 접근 권한
    sudo usermod -aG video "$USER_NAME"
    ok "기본 패키지 설치 완료"
    warn "카메라 권한 적용을 위해 나중에 한 번 재로그인이 필요합니다."
}

# ---------------------------------------------------------------- VNC
install_vnc() {
    step "VNC 서버 설치 (TigerVNC + XFCE)"

    sudo apt install -y \
        tigervnc-standalone-server tigervnc-common \
        dbus-x11 xfce4 xfce4-goodies \
        || die "VNC 패키지 설치 실패"

    mkdir -p "$USER_HOME/.vnc"

    # dbus-launch 없이는 XFCE가 회색 화면만 뜬다.
    # SESSION_MANAGER / DBUS_SESSION_BUS_ADDRESS를 지우지 않으면
    # dbus-launch가 죽은 주소를 물고 늘어져 같은 증상이 반복된다.
    cat > "$USER_HOME/.vnc/xstartup" <<'XEOF'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
[ -r "$HOME/.Xresources" ] && xrdb "$HOME/.Xresources"
exec dbus-launch --exit-with-session startxfce4
XEOF
    chmod +x "$USER_HOME/.vnc/xstartup"
    ok "xstartup 생성"

    if [ ! -f "$USER_HOME/.vnc/passwd" ]; then
        echo
        info "VNC 접속 비밀번호를 설정합니다 (8자까지만 유효)."
        vncpasswd
    else
        ok "기존 비밀번호 유지 (변경하려면: vncpasswd)"
    fi

    sudo tee /etc/systemd/system/vncserver@.service > /dev/null <<EOF
[Unit]
Description=TigerVNC server on display %i
After=network.target

[Service]
Type=forking
User=${USER_NAME}
WorkingDirectory=${USER_HOME}
ExecStartPre=-/usr/bin/vncserver -kill :%i
ExecStart=/usr/bin/vncserver :%i -localhost no -geometry 1280x800 -depth 24
ExecStop=/usr/bin/vncserver -kill :%i
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable --now vncserver@1

    sleep 3
    if ss -tlnp 2>/dev/null | grep -q ':5901'; then
        ok "VNC 실행 중 — 접속: $(detect_ip):1"
    else
        warn "VNC가 안 떴습니다. 로그 확인: journalctl -u vncserver@1 -n 30"
        warn "PID 파일 경로가 다를 수 있습니다: ls ~/.vnc/*.pid"
    fi
}

# ---------------------------------------------------------------- edgebridge
install_bridge() {
    step "SmartThings edgebridge 설치"

    # 기존 프로세스가 포트를 쥐고 있으면 새 인스턴스가 조용히 실패한다.
    if pgrep -f edgebridge.py > /dev/null; then
        warn "실행 중인 edgebridge를 종료합니다."
        pkill -f edgebridge.py
        sleep 1
    fi

    if [ -d "$USER_HOME/edgebridge/.git" ]; then
        ok "이미 받아져 있음 — 최신으로 갱신"
        git -C "$USER_HOME/edgebridge" pull --quiet || warn "pull 실패 (무시 가능)"
    else
        git clone --depth 1 https://github.com/toddaustin07/edgebridge.git \
            "$USER_HOME/edgebridge" || die "edgebridge clone 실패"
    fi

    python3 -c "import requests" 2>/dev/null || pip3 install requests

    local ip
    ip=$(detect_ip)
    [ -z "$ip" ] && die "LAN IP를 찾을 수 없습니다. 네트워크 연결을 확인하세요."

    sudo tee /etc/systemd/system/edgebridge.service > /dev/null <<EOF
[Unit]
Description=SmartThings edgebridge forwarding server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${USER_HOME}/edgebridge
# 부팅 직후엔 IP 할당 전이라 'Network is unreachable'로 죽는다. 지연 필요.
ExecStartPre=/bin/sleep 15
ExecStart=/usr/bin/python3 ${USER_HOME}/edgebridge/edgebridge.py
Restart=always
RestartSec=10
StartLimitIntervalSec=0
StandardOutput=journal
StandardError=journal
SyslogIdentifier=edgebridge

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable edgebridge
    sudo systemctl restart edgebridge

    # ExecStartPre의 sleep 15를 기다린다
    info "서비스 기동 대기 중 (약 20초)..."
    sleep 20

    if ss -tlnp 2>/dev/null | grep -q ':8088'; then
        ok "edgebridge 실행 중 — http://${ip}:8088"
    else
        warn "포트 8088이 안 열렸습니다. 로그: journalctl -u edgebridge -n 30"
    fi

    # 로그 조회 권한 (hint 메시지 제거)
    sudo usermod -aG adm,systemd-journal "$USER_NAME" 2>/dev/null || true

    echo
    info "다음은 휴대폰 작업입니다 (마스터 가이드 5단계 참고):"
    info "  LAN App/Device Address : ${ip}"
    info "  Bridge Address         : ${ip}:8088"
    info "  LAN Device Name        : falldetect"
}

# ---------------------------------------------------------------- AI 모델
install_model() {
    step "AI 모델 환경 설치 (torch — 시간이 오래 걸립니다)"

    local avail
    avail=$(check_disk)
    if [ "$avail" -lt 3000 ]; then
        die "여유 공간 ${avail}MB — torch 설치에 최소 3GB가 필요합니다."
    fi

    if [ -d "$PROJECT_DIR/.git" ]; then
        ok "저장소 이미 존재"
    else
        git clone --depth 1 https://github.com/ehgus06-alt/thermal-fall-detection \
            "$PROJECT_DIR" || die "저장소 clone 실패"
    fi

    cd "$PROJECT_DIR" || die "디렉터리 이동 실패"

    # --system-site-packages: apt로 설치한 opencv를 재사용한다.
    # pip으로 opencv를 받으면 arm64에서 소스 빌드로 넘어가 한 시간 넘게 걸린다.
    if [ ! -d .venv ]; then
        python3 -m venv .venv --system-site-packages || die "venv 생성 실패"
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate

    pip install --upgrade pip --quiet

    info "torch 다운로드 중 (200MB+, 수 분 소요)..."
    if ! pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu; then
        warn "최신 torch 실패 — 2.2 버전으로 재시도합니다."
        pip install "torch==2.2.*" "torchvision==0.17.*" \
            --index-url https://download.pytorch.org/whl/cpu \
            || die "torch 설치 실패. 파이썬 버전을 확인하세요: python3 -V"
    fi

    pip install numpy pillow scikit-learn --quiet || die "의존성 설치 실패"

    # 모델이 실제로 로드되는지 확인
    if python3 -c "
import torch, torchvision, torch.nn as nn
ck = torch.load('runs/best.pt', map_location='cpu', weights_only=False)
m = torchvision.models.mobilenet_v3_small()
m.classifier[-1] = nn.Linear(m.classifier[-1].in_features, 1)
m.load_state_dict(ck['model'])
print('W =', ck['args']['W'])
" 2>/dev/null; then
        ok "모델 로드 검증 성공"
    else
        fail "모델 로드 실패 — torch 설치를 확인하세요."
    fi

    deactivate
    ok "설치 위치: $PROJECT_DIR"
    info "실행:  cd $PROJECT_DIR && source .venv/bin/activate"
    info "       python3 lepton_live.py --camera 0 --webhook http://$(detect_ip):8088/falldetect/trigger"
}

# ---------------------------------------------------------------- 점검
run_check() {
    step "설치 상태 점검"

    local ip; ip=$(detect_ip)
    echo
    echo "  ${BOLD}네트워크${RST}"
    if [ -n "$ip" ]; then ok "LAN IP: $ip"; else fail "IP 없음"; fi
    ping -c1 -W2 8.8.8.8 >/dev/null 2>&1 && ok "인터넷 연결" || fail "인터넷 안 됨"

    echo
    echo "  ${BOLD}디스크${RST}"
    local avail; avail=$(check_disk)
    if [ "$avail" -gt 2000 ]; then ok "여유 ${avail} MB"; else warn "여유 ${avail} MB — 부족"; fi

    echo
    echo "  ${BOLD}패키지${RST}"
    for m in cv2 numpy requests; do
        python3 -c "import $m" 2>/dev/null && ok "$m" || fail "$m 없음"
    done
    command -v v4l2-ctl >/dev/null && ok "v4l-utils" || warn "v4l-utils 없음"

    echo
    echo "  ${BOLD}카메라${RST}"
    if ls /dev/video* >/dev/null 2>&1; then
        ok "장치: $(ls /dev/video* | tr '\n' ' ')"
    else
        warn "카메라 미연결"
    fi

    echo
    echo "  ${BOLD}서비스${RST}"
    for svc in edgebridge vncserver@1; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            ok "$svc 실행 중"
        else
            warn "$svc 미실행"
        fi
    done
    ss -tlnp 2>/dev/null | grep -q ':8088' && ok "포트 8088 열림" || warn "포트 8088 닫힘"
    ss -tlnp 2>/dev/null | grep -q ':5901' && ok "포트 5901 열림" || warn "포트 5901 닫힘"

    echo
    echo "  ${BOLD}AI 모델${RST}"
    if [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
        if "$PROJECT_DIR/.venv/bin/python" -c "import torch" 2>/dev/null; then
            ok "torch $("$PROJECT_DIR/.venv/bin/python" -c 'import torch;print(torch.__version__)')"
        else
            fail "torch 없음"
        fi
    else
        warn "모델 환경 미설치"
    fi

    echo
    if [ -n "$ip" ]; then
        echo "  ${BOLD}SmartThings 연동 테스트${RST}"
        info "  curl -X POST http://${ip}:8088/falldetect/trigger"
    fi
    echo
}

# ---------------------------------------------------------------- 메인
main() {
    local target="${1:-all}"

    echo
    echo "${BOLD}RDK X3 낙상감지 프로젝트 설치${RST}"
    echo "${DIM}계정: ${USER_NAME}   IP: $(detect_ip)${RST}"

    case "$target" in
        base)   install_base ;;
        vnc)    install_vnc ;;
        bridge) install_bridge ;;
        model)  install_model ;;
        check)  run_check ;;
        all)
            install_base
            install_vnc
            install_bridge
            install_model
            run_check
            ;;
        *)
            echo "사용법: $0 [all|base|vnc|bridge|model|check]"
            exit 1
            ;;
    esac

    echo
    echo "${BOLD}${GRN}완료${RST}"
    if [ "$target" = "all" ]; then
        echo
        echo "다음 할 일:"
        echo "  1. 재로그인 (카메라 권한 적용):  exit 후 다시 ssh 접속"
        echo "  2. 휴대폰에서 SmartThings 기기 생성 — 마스터 가이드 5단계"
        echo "  3. 연동 확인:  curl -X POST http://$(detect_ip):8088/falldetect/trigger"
    fi
    echo
}

main "$@"
