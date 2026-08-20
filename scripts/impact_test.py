#!/usr/bin/env python3
"""
진동센서 노드 테스트 — RDK X3 에서 ESP-NOW 수신기를 읽는다.

수신기 XIAO 를 USB 로 꽂으면 /dev/ttyACM0 으로 잡힌다.

사용법:

  python3 impact_test.py                    # 자동으로 포트 찾기
  python3 impact_test.py -p /dev/ttyACM0
  python3 impact_test.py --csv impacts.csv  # 기록
  python3 impact_test.py --raw              # 수신기 출력을 그대로 (MAC 확인용)

의존성:  pip install pyserial
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
import time
from dataclasses import dataclass
from typing import Iterator, Optional

LINE_RE = re.compile(
    r"IMPACT\s+device=(?P<dev>\S+)\s+seq=(?P<seq>\d+)\s+"
    r"peak=(?P<peak>[\d.]+)\s+dur=(?P<dur>[\d.]+)\s+"
    r"rssi=(?P<rssi>-?\d+)\s+batt=(?P<batt>\d+)"
)


@dataclass
class Impact:
    device: str
    seq: int
    peak_g: float
    duration_ms: float
    rssi: int
    battery_mv: int
    t: float


def find_port() -> Optional[str]:
    cands = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    return cands[0] if cands else None


def read_impacts(ser, echo_comments: bool = True) -> Iterator[Impact]:
    while True:
        try:
            raw = ser.readline()
        except Exception as exc:
            print(f"시리얼 읽기 오류: {exc}")
            return
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue

        if line.startswith("#"):
            if echo_comments:
                print(f"  \033[2m{line}\033[0m")
            continue

        m = LINE_RE.search(line)
        if not m:
            print(f"  ? {line}")
            continue

        yield Impact(
            device=m.group("dev"),
            seq=int(m.group("seq")),
            peak_g=float(m.group("peak")),
            duration_ms=float(m.group("dur")),
            rssi=int(m.group("rssi")),
            battery_mv=int(m.group("batt")),
            t=time.time(),
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ESP-NOW 진동센서 수신 테스트",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("-p", "--port", help="시리얼 포트 (기본: 자동 탐색)")
    ap.add_argument("-b", "--baud", type=int, default=115200)
    ap.add_argument("--csv", help="충격 이벤트를 CSV 로 기록")
    ap.add_argument("--raw", action="store_true",
                    help="수신기 출력을 그대로 표시 (MAC 주소 확인용)")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("시리얼 포트를 찾지 못했습니다.")
        print()
        print("확인할 것:")
        print("  ls /dev/ttyACM* /dev/ttyUSB*")
        print("  lsusb                      # 수신기 XIAO 가 보이는지")
        print("  dmesg | tail -20           # 꽂았을 때 커널 메시지")
        print("  sudo usermod -aG dialout $USER   # 권한 (재로그인 필요)")
        return 1

    try:
        import serial
    except ImportError:
        print("pyserial 이 없습니다:  pip install pyserial")
        return 1

    try:
        ser = serial.Serial(port, args.baud, timeout=1)
    except OSError as exc:
        print(f"{port} 를 열지 못했습니다: {exc}")
        print("  권한 문제라면:  sudo usermod -aG dialout $USER  (재로그인)")
        return 1

    print(f"\n  {port} @ {args.baud}")
    print("  수신 대기 중 (Ctrl+C 종료)\n")

    if args.raw:
        try:
            while True:
                line = ser.readline().decode("utf-8", "replace").rstrip()
                if line:
                    print(f"  {line}")
        except KeyboardInterrupt:
            print()
            return 0
        finally:
            ser.close()

    csv_f = None
    if args.csv:
        csv_f = open(args.csv, "w", encoding="utf-8")
        csv_f.write("unix_time,device,seq,peak_g,duration_ms,rssi,battery_mv\n")
        print(f"  CSV 기록: {args.csv}\n")

    n = 0
    try:
        for imp in read_impacts(ser):
            n += 1
            ts = time.strftime("%H:%M:%S", time.localtime(imp.t))
            bar = "█" * min(int(imp.peak_g * 4), 40)
            print(f"  [{ts}] {imp.device} #{imp.seq}  "
                  f"peak={imp.peak_g:5.2f}g  dur={imp.duration_ms:4.0f}ms  "
                  f"rssi={imp.rssi}dBm  {bar}")

            if csv_f:
                csv_f.write(f"{imp.t:.3f},{imp.device},{imp.seq},{imp.peak_g},"
                            f"{imp.duration_ms},{imp.rssi},{imp.battery_mv}\n")
                csv_f.flush()

    except KeyboardInterrupt:
        print(f"\n\n  충격 이벤트 {n}건 수신")
        print()
        print("  참고 — 구분해서 기록해두면 임계값 정하기가 쉽습니다:")
        print("    발소리 / 문 닫힘     : 보통 1.5g 미만, 짧음")
        print("    물건 떨어뜨림        : 크지만 그 뒤 사람 움직임이 이어짐")
        print("    사람이 넘어짐        : 크고, 그 뒤 정지가 오래 지속")
    finally:
        ser.close()
        if csv_f:
            csv_f.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
