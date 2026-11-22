"""Summarize Betaflight blackbox logs: flight mode, hover throttle, stick bias
(drift compensation), attitude, motor balance, battery sag, gyro noise and
low-frequency bounce.

Usage:
    python tools/analyze.py logs/                 # every btfl_*.bbl in a folder
    python tools/analyze.py logs/btfl_028.bbl     # specific files

Assumes Betaflight QuadX motor order: 0 rear-right, 1 front-right,
2 rear-left, 3 front-left.
"""
import sys, glob, os, math
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbdecode import decode

FEAT = {3: 'RX_SERIAL', 4: 'MOTOR_STOP', 7: 'GPS', 10: 'TELEMETRY', 18: 'OSD',
        22: 'AIRMODE', 28: 'ANTI_GRAVITY', 29: 'DYN_FILTER'}
KEYS = ['thr_mid', 'thr_expo', 'throttle_limit_percent', 'rollPID', 'pitchPID',
        'levelPID', 'angle_limit', 'simplified_master_multiplier', 'motor_idle',
        'P interval', 'debug_mode', 'dshot_bidir']
DISARM = {0: 'arming disabled', 1: 'failsafe', 2: 'throttle timeout', 3: 'sticks',
          4: 'switch', 5: 'CRASH PROTECTION', 6: 'RUNAWAY TAKEOFF', 7: 'gps rescue',
          8: 'serial cmd', 9: 'landing'}
MOTOR_MIN, MOTOR_MAX = 158, 2047  # overwritten from header motorOutput


def log_files(args):
    out = []
    for a in args:
        if os.path.isdir(a):
            out += sorted(f for f in glob.glob(os.path.join(a, '*.bbl'))
                          if not os.path.basename(f).lower().endswith('_all.bbl'))
        else:
            out.append(a)
    return out


def summarize(f, prevcfg):
    hdr, names, arr, _, slow, ev, _ = decode(f)
    name = os.path.basename(f)
    cfg = {k: hdr.get(k) for k in KEYS}
    feats = int(hdr.get('features', '0'))
    cfg['features'] = ','.join(v for b, v in FEAT.items() if feats >> b & 1)
    if cfg != prevcfg:
        print(f'== config from {name}: {cfg}')
    if len(arr) < 50:
        print(f'{name}: {len(arr)} frames (empty)')
        return cfg

    mlo, mhi = (int(x) for x in hdr.get('motorOutput', f'{MOTOR_MIN},{MOTOR_MAX}').split(','))
    ix = {n: i for i, n in enumerate(names)}
    col = lambda n: arr[:, ix[n]].astype(float)
    t = (col('time') - col('time')[0]) / 1e6
    dt = np.median(np.diff(t))
    mot = np.stack([col(f'motor[{i}]') for i in range(4)], 1)
    mot_pct = (mot - mlo) / (mhi - mlo) * 100
    thr = col('rcCommand[3]')
    rcr, rcp, rcy = col('rcCommand[0]'), col('rcCommand[1]'), col('rcCommand[2]')
    gy = np.stack([col(f'gyroADC[{i}]') for i in range(3)], 1)
    gu = np.stack([col(f'gyroUnfilt[{i}]') for i in range(3)], 1)
    q = np.stack([col(f'imuQuaternion[{i}]') for i in range(3)], 1) / 0x7FFF
    w = np.sqrt(np.clip(1 - (q ** 2).sum(1), 0, 1))
    x, y, z = q.T
    roll = np.degrees(np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    pitch = np.degrees(np.arcsin(np.clip(2 * (w * y - z * x), -1, 1)))
    acc = np.stack([col(f'accSmooth[{i}]') for i in range(3)], 1) / int(hdr.get('acc_1G', 2048))
    vb = col('vbatLatest') / 100

    # S-frame flightModeFlags = rcModeActivationMask: bit 0 ARM, bit 1 ANGLE
    sidx = arr[:, -1]
    modes = np.zeros(len(arr), dtype=np.int64)
    if len(slow):
        ok = sidx >= 0
        modes[ok] = slow[sidx[ok], 0]
    angle_frac = np.mean(modes & 2 > 0) * 100

    avgmot = mot_pct.mean(1)
    air = avgmot > 15  # rough airborne mask
    airtime = air.sum() * dt
    line = (f'{name}: {t[-1]:5.1f}s, airborne~{airtime:5.1f}s, ANGLE {angle_frac:3.0f}%, '
            f'battery {vb[:20].mean():.2f}V at arm, min {vb.min():.2f}V')
    dis = [DISARM.get(e[1], e[1]) for e in ev if e[0] == 'disarm']
    if dis:
        line += f', disarm: {", ".join(map(str, dis))}'
    print(line)
    if airtime < 1.5:
        return cfg

    a = air
    mm = mot_pct[a].mean(0)
    rl = (mm[0] + mm[1]) / 2 - (mm[2] + mm[3]) / 2
    fr = (mm[1] + mm[3]) / 2 - (mm[0] + mm[2]) / 2
    dg = (mm[0] + mm[3]) / 2 - (mm[1] + mm[2]) / 2
    print(f'   motors % RR {mm[0]:.1f} FR {mm[1]:.1f} RL {mm[2]:.1f} FL {mm[3]:.1f} | '
          f'right-left {rl:+.1f}  front-rear {fr:+.1f}  yaw diagonal {dg:+.1f} | '
          f'saturated {np.mean(mot_pct[a].max(1) > 95) * 100:.1f}% of airtime')
    print(f'   throttle stick median {np.median(thr[a]):.0f}; stick bias roll {rcr[a].mean():+.1f} '
          f'pitch {rcp[a].mean():+.1f} yaw {rcy[a].mean():+.1f} (of +/-500)')
    print(f'   attitude mean roll {roll[a].mean():+.1f} pitch {pitch[a].mean():+.1f} deg; '
          f'on ground at arm: roll {roll[:200].mean():+.1f} pitch {pitch[:200].mean():+.1f} deg')
    for lo, hi in [(15, 30), (30, 50), (50, 101)]:
        m = (avgmot >= lo) & (avgmot < hi)
        if m.sum() > 200:
            print(f'   battery at {lo}-{hi}% motor: median {np.median(vb[m]):.2f}V')
    print(f'   gyro RMS unfiltered r/p/y {np.sqrt((gu[a] ** 2).mean(0)).round(1)}  '
          f'filtered {np.sqrt((gy[a] ** 2).mean(0)).round(1)} deg/s')
    seg = gu[a][:, :2] - gu[a][:, :2].mean(0)
    if len(seg) > 1024:
        N = 1 << int(math.log2(len(seg)))
        win = np.hanning(N)
        spec = np.abs(np.fft.rfft(seg[:N] * win[:, None], axis=0)).sum(1)
        f_ = np.fft.rfftfreq(N, dt)
        lo, hi = (f_ > 1) & (f_ < 30), f_ > 50
        az = acc[a, 2][:N] - acc[a, 2][:N].mean()
        sp = np.abs(np.fft.rfft(az * win))
        print(f'   gyro peak 1-30 Hz {f_[lo][spec[lo].argmax()]:.1f} Hz, >50 Hz {f_[hi][spec[hi].argmax()]:.0f} Hz; '
              f'vertical bounce sd {acc[a, 2].std():.2f} g @ {f_[lo][sp[lo].argmax()]:.1f} Hz; '
              f'throttle stick sd {thr[a].std():.0f}')
    return cfg


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cfg = None
    for f in log_files(sys.argv[1:]):
        cfg = summarize(f, cfg)
