#!/usr/bin/env python3
"""
VL53L1X ToF 거리센서 테스트 (RDK X3 / Linux I2C)

두 단계로 나뉜다:

  1. 배선 확인 — 순수 I2C 로 칩 ID 만 읽는다. 라이브러리 없이도 된다.
  2. 거리 측정 — VL53L1X 라이브러리가 있으면 실제 거리를 읽는다.

배선이 맞는지부터 확인하고 넘어가는 구조라, 라이브러리 설치 문제와
배선 문제를 헷갈리지 않는다.

사용법:

  # I2C 버스에 뭐가 붙어 있는지
  python3 vl53l1x_test.py --scan

  # 배선 확인 (칩 ID 만 읽음 — 라이브러리 불필요)
  python3 vl53l1x_test.py --id

  # 거리 측정
  python3 vl53l1x_test.py
  python3 vl53l1x_test.py --mode short      # 1.3m, 밝은 곳에서 안정적
  python3 vl53l1x_test.py --mode long       # 4m, 어두운 곳에서 유리
  python3 vl53l1x_test.py --csv dist.csv

배선 (RDK X3 40핀):
    VIN → 1번 (3.3V)    ⚠ 5V 아님. 대부분의 브레이크아웃은 3.3V 로직이다
    GND → 6번 (GND)
    SDA → 3번
    SCL → 5번

의존성:
    pip install smbus2                 # 1단계 (배선 확인)
    pip install VL53L1X                # 2단계 (거리 측정)
"""
from __future__ import annotations

import argparse
import sys
import time

DEFAULT_ADDR = 0x29          # VL53L1X 기본 I2C 주소
REG_MODEL_ID = 0x010F        # 0xEA
REG_MODULE_TYPE = 0x0110     # 0xCC
REG_MASK_REV = 0x0111        # 0x10
EXPECTED_IDENT = (0xEA, 0xCC)


# ------------------------------------------------------------------ 저수준

def open_bus(bus_no: int):
    try:
        from smbus2 import SMBus
    except ImportError:
        print("smbus2 가 없습니다:  pip install smbus2")
        sys.exit(1)
    try:
        return SMBus(bus_no)
    except (FileNotFoundError, PermissionError) as exc:
        print(f"/dev/i2c-{bus_no} 를 열 수 없습니다: {exc}")
        print()
        print("확인할 것:")
        print(f"  ls /dev/i2c-*                     # 버스가 있는지")
        print(f"  sudo usermod -aG i2c $USER        # 권한 (재로그인 필요)")
        print(f"  python3 vl53l1x_test.py --scan -b 0   # 다른 버스 번호 시도")
        sys.exit(1)


def read_reg16(bus, addr: int, reg: int) -> int:
    """VL53L1X 는 16비트 레지스터 주소를 쓴다 (일반 I2C 장치와 다름)."""
    from smbus2 import i2c_msg
    w = i2c_msg.write(addr, [(reg >> 8) & 0xFF, reg & 0xFF])
    r = i2c_msg.read(addr, 1)
    bus.i2c_rdwr(w, r)
    return list(r)[0]


# ------------------------------------------------------------------ 스캔

def cmd_scan(bus_no: int) -> int:
    import glob
    buses = sorted(glob.glob("/dev/i2c-*"))
    if not buses:
        print("/dev/i2c-* 가 없습니다. I2C 가 비활성 상태일 수 있습니다.")
        return 1
    print(f"I2C 버스: {', '.join(buses)}")
    print()

    bus = open_bus(bus_no)
    print(f"버스 {bus_no} 스캔 중...")
    found = []
    for addr in range(0x08, 0x78):
        try:
            bus.write_quick(addr)
            found.append(addr)
        except OSError:
            pass
    bus.close()

    if not found:
        print("  장치 없음")
        print()
        print("  - 배선(SDA/SCL/전원)을 확인하세요")
        print("  - 다른 버스 번호를 시도하세요:  -b 0, -b 1, -b 2 ...")
        return 1

    print("  발견:", ", ".join(f"0x{a:02X}" for a in found))
    if DEFAULT_ADDR in found:
        print(f"  → 0x{DEFAULT_ADDR:02X} 가 VL53L1X 로 보입니다")
    else:
        print(f"  → 0x{DEFAULT_ADDR:02X}(VL53L1X 기본 주소)가 없습니다")
    return 0


# ------------------------------------------------------------------ ID 확인

