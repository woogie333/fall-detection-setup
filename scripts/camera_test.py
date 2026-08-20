#!/usr/bin/env python3
"""
카메라 연결 테스트 도구 (UVC 웹캠 / PureThermal 공용)

VNC 없이 PC나 휴대폰 브라우저로 카메라 영상을 확인한다.
나중에 Lepton을 꽂았을 때도 --raw 옵션으로 그대로 쓸 수 있다.

사용법:

  # 연결된 카메라와 지원 포맷 확인
  python3 camera_test.py --info

  # 한 장 찍어서 저장
  python3 camera_test.py --snap out.jpg

  # 브라우저로 실시간 확인 (Ctrl+C로 종료)
  python3 camera_test.py --stream
  #   → 브라우저에서 http://<보드IP>:8090 접속

  # Lepton Y16 raw 모드 (열화상용)
  python3 camera_test.py --stream --raw

의존성:
    sudo apt install -y python3-opencv
    또는
    pip3 install opencv-python-headless numpy
"""

from __future__ import annotations

import argparse
import glob
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import cv2
    import numpy as np
except ImportError:
    print("오류: OpenCV가 없습니다.")
    print("  sudo apt install -y python3-opencv")
    sys.exit(1)


# ----------------------------------------------------------------------
# 정보 조회
# ----------------------------------------------------------------------

def cmd_info() -> int:
    devices = sorted(glob.glob("/dev/video*"))
    if not devices:
        print("/dev/video* 장치가 없습니다. 카메라가 인식되지 않았습니다.")
        print()
        print("확인할 것:")
        print("  lsusb                    # USB 목록에 보이는가")
        print("  dmesg | tail -30         # 연결 시 커널 메시지")
        print("  다른 USB 포트에 꽂아보기 (전력 부족일 수 있음)")
        return 1

    print(f"발견된 장치: {', '.join(devices)}")
    print()

    for dev in devices:
        print(f"── {dev} " + "─" * 40)
        try:
            out = subprocess.run(
                ["v4l2-ctl", "-d", dev, "--list-formats-ext"],
                capture_output=True, text=True, timeout=5,
            )
            text = out.stdout.strip()
            print(text if text else "  (포맷 정보 없음 — 메타데이터 전용 노드일 수 있음)")
        except FileNotFoundError:
            print("  v4l2-ctl 없음:  sudo apt install -y v4l-utils")
            break
        except subprocess.SubprocessError as exc:
            print(f"  조회 실패: {exc}")
        print()

    print("참고: Lepton(PureThermal)이라면 'Y16' 포맷이 보여야 raw 열화상을 쓸 수 있습니다.")
    return 0


# ----------------------------------------------------------------------
# 카메라 열기
# ----------------------------------------------------------------------

def open_camera(index: int, width: int, height: int, raw: bool) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(f"/dev/video{index} 를 열 수 없습니다")

    if raw:
        # Lepton Y16: 16비트 그레이스케일 원본.
        # CONVERT_RGB를 꺼야 드라이버가 임의로 변환하지 않는다.
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"Y16 "))
    else:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    return cap


def fix_frame(frame: "np.ndarray") -> "np.ndarray":
    """1차원 버퍼로 온 프레임을 (H, W) 이미지로 되돌린다.

    PureThermal + V4L2 조합에서 CONVERT_RGB=0 으로 캡처하면 OpenCV 가
    프레임을 (1, N) 평면 버퍼로 넘기는 경우가 있다. 그대로 두면 이후
    letterbox/resize 코드가 N 을 한 변으로 착각해 수만 픽셀짜리 이미지를
    만들려다 메모리를 터뜨린다(프로세스가 Killed 됨).

    Lepton 3 은 160x120 이며, Y16 에는 160x122 변형도 있다. 아래 2줄은
    영상이 아니라 텔레메트리(센서 온도·FFC 상태)이므로 잘라낸다.
    """
    a = np.asarray(frame)

    # 이미 정상적인 2D/3D 이미지면 그대로 (단 122행이면 텔레메트리 제거)
    if a.ndim >= 2 and a.shape[0] not in (0, 1):
        return a[:120] if a.shape[0] == 122 else a

    flat = a.reshape(-1)

    # uint8 로 왔지만 실제로는 16비트 데이터인 경우 재해석
    if flat.dtype == np.uint8 and flat.size in (160 * 120 * 2, 160 * 122 * 2):
        flat = flat.view(np.uint16)

    n = flat.size
    for h, w in ((120, 160), (122, 160)):
        if n == h * w:
            img = flat.reshape(h, w)
            return img[:120] if h == 122 else img
    for h, w, c in ((120, 160, 3), (122, 160, 3)):
        if n == h * w * c:
            img = flat.reshape(h, w, c)
            return img[:120] if h == 122 else img

    return a


def normalize_raw(frame: "np.ndarray") -> "np.ndarray":
    """16비트 열화상 원본을 눈으로 볼 수 있게 8비트로 변환한다.

    Lepton 3.0은 non-radiometric이라 절대온도가 없다. 프레임마다 최소/최대로
    정규화하면 대비는 좋지만 프레임 간 밝기 기준이 흔들린다는 점에 유의.
    (실제 감지 로직에서는 정규화 전 원본값을 써야 한다.)
    """
    lo, hi = int(frame.min()), int(frame.max())
    if hi <= lo:
        return np.zeros(frame.shape, dtype=np.uint8)
    scaled = ((frame.astype(np.float32) - lo) * (255.0 / (hi - lo)))
    return scaled.astype(np.uint8)


