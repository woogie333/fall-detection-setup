#!/usr/bin/env python3
"""
LD2450 연결 테스트 및 실시간 확인.

사용법:

  # 포트 찾기
  python3 ld2450_test.py --list

  # 터미널에 좌표 출력
  python3 ld2450_test.py -p /dev/ttyUSB0

  # 브라우저에서 평면도로 보기 (권장 — 좌표가 맞는지 눈으로 확인)
  python3 ld2450_test.py -p /dev/ttyUSB0 --web
  #   → http://<보드IP>:8091

  # 원시 바이트 확인 (통신이 되는지조차 의심스러울 때)
  python3 ld2450_test.py -p /dev/ttyUSB0 --raw

  # CSV 로 기록 (나중에 임계값 보정용)
  python3 ld2450_test.py -p /dev/ttyUSB0 --csv ld2450_log.csv

의존성:
    pip install pyserial
    sudo apt install -y python3-opencv     # --web 사용 시
"""
from __future__ import annotations

import argparse
import glob
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from ld2450 import BAUD, HEADER, Frame, open_serial, read_frames

# --------------------------------------------------------------- 포트 찾기

def list_ports() -> int:
    cands = sorted(
        glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
        + glob.glob("/dev/ttyS[0-9]") + glob.glob("/dev/ttyAMA*")
    )
    if not cands:
        print("시리얼 포트를 찾지 못했습니다.")
        print()
        print("USB-TTL 어댑터를 쓴다면:")
        print("  lsusb                 # 어댑터가 보이는지")
        print("  dmesg | tail -20      # 꽂았을 때 커널 메시지")
        print()
        print("보드 GPIO UART 를 쓴다면 /dev/ttyS* 가 이미 있어야 합니다.")
        return 1

    print("발견된 포트:")
    for c in cands:
        print(f"  {c}")
    print()
    print("USB-TTL 어댑터는 보통 /dev/ttyUSB0 입니다.")
    print("권한 오류가 나면:  sudo usermod -aG dialout $USER   (재로그인 필요)")
    return 0


# --------------------------------------------------------------- 원시 확인

def cmd_raw(port: str, baud: int, seconds: float) -> int:
    """디코딩 없이 바이트만 본다. 배선·속도 확인용."""
    ser = open_serial(port, baud)
    print(f"{port} @ {baud} — {seconds}초간 원시 데이터 수신\n")
    end = time.time() + seconds
    total = 0
    headers = 0
    while time.time() < end:
        b = ser.read(64)
        if not b:
            continue
        total += len(b)
        headers += b.count(HEADER)
        print(b.hex(" "), flush=True)
    ser.close()

    print()
    print(f"총 {total} bytes, 헤더(AA FF 03 00) {headers}회 발견")
    if total == 0:
        print("  → 데이터가 전혀 없습니다. TX/RX 가 바뀌었거나 전원이 없습니다.")
    elif headers == 0:
        print("  → 데이터는 오는데 헤더가 없습니다. baud 가 틀렸을 가능성이 큽니다.")
    else:
        print("  → 정상입니다.")
    return 0


# --------------------------------------------------------------- 웹 평면도

class Hub:
    def __init__(self):
        self.jpeg = None
        self.lock = threading.Lock()

    def put(self, img):
        import cv2
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()

    def get(self):
        with self.lock:
            return self.jpeg


HUB = Hub()

_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LD2450</title><style>
body{margin:0;background:#111;color:#999;font-family:system-ui,sans-serif;
     display:flex;flex-direction:column;align-items:center;gap:12px;padding:16px}