def cmd_id(bus_no: int, addr: int) -> int:
    """라이브러리 없이 칩 ID 만 읽어 배선을 검증한다."""
    bus = open_bus(bus_no)
    print(f"버스 {bus_no}, 주소 0x{addr:02X} 에서 칩 ID 읽는 중...\n")

    try:
        model = read_reg16(bus, addr, REG_MODEL_ID)
        mtype = read_reg16(bus, addr, REG_MODULE_TYPE)
        mrev = read_reg16(bus, addr, REG_MASK_REV)
    except OSError as exc:
        print(f"읽기 실패: {exc}")
        print()
        print("  - --scan 으로 장치가 보이는지 먼저 확인하세요")
        print("  - 전원이 3.3V 인지 확인하세요")
        bus.close()
        return 1
    finally:
        try:
            bus.close()
        except Exception:
            pass

    print(f"  MODEL_ID    (0x010F) = 0x{model:02X}   (기대 0xEA)")
    print(f"  MODULE_TYPE (0x0110) = 0x{mtype:02X}   (기대 0xCC)")
    print(f"  MASK_REV    (0x0111) = 0x{mrev:02X}")
    print()

    if (model, mtype) == EXPECTED_IDENT:
        print("  ✓ VL53L1X 확인. 배선 정상입니다.")
        print()
        print("  다음: pip install VL53L1X  후  python3 vl53l1x_test.py")
        return 0

    print("  ✗ ID 가 일치하지 않습니다.")
    print("    VL53L0X 등 다른 모델이거나, 통신이 불안정합니다.")
    return 1


# ------------------------------------------------------------------ 거리 측정

MODES = {"short": 1, "medium": 2, "long": 3}


def cmd_range(bus_no: int, addr: int, mode: str, csv_path: str | None,
              interval: float) -> int:
    try:
        import VL53L1X
    except ImportError:
        print("VL53L1X 라이브러리가 없습니다.")
        print()
        print("  pip install VL53L1X")
        print()
        print("설치가 안 되면(빌드 실패 등) 배선 확인까지는 가능합니다:")
        print("  python3 vl53l1x_test.py --id")
        return 1

    tof = VL53L1X.VL53L1X(i2c_bus=bus_no, i2c_address=addr)
    try:
        tof.open()
        tof.start_ranging(MODES[mode])
    except Exception as exc:
        print(f"센서 초기화 실패: {exc}")
        print("  --id 로 배선부터 확인하세요.")
        return 1

    csv_f = None
    if csv_path:
        csv_f = open(csv_path, "w", encoding="utf-8")
        csv_f.write("unix_time,distance_mm\n")
        print(f"CSV 기록: {csv_path}")

    print(f"\n  모드: {mode}  (Ctrl+C 종료)\n")

    n = 0
    t0 = time.time()
    try:
        while True:
            d = tof.get_distance()          # mm
            n += 1
            now = time.time()

            if csv_f:
                csv_f.write(f"{now:.3f},{d}\n")

            # 4m 를 40칸으로 표시
            blen = max(0, min(int(d / 100), 40))
            bar = "█" * blen
            note = ""
            if d <= 0:
                note = "  (측정 실패 — 범위 밖이거나 반사가 약함)"
            elif d < 40:
                note = "  (너무 가까움)"
            print(f"  {d:5d} mm  {d/1000:5.2f} m  {bar}{note}")

            time.sleep(interval)
    except KeyboardInterrupt:
        dt = time.time() - t0
        print(f"\n\n  {n} 회 측정 / {dt:.1f}초 = {n/max(dt,1e-6):.1f} Hz")
    finally:
        try:
            tof.stop_ranging()
            tof.close()
        except Exception:
            pass
        if csv_f:
            csv_f.close()
    return 0


# ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="VL53L1X ToF 거리센서 테스트",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("-b", "--bus", type=int, default=1, help="I2C 버스 번호 (기본 1)")
    ap.add_argument("-a", "--addr", type=lambda x: int(x, 0), default=DEFAULT_ADDR)
    ap.add_argument("--scan", action="store_true", help="I2C 버스 스캔")
    ap.add_argument("--id", action="store_true", help="칩 ID 확인 (라이브러리 불필요)")
    ap.add_argument("--mode", choices=list(MODES), default="long")
    ap.add_argument("--interval", type=float, default=0.1, help="측정 주기(초)")
    ap.add_argument("--csv", help="거리를 CSV 로 기록")
    args = ap.parse_args()

    if args.scan:
        return cmd_scan(args.bus)
    if args.id:
        return cmd_id(args.bus, args.addr)
    return cmd_range(args.bus, args.addr, args.mode, args.csv, args.interval)


if __name__ == "__main__":
    sys.exit(main())