def prepare_for_display(frame: "np.ndarray", raw: bool) -> "np.ndarray":
    frame = fix_frame(frame)
    if not raw:
        return frame
    gray8 = normalize_raw(frame)
    color = cv2.applyColorMap(gray8, cv2.COLORMAP_INFERNO)
    # 160x120은 브라우저에서 너무 작다
    return cv2.resize(color, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)


# ----------------------------------------------------------------------
# 스냅샷
# ----------------------------------------------------------------------

def cmd_snap(args) -> int:
    cap = open_camera(args.device, args.width, args.height, args.raw)
    try:
        # 첫 몇 프레임은 노출이 안정되지 않아 버린다
        for _ in range(5):
            cap.read()
            time.sleep(0.1)

        ok, frame = cap.read()
        if not ok or frame is None:
            print("프레임을 읽지 못했습니다.")
            print("  --raw 옵션 유무를 바꿔보거나, --device 번호를 바꿔보세요.")
            return 1

        print(f"프레임 수신: shape={frame.shape}, dtype={frame.dtype}")
        if args.raw:
            print(f"  값 범위: {frame.min()} ~ {frame.max()}")

        cv2.imwrite(args.snap, prepare_for_display(frame, args.raw))
        print(f"저장됨: {args.snap}")
        print()
        print(f"PC에서 확인:  scp {args.user}@<보드IP>:~/{args.snap} .")
        return 0
    finally:
        cap.release()


# ----------------------------------------------------------------------
# MJPEG 스트리밍
# ----------------------------------------------------------------------

class _FrameHub:
    """최신 프레임 한 장만 들고 있는다. 느린 클라이언트가 있어도 지연이 쌓이지 않는다."""

    def __init__(self) -> None:
        self._jpeg: bytes | None = None
        self._lock = threading.Lock()
        self.frame_count = 0

    def put(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg = jpeg
            self.frame_count += 1

    def get(self) -> bytes | None:
        with self._lock:
            return self._jpeg


_hub = _FrameHub()

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>camera test</title>
<style>
  body{margin:0;background:#111;color:#ddd;font-family:system-ui,sans-serif;
       display:flex;flex-direction:column;align-items:center;gap:12px;padding:16px}
  img{max-width:100%;border-radius:8px;background:#000}
  p{font-size:13px;color:#888;margin:0}
</style></head>
<body>
  <img src="/stream">
  <p>새로고침하면 재연결됩니다. 종료는 보드에서 Ctrl+C.</p>
</body></html>""".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)
            return

        if self.path != "/stream":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame"
        )
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            while True:
                jpeg = _hub.get()
                if jpeg is None:
                    time.sleep(0.05)
                    continue
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 브라우저를 닫으면 정상적으로 발생한다

    def log_message(self, *args):
        pass  # 접근 로그로 콘솔을 더럽히지 않는다


def cmd_stream(args) -> int:
    cap = open_camera(args.device, args.width, args.height, args.raw)

    server = HTTPServer(("0.0.0.0", args.port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print()
    print(f"  브라우저에서 열기:  http://<보드IP>:{args.port}")
    print(f"  보드 IP 확인:       hostname -I")
    print()
    print("  종료: Ctrl+C")
    print()

    fail_streak = 0
    last_report = time.monotonic()

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                fail_streak += 1
                if fail_streak == 1:
                    print("경고: 프레임 읽기 실패. 재시도 중...")
                if fail_streak > 100:
                    print("오류: 프레임을 계속 못 읽습니다. 카메라 연결을 확인하세요.")
                    return 1
                time.sleep(0.05)
                continue
            fail_streak = 0

            display = prepare_for_display(frame, args.raw)
            ok, buf = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                _hub.put(buf.tobytes())

            now = time.monotonic()
            if now - last_report >= 5.0:
                fps = _hub.frame_count / (now - last_report)
                print(f"  {fps:.1f} fps")
                _hub.frame_count = 0
                last_report = now

    except KeyboardInterrupt:
        print("\n종료합니다.")
        return 0
    finally:
        cap.release()
        server.shutdown()


# ----------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="카메라 연결 테스트 (UVC 웹캠 / PureThermal)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--info", action="store_true", help="장치와 지원 포맷 조회")
    p.add_argument("--snap", metavar="FILE", help="한 장 찍어 파일로 저장")
    p.add_argument("--stream", action="store_true", help="브라우저로 실시간 스트리밍")
    p.add_argument("-d", "--device", type=int, default=0, help="/dev/videoN 의 N (기본 0)")
    p.add_argument("--raw", action="store_true", help="Y16 원본 모드 (Lepton 열화상용)")
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=480)
    p.add_argument("--port", type=int, default=8090, help="스트리밍 포트 (기본 8090)")
    p.add_argument("--user", default="sunrise", help="scp 안내 문구에 쓸 계정명")

    args = p.parse_args()

    if args.info:
        return cmd_info()
    if args.snap:
        return cmd_snap(args)
    if args.stream:
        return cmd_stream(args)

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
