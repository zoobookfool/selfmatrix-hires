#!/usr/bin/env python3
"""Reproduce the streaming compression-ratio table in
docs/stage2-compression-gateway.md sec.4.3.

Measures the gateway's own zlib/wavpack codec (codecs.py) compressing N
consecutive 128-sample stereo packets as one block, for N in {1,4,16,64}.
This is the latency-vs-ratio tradeoff that drives the sec.5.1 batch-size
choice. Synthetic signals only (silence w/ dither, speech-like, music-like,
white noise) -- NOT real-mic material; treat as relative, not absolute.

Usage:
    python3 bench_ratio.py
    GATEWAY_WAVPACK_LIB=/path/to/libwavpack.so.1 python3 bench_ratio.py

Without GATEWAY_WAVPACK_LIB the wavpack rows are skipped (zlib still runs).
On Debian/Ubuntu a shared library can be fetched without root via:
    apt-get download libwavpack1 && dpkg-deb -x libwavpack1_*.deb ./wp
    export GATEWAY_WAVPACK_LIB="$(readlink -f ./wp/usr/lib/*/libwavpack.so.*)"
"""
import math
import os
import struct
import sys
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gwcodecs", os.path.join(_HERE, "codecs.py"))
gwc = importlib.util.module_from_spec(_spec)
sys.modules["gwcodecs"] = gwc
_spec.loader.exec_module(gwc)

BLOCK, CH = 128, 2
BATCHES = [1, 4, 16, 64]
TOTAL_PACKETS = 4096
_state = 777


def _rnd():
    global _state
    _state = (_state * 1103515245 + 12345) & 0x7FFFFFFF
    return _state / 0x7FFFFFFF


def _gen(kind, n, t0, srate):
    out = []
    for i in range(n):
        t = (t0 + i) / srate
        if kind in ("silence", "noise"):
            v = (_rnd() - 0.5) * 2e-4
        elif kind == "speech":
            on = ((t0 + i) // 4096) % 2 == 0
            v = (0.1 * math.sin(2 * math.pi * 300 * t) + 0.05 * (_rnd() - 0.5)) if on else (_rnd() - 0.5) * 2e-4
        elif kind == "music":
            v = 0.2 * math.sin(2 * math.pi * 220 * t) + 0.1 * math.sin(2 * math.pi * 880 * t) + 0.05 * (_rnd() - 0.5)
        else:
            v = 0.0
        out.append(max(-1.0, min(1.0, v)))
    return out


def _enc_int24(fs):
    b = bytearray()
    for f in fs:
        q = max(-8388608, min(8388607, int(round(f * 8388607))))
        if q < 0:
            q += 1 << 24
        b += struct.pack("<I", q)[:3]
    return bytes(b)


def _enc_f32(fs):
    return struct.pack("<%df" % len(fs), *fs)


def _measure(codec_name, fmt, kind, N, srate, lib):
    codec = gwc.make_codec(codec_name, wavpack_lib=lib)
    bitres = 24 if fmt == "int24" else 32
    pfmt = gwc.PayloadFormat(bytes_per_sample=bitres // 8, channels=CH, buffer_size=BLOCK * N)
    raw = comp = made = 0
    while made < TOTAL_PACKETS:
        fs = _gen(kind, BLOCK * CH * N, made * BLOCK * CH, srate)
        payload = _enc_int24(fs) if fmt == "int24" else _enc_f32(fs)
        c = codec.encode(pfmt, payload)
        assert codec.decode(pfmt, c, len(payload)) == payload, "roundtrip mismatch"
        raw += len(payload)
        comp += len(c)
        made += N
    return 100.0 * comp / raw


def _lat_ms(N, srate):
    return N * BLOCK / srate * 1000.0


def main():
    lib = os.environ.get("GATEWAY_WAVPACK_LIB")
    codecs = ["zlib"] + (["wavpack"] if lib else [])
    if not lib:
        print("(GATEWAY_WAVPACK_LIB not set -> wavpack rows skipped)\n", file=sys.stderr)
    for srate, fmt, label in [(192000, "int24", "192kHz/24bit"), (384000, "float32", "384kHz/32float")]:
        print("\n=== %s : ratio%% by batch N (one-way batch latency) ===" % label)
        print("codec    signal   " + "".join("N=%-3d(%5.1fms) " % (N, _lat_ms(N, srate)) for N in BATCHES))
        for codec_name in codecs:
            for kind in ("silence", "speech", "music", "noise"):
                cells = "".join("%6.1f       " % _measure(codec_name, fmt, kind, N, srate, lib) for N in BATCHES)
                print("%-8s %-8s %s" % (codec_name, kind, cells))


if __name__ == "__main__":
    main()
