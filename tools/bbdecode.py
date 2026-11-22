"""Minimal Betaflight blackbox (.bbl) decoder (log data version 2).

Independent implementation of the blackbox log format: header parsing,
I/P/S/E frames, all field encodings and predictors Betaflight 2026.x emits.
GPS (G/H) frames are skipped.

Usage:
    python bbdecode.py <file.bbl> [<out.npz>]

As a module:
    hdr, names, arr, slow_names, slow, events, corrupt = decode(path)
    arr: int64 array, one row per main frame, columns = names (+ last column
         = index of the most recent slow frame, for flight-mode lookup).
"""
import sys, struct
import numpy as np

FRAME_TYPES = b'IPSGHE'


def sx(v, bits):
    m = 1 << (bits - 1)
    v &= (1 << bits) - 1
    return (v ^ m) - m


class Rd:
    __slots__ = ('d', 'p', 'n')

    def __init__(self, d, p):
        self.d, self.p, self.n = d, p, len(d)

    def byte(self):
        if self.p >= self.n:
            raise EOFError
        b = self.d[self.p]
        self.p += 1
        return b

    def uvb(self):
        res = 0
        shift = 0
        for _ in range(5):
            b = self.byte()
            res |= (b & 0x7F) << shift
            if not b & 0x80:
                return res
            shift += 7
        raise ValueError('bad varint')

    def svb(self):
        u = self.uvb()
        return (u >> 1) ^ -(u & 1)

    def tag2_3s32(self):
        lead = self.byte()
        sel = lead >> 6
        if sel == 0:
            return [sx(lead >> 4, 2), sx(lead >> 2, 2), sx(lead, 2)]
        if sel == 1:
            v0 = sx(lead, 4)
            b = self.byte()
            return [v0, sx(b >> 4, 4), sx(b, 4)]
        if sel == 2:
            v0 = sx(lead, 6)
            v1 = sx(self.byte(), 6)
            v2 = sx(self.byte(), 6)
            return [v0, v1, v2]
        out = []
        s = lead
        for _ in range(3):
            k = s & 3
            if k == 0:
                out.append(sx(self.byte(), 8))
            elif k == 1:
                b1 = self.byte(); b2 = self.byte()
                out.append(sx(b1 | (b2 << 8), 16))
            elif k == 2:
                b1 = self.byte(); b2 = self.byte(); b3 = self.byte()
                out.append(sx(b1 | (b2 << 8) | (b3 << 16), 24))
            else:
                b1 = self.byte(); b2 = self.byte(); b3 = self.byte(); b4 = self.byte()
                out.append(sx(b1 | (b2 << 8) | (b3 << 16) | (b4 << 24), 32))
            s >>= 2
        return out

    def tag8_4s16(self):
        sel = self.byte()
        nib = 0
        buf = 0
        out = []
        for _ in range(4):
            k = sel & 3
            if k == 0:
                out.append(0)
            elif k == 1:
                if nib == 0:
                    buf = self.byte()
                    out.append(sx(buf >> 4, 4))
                    nib = 1
                else:
                    out.append(sx(buf & 0xF, 4))
                    nib = 0
            elif k == 2:
                if nib == 0:
                    out.append(sx(self.byte(), 8))
                else:
                    c1 = (buf << 4) & 0xFF
                    buf = self.byte()
                    c1 |= buf >> 4
                    out.append(sx(c1, 8))
            else:
                if nib == 0:
                    c1 = self.byte(); c2 = self.byte()
                    out.append(sx((c1 << 8) | c2, 16))
                else:
                    c1 = self.byte(); c2 = self.byte()
                    out.append(sx(((buf & 0xF) << 12) | (c1 << 4) | (c2 >> 4), 16))
                    buf = c2
            sel >>= 2
        return out

    def tag8_8svb(self, count):
        if count == 1:
            return [self.svb()]
        hdr = self.byte()
        out = []
        for _ in range(count):
            out.append(self.svb() if hdr & 1 else 0)
            hdr >>= 1
        return out


def read_raw(rd, encs):
    n = len(encs)
    vals = [0] * n
    i = 0
    while i < n:
        e = encs[i]
        if e == 0:
            vals[i] = rd.svb(); i += 1
        elif e == 1:
            vals[i] = rd.uvb(); i += 1
        elif e == 3:
            vals[i] = -sx(rd.uvb() & 0x3FFF, 14); i += 1
        elif e == 9:
            i += 1
        elif e == 6:
            j = i + 1
            while j < n and j < i + 8 and encs[j] == 6:
                j += 1
            v = rd.tag8_8svb(j - i)
            vals[i:j] = v
            i = j
        elif e == 7:
            v = rd.tag2_3s32()
            for k in range(3):
                if i + k < n:
                    vals[i + k] = v[k]
            i += 3
        elif e == 8:
            v = rd.tag8_4s16()
            for k in range(4):
                if i + k < n:
                    vals[i + k] = v[k]
            i += 4
        else:
            raise ValueError('unsupported encoding %d' % e)
    return vals


def parse_header(data):
    hdr = {}
    p = 0
    while data.startswith(b'H ', p):
        e = data.index(b'\n', p)
        line = data[p + 2:e].decode('latin1')
        if ':' in line:
            k, v = line.split(':', 1)
            hdr[k] = v
        p = e + 1
    return hdr, p


def ints(s):
    return [int(x) for x in s.split(',')] if s else []