img{max-width:100%;width:520px;border-radius:10px;background:#000}
p{font-size:13px;margin:0;color:#777}</style></head><body>
<img src="/stream"><p>센서는 아래쪽 중앙. 위로 갈수록 멀어집니다. 종료는 Ctrl+C.</p>
</body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
        self.end_headers()
        try:
            while True:
                j = HUB.get()
                if j is None:
                    time.sleep(0.05)
                    continue
                self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + j + b"\r\n")
                time.sleep(0.08)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def render(frame: Frame, size=520, max_mm=6000):
    """위에서 내려다본 평면도. 센서는 하단 중앙."""
    import cv2
    import numpy as np

    img = np.zeros((size, size, 3), np.uint8)
    cx, cy = size // 2, size - 30
    px_per_mm = (size - 60) / max_mm

    # 거리 링 (1m 간격)
    for m in range(1, max_mm // 1000 + 1):
        r = int(m * 1000 * px_per_mm)
        cv2.circle(img, (cx, cy), r, (45, 45, 45), 1)
        cv2.putText(img, f"{m}m", (cx + 4, cy - r + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (90, 90, 90), 1)

    # 화각 ±60°
    import math
    for ang in (-60, 60):
        a = math.radians(ang)
        ex = int(cx + math.sin(a) * (size - 60))
        ey = int(cy - math.cos(a) * (size - 60))
        cv2.line(img, (cx, cy), (ex, ey), (40, 40, 40), 1)

    cv2.circle(img, (cx, cy), 5, (200, 200, 200), -1)

    colors = [(90, 200, 255), (140, 255, 140), (255, 160, 120)]
    for i, t in enumerate(frame.targets):
        px = int(cx + t.x_mm * px_per_mm)
        py = int(cy - t.y_mm * px_per_mm)
        if not (0 <= px < size and 0 <= py < size):
            continue
        c = colors[i % 3]
        cv2.circle(img, (px, py), 9, c, -1)
        cv2.circle(img, (px, py), 16, c, 1)
        cv2.putText(img, f"{t.distance_mm/1000:.2f}m {t.speed_cms:+d}cm/s",
                    (px + 20, py + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.42, c, 1)

    cv2.putText(img, f"targets: {frame.count}", (12, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 220, 220), 1)
    return img


# --------------------------------------------------------------- 메인 루프

def run(port: str, baud: int, web: bool, web_port: int, csv_path: str | None) -> int:
    ser = open_serial(port, baud)

    csv_f = None
    if csv_path:
        csv_f = open(csv_path, "w", encoding="utf-8")
        csv_f.write("t,idx,x_mm,y_mm,speed_cms\n")
        print(f"CSV 기록: {csv_path}")

    if web:
        srv = HTTPServer(("0.0.0.0", web_port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"\n  브라우저:  http://<보드IP>:{web_port}\n")

    print("수신 중 (Ctrl+C 종료)\n")
    n = 0
    t0 = time.time()
    last_print = 0.0
    empty_streak = 0

    try:
        for frame in read_frames(ser):
            n += 1

            if csv_f:
                for i, t in enumerate(frame.targets):
                    csv_f.write(f"{frame.t:.3f},{i},{t.x_mm},{t.y_mm},{t.speed_cms}\n")

            if web:
                HUB.put(render(frame))

            if frame.count == 0:
                empty_streak += 1
            else:
                empty_streak = 0

            now = time.time()
            if now - last_print >= 0.5:
                last_print = now
                if frame.count == 0:
                    print(f"  [{n:5d}] 감지 없음 ({empty_streak}프레임 연속)")
                else:
                    parts = [
                        f"#{i} x={t.x_mm:+5d}mm y={t.y_mm:5d}mm "
                        f"({t.distance_mm/1000:.2f}m, {t.angle_deg:+.0f}°) "
                        f"v={t.speed_cms:+4d}cm/s"
                        for i, t in enumerate(frame.targets)
                    ]
                    print(f"  [{n:5d}] " + " | ".join(parts))

    except KeyboardInterrupt:
        dt = time.time() - t0
        print(f"\n\n{n} 프레임 / {dt:.1f}초 = {n/max(dt,1e-6):.1f} fps  (정상값 ≈ 10)")
    finally:
        ser.close()
        if csv_f:
            csv_f.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="LD2450 mmWave 레이더 테스트",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("-p", "--port", help="시리얼 포트 (예: /dev/ttyUSB0)")
    ap.add_argument("-b", "--baud", type=int, default=BAUD)
    ap.add_argument("--list", action="store_true", help="사용 가능한 포트 조회")
    ap.add_argument("--raw", action="store_true", help="원시 바이트 확인 (배선 점검용)")
    ap.add_argument("--raw-seconds", type=float, default=3.0)
    ap.add_argument("--web", action="store_true", help="브라우저 평면도")
    ap.add_argument("--web-port", type=int, default=8091)
    ap.add_argument("--csv", help="좌표를 CSV 로 기록")
    args = ap.parse_args()

    if args.list:
        return list_ports()
    if not args.port:
        print("포트를 지정하세요.  --list 로 확인할 수 있습니다.")
        return 1
    if args.raw:
        return cmd_raw(args.port, args.baud, args.raw_seconds)
    return run(args.port, args.baud, args.web, args.web_port, args.csv)


if __name__ == "__main__":
    sys.exit(main())