def decode(path):
    data = open(path, 'rb').read()
    hdr, start = parse_header(data)
    names = hdr['Field I name'].split(',')
    n = len(names)
    Ipred = ints(hdr['Field I predictor']); Ienc = ints(hdr['Field I encoding'])
    Ppred = ints(hdr['Field P predictor']); Penc = ints(hdr['Field P encoding'])
    Snames = hdr.get('Field S name', '').split(',') if 'Field S name' in hdr else []
    Senc = ints(hdr.get('Field S encoding', ''))
    Gnames = hdr.get('Field G name', '').split(',') if 'Field G name' in hdr else []
    Genc = ints(hdr.get('Field G encoding', '')); Gpred = ints(hdr.get('Field G predictor', ''))
    Henc = ints(hdr.get('Field H encoding', ''))
    minmotor = ints(hdr.get('motorOutput', '0'))[0]
    vbatref = int(hdr.get('vbatref', '0'))
    minthr = int(hdr.get('minthrottle', '0'))
    pint = int(hdr.get('P interval', '1').split('/')[-1]) if hdr.get('P interval') else 1
    m0 = names.index('motor[0]') if 'motor[0]' in names else -1

    rows = []
    slow_t = []  # (time, values)
    events = []
    prev = prev2 = None
    last_slow = [0] * len(Snames)
    rd = Rd(data, start)
    corrupt = 0

    def predict(raw, preds, cur_prev, cur_prev2, is_I):
        out = [0] * n
        for i in range(n):
            pr = preds[i]
            r = raw[i]
            if pr == 0:
                v = r
            elif pr == 1:
                v = r + (cur_prev[i] if cur_prev else 0)
            elif pr == 2:
                v = r + (2 * cur_prev[i] - cur_prev2[i] if cur_prev else 0)
            elif pr == 3:
                if cur_prev:
                    s = cur_prev[i] + cur_prev2[i]
                    a = abs(s) // 2
                    v = r + (a if s >= 0 else -a)
                else:
                    v = r
            elif pr == 4:
                v = r + minthr
            elif pr == 5:
                v = r + out[m0]
            elif pr == 6:
                v = (cur_prev[i] if cur_prev else 0) + pint
            elif pr == 8:
                v = r + 1500
            elif pr == 9:
                v = r + vbatref
            elif pr == 11:
                v = r + minmotor
            else:
                v = r
            out[i] = v
        return out

    last_time = None
    while rd.p < rd.n:
        fpos = rd.p
        t = rd.d[rd.p]
        if t not in FRAME_TYPES:
            rd.p += 1
            corrupt += 1
            continue
        rd.p += 1
        try:
            if t == ord('I'):
                raw = read_raw(rd, Ienc)
                fr = predict(raw, Ipred, None, None, True)
                ok = rd.p >= rd.n or rd.d[rd.p] in FRAME_TYPES
                if ok:
                    rows.append(fr + [len(slow_t) - 1])
                    prev = prev2 = fr
                else:
                    prev = prev2 = None; rd.p = fpos + 1; corrupt += 1
            elif t == ord('P'):
                if prev is None:
                    # cannot decode without history; skip by parsing
                    read_raw(rd, Penc)
                    continue
                raw = read_raw(rd, Penc)
                fr = predict(raw, Ppred, prev, prev2, False)
                ok = rd.p >= rd.n or rd.d[rd.p] in FRAME_TYPES
                if ok:
                    rows.append(fr + [len(slow_t) - 1])
                    prev2, prev = prev, fr
                else:
                    prev = prev2 = None; rd.p = fpos + 1; corrupt += 1
            elif t == ord('S'):
                raw = read_raw(rd, Senc)
                last_slow = raw
                slow_t.append(raw)
            elif t == ord('G'):
                read_raw(rd, Genc)
            elif t == ord('H'):
                read_raw(rd, Henc)
            elif t == ord('E'):
                et = rd.byte()
                if et == 0:
                    events.append(('sync', rd.uvb()))
                elif et == 13:
                    f = rd.byte()
                    if f & 128:
                        rd.p += 4
                    else:
                        rd.svb()
                elif et == 14:
                    a = rd.uvb(); b = rd.uvb()
                    events.append(('resume', a, b)); prev = prev2 = None
                elif et == 15:
                    events.append(('disarm', rd.uvb(), rows[-1][1] if rows else 0))
                elif et == 30:
                    a = rd.uvb(); b = rd.uvb()
                    events.append(('mode', a, b, rows[-1][1] if rows else 0))
                elif et == 40:
                    events.append(('imufail', rd.uvb()))
                elif et == 255:
                    events.append(('end',))
                    break
                else:
                    rd.p = fpos + 1; corrupt += 1
        except (EOFError, ValueError, IndexError):
            if rd.p >= rd.n:
                break
            rd.p = fpos + 1
            corrupt += 1
            prev = prev2 = None
    arr = np.array(rows, dtype=np.int64) if rows else np.zeros((0, n + 1), dtype=np.int64)
    slow = np.array(slow_t, dtype=np.int64) if slow_t else np.zeros((0, len(Snames)), dtype=np.int64)
    return hdr, names, arr, Snames, slow, events, corrupt


if __name__ == '__main__':
    hdr, names, arr, Snames, slow, events, corrupt = decode(sys.argv[1])
    print('frames', arr.shape, 'slow', slow.shape, 'corrupt', corrupt, 'events', len(events))
    if len(sys.argv) > 2:
        np.savez(sys.argv[2], arr=arr, names=np.array(names), slow=slow, Snames=np.array(Snames))
