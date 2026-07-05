#!/usr/bin/env python3
"""
Network-free unit self-test for the Stage 2 compression gateway PoC.

Run with:  python selftest.py
(or, on the WSL side:  python3 selftest.py)

Covers:
  1. Codec round-trip bit-exactness: 24-bit int x2ch x128 and float32 x2ch
     x128, for random / sine / silence material, 1000 packets each.
     zlib must PASS. wavpack runs only if a shared library is found
     (GATEWAY_WAVPACK_LIB env var, or default search names) -- otherwise
     SKIP is reported, not a failure.
  2. Handshake sniffer: synthetic byte sequences per the confirmed
     protocol facts, both split-arrival and coalesced-arrival.
  3. Tunnel frame encode/decode round-trip.
  4. Header passthrough: a dummy 16-byte header survives encode->decode
     without a single bit changed.

Exit code is 0 iff every mandatory (non-SKIP) check passed.
"""

from __future__ import annotations

import importlib.util
import math
import os
import random
import struct
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)


def _load_by_path(name: str, filename: str):
    """Load a local module by explicit file path.

    Needed for gateway/codecs.py in particular: plain `import codecs`
    resolves to the stdlib module (already cached in sys.modules), not
    our local file, regardless of sys.path ordering.
    """
    spec = importlib.util.spec_from_file_location(name, os.path.join(_THIS_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses etc. look modules up via sys.modules
    spec.loader.exec_module(module)
    return module


gwcodecs = _load_by_path("gateway_codecs", "codecs.py")
gw = _load_by_path("gateway_main", "gateway.py")

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

_results = []


def record(name: str, status: str, detail: str = ""):
    _results.append((name, status, detail))
    line = f"[{status}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)


# ---------------------------------------------------------------------
# Test material generators
# ---------------------------------------------------------------------


def gen_int24_samples(n: int, kind: str, seed: int) -> bytes:
    rnd = random.Random(seed)
    out = bytearray(n * 3)
    for i in range(n):
        if kind == "random":
            v = rnd.randint(-8388608, 8388607)
        elif kind == "sine":
            v = int(8000000 * math.sin(2 * math.pi * 440 * i / 192000.0))
        else:  # silence
            v = 0
        v &= 0xFFFFFF
        out[i * 3] = v & 0xFF
        out[i * 3 + 1] = (v >> 8) & 0xFF
        out[i * 3 + 2] = (v >> 16) & 0xFF
    return bytes(out)


def gen_float32_samples(n: int, kind: str, seed: int) -> bytes:
    rnd = random.Random(seed)
    vals = []
    for i in range(n):
        if kind == "random":
            vals.append(rnd.uniform(-1.0, 1.0))
        elif kind == "sine":
            vals.append(0.5 * math.sin(2 * math.pi * 440 * i / 384000.0))
        else:
            vals.append(0.0)
    return struct.pack("<%df" % n, *vals)


# ---------------------------------------------------------------------
# 1. Codec round-trip bit-exactness
# ---------------------------------------------------------------------


def run_codec_roundtrip_suite(codec_name: str, wavpack_lib: str = None) -> bool:
    try:
        codec = gwcodecs.make_codec(codec_name, wavpack_lib=wavpack_lib)
    except gwcodecs.CodecError as exc:
        record(f"codec:{codec_name} availability", SKIP, str(exc))
        return True  # SKIP does not count as failure

    buffer_size = 128
    channels = 2
    all_ok = True

    configs = [
        ("int24", 3, gen_int24_samples),
        ("float32", 4, gen_float32_samples),
    ]
    kinds = ["random", "sine", "silence"]
    n_packets = 1000

    for fmt_name, bps, gen in configs:
        fmt = gwcodecs.PayloadFormat(bytes_per_sample=bps, channels=channels, buffer_size=buffer_size)
        n_samples = buffer_size * channels
        for kind in kinds:
            mismatches = 0
            first_mismatch_detail = ""
            for pkt in range(n_packets):
                payload = gen(n_samples, kind, seed=pkt * 7919 + hash(kind) % 997)
                try:
                    blob = codec.encode(fmt, payload)
                    decoded = codec.decode(fmt, blob, expected_len=len(payload))
                except gwcodecs.CodecError as exc:
                    mismatches += 1
                    if not first_mismatch_detail:
                        first_mismatch_detail = f"pkt#{pkt} raised {exc}"
                    continue
                if decoded != payload:
                    mismatches += 1
                    if not first_mismatch_detail:
                        # find first differing byte for a useful message
                        diff_at = next((i for i in range(min(len(decoded), len(payload))) if decoded[i] != payload[i]), min(len(decoded), len(payload)))
                        first_mismatch_detail = (
                            f"pkt#{pkt} differs at byte {diff_at} "
                            f"(len decoded={len(decoded)} vs payload={len(payload)})"
                        )
            test_name = f"codec:{codec_name} roundtrip {fmt_name}/{kind} x{n_packets}"
            if mismatches == 0:
                record(test_name, PASS)
            else:
                record(test_name, FAIL, f"{mismatches}/{n_packets} mismatched; {first_mismatch_detail}")
                all_ok = False
    return all_ok


# ---------------------------------------------------------------------
# 2. Handshake sniffer
# ---------------------------------------------------------------------


def build_client_hello(port: int, name: str) -> bytes:
    name_bytes = name.encode("utf-8") + b"\x00"
    name_field = name_bytes + b"\x00" * (64 - len(name_bytes))
    assert len(name_field) == 64
    return struct.pack("<i", port) + name_field


def build_hub_port_reply(port: int) -> bytes:
    return struct.pack("<i", port)


def test_handshake_coalesced() -> bool:
    sniffer = gw.HandshakeSniffer()
    hello = build_client_hello(53000, "tester")
    assert len(hello) == 68
    sniffer.feed_from_client(hello)
    reply = build_hub_port_reply(61005)
    port = sniffer.feed_from_hub(reply)
    ok = port == 61005 and sniffer.assigned_udp_port == 61005
    record("handshake: coalesced (single read) client+reply", PASS if ok else FAIL, f"got port={port}")
    return ok


def test_handshake_split() -> bool:
    sniffer = gw.HandshakeSniffer()
    hello = build_client_hello(53001, "tester-split-name-thats-a-bit-longer-than-average")
    assert len(hello) == 68
    # split client hello across 3 arbitrary chunk boundaries
    chunks = [hello[0:1], hello[1:4], hello[4:40], hello[40:68]]
    for c in chunks:
        sniffer.feed_from_client(c)
    reply = build_hub_port_reply(61123)
    # split hub reply across 2 chunks (2 bytes then 2 bytes)
    port1 = sniffer.feed_from_hub(reply[0:2])
    port2 = sniffer.feed_from_hub(reply[2:4])
    ok = port1 is None and port2 == 61123 and sniffer.assigned_udp_port == 61123
    record("handshake: split (multi read) client+reply", PASS if ok else FAIL, f"port1={port1} port2={port2}")
    return ok


def test_handshake_auth_probe_rejected_for_port_learning() -> bool:
    """Auth::OK (65536) must NOT be misinterpreted as a valid UDP port."""
    sniffer = gw.HandshakeSniffer()
    # client sends nothing meaningful in auth mode before TLS; simulate
    # hub echoing Auth::OK (65536) as if it were the "port reply" bytes,
    # which must be rejected (>65535) rather than accepted as a port.
    auth_ok = struct.pack("<i", 65536)
    port = sniffer.feed_from_hub(auth_ok)
    ok = port is None and sniffer.assigned_udp_port == -1
    record("handshake: Auth::OK (65536) correctly rejected as port value", PASS if ok else FAIL, f"port={port} assigned={sniffer.assigned_udp_port}")
    return ok


# ---------------------------------------------------------------------
# 3. Tunnel frame round-trip
# ---------------------------------------------------------------------


def test_tunnel_frame_roundtrip() -> bool:
    all_ok = True
    cases = [
        (gw.CODEC_NONE, 0, 12345, 100, b"x" * 100),
        (gw.CODEC_ZLIB, 0, 1, 1040, b"\x01\x02\x03" * 200),
        (gw.CODEC_NONE, gw.FLAG_PASSTHROUGH, 65535, 16, b"\xff" * 16),
        (gw.CODEC_WAVPACK, 0, 0, 784, b"\xaa" * 50),
    ]
    for codec_id, flags, port_tag, orig_len, data in cases:
        frame = gw.build_tunnel_frame(codec_id, flags, port_tag, orig_len, data)
        parsed = gw.parse_tunnel_frame(frame)
        ok = parsed is not None and parsed == (codec_id, flags, port_tag & 0xFFFF, orig_len & 0xFFFF, data)
        if not ok:
            all_ok = False
        record(
            f"tunnel frame roundtrip codec={codec_id} tag={port_tag} len={orig_len}",
            PASS if ok else FAIL,
            "" if ok else f"parsed={parsed}",
        )

    # malformed frame (bad magic) must be rejected
    bad = b"XX" + bytes([1, 0, 0]) + b"\x00\x00\x00\x00" + b"data"
    ok2 = gw.parse_tunnel_frame(bad) is None
    record("tunnel frame rejects bad magic", PASS if ok2 else FAIL)
    all_ok = all_ok and ok2

    # too-short frame must be rejected
    ok3 = gw.parse_tunnel_frame(b"GW") is None
    record("tunnel frame rejects truncated header", PASS if ok3 else FAIL)
    all_ok = all_ok and ok3

    return all_ok


# ---------------------------------------------------------------------
# 4. Header passthrough (byte-exact, through the full compress/decompress path)
# ---------------------------------------------------------------------


def test_header_passthrough() -> bool:
    all_ok = True
    codec_by_id = {
        gw.CODEC_NONE: gwcodecs.make_codec("none"),
        gw.CODEC_ZLIB: gwcodecs.make_codec("zlib"),
    }

    for codec_name, codec_id in (("none", gw.CODEC_NONE), ("zlib", gw.CODEC_ZLIB)):
        codec = codec_by_id[codec_id]
        buffer_size = 128
        channels = 2
        bytes_per_sample = 3
        # header fields chosen to be non-trivial / non-zero to catch any
        # accidental modification.
        header = struct.pack(
            gw.JACKTRIP_HEADER_FMT,
            0x0123456789ABCDEF & ((1 << 64) - 1),  # TimeStamp
            0xBEEF,  # SeqNumber
            buffer_size,  # BufferSize
            7,  # SamplingRate (arbitrary enum value incl. UNDEF-like)
            bytes_per_sample * 8,  # BitResolution
            channels,  # NumIncomingChannelsFromNet
            0,  # NumOutgoingChannelsToNet (0 -> effective = incoming)
        )
        payload_bytes = os.urandom(buffer_size * channels * bytes_per_sample)
        datagram = header + payload_bytes

        codec_id_used, flags, wire = gw.compress_datagram(datagram, codec, codec_id)
        # header must be byte-identical at the start of `wire`
        header_ok = wire[: gw.JACKTRIP_HEADER_LEN] == header
        decoded = gw.decompress_datagram(codec_id_used, flags, len(datagram), wire, codec_by_id)
        full_ok = decoded == datagram
        header_after_decode_ok = decoded[: gw.JACKTRIP_HEADER_LEN] == header

        ok = header_ok and full_ok and header_after_decode_ok
        record(
            f"header passthrough via codec={codec_name}",
            PASS if ok else FAIL,
            "" if ok else f"header_ok={header_ok} full_ok={full_ok} header_after={header_after_decode_ok}",
        )
        all_ok = all_ok and ok

    # cross-check-failure path: truncated/garbage datagram must still
    # preserve whatever 16 bytes look like a header, passthrough only.
    garbage = os.urandom(10)  # shorter than a header
    codec = codec_by_id[gw.CODEC_ZLIB]
    codec_id_used, flags, wire = gw.compress_datagram(garbage, codec, gw.CODEC_ZLIB)
    ok = (flags & gw.FLAG_PASSTHROUGH) != 0 and wire == garbage
    record("header passthrough: sub-header-length datagram forced to passthrough", PASS if ok else FAIL)
    all_ok = all_ok and ok

    return all_ok


# ---------------------------------------------------------------------
# 5. Batch frame round-trip (build_batch_frame / parse_batch_frame)
# ---------------------------------------------------------------------


def _make_datagram(seq, buffer_size, channels, bps, kind, gen):
    header = struct.pack(
        gw.JACKTRIP_HEADER_FMT,
        0x1122334455667788 + seq,  # TimeStamp (varies per packet)
        seq & 0xFFFF,              # SeqNumber
        buffer_size,
        7,                         # SamplingRate enum
        bps * 8,                   # BitResolution
        channels,
        0,                         # OutgoingChannels (0 -> effective = incoming)
    )
    payload = gen(buffer_size * channels, kind, seed=seq * 7919 + hash(kind) % 997)
    return header + payload


def run_batch_roundtrip_suite(codec_name: str, wavpack_lib: str = None) -> bool:
    try:
        codec = gwcodecs.make_codec(codec_name, wavpack_lib=wavpack_lib)
    except gwcodecs.CodecError as exc:
        record(f"batch:{codec_name} availability", SKIP, str(exc))
        return True
    codec_id = gw.CODEC_ID_BY_NAME[codec_name]
    codec_by_id = {
        gw.CODEC_NONE: gwcodecs.make_codec("none"),
        gw.CODEC_ZLIB: gwcodecs.make_codec("zlib"),
    }
    codec_by_id[codec_id] = codec

    buffer_size, channels = 128, 2
    configs = [("int24", 3, gen_int24_samples), ("float32", 4, gen_float32_samples)]
    all_ok = True
    for fmt_name, bps, gen in configs:
        fmt = gwcodecs.PayloadFormat(bytes_per_sample=bps, channels=channels, buffer_size=buffer_size)
        for N in (2, 16, 64):
            for kind in ("random", "sine", "silence"):
                datagrams = [_make_datagram(s, buffer_size, channels, bps, kind, gen) for s in range(N)]
                frame = gw.build_batch_frame(codec_id, 0xABCD, datagrams, fmt, codec)
                parsed = gw.parse_tunnel_frame(frame)
                ok = parsed is not None and (parsed[1] & gw.FLAG_BATCH)
                if ok:
                    out = gw.parse_batch_frame(parsed[0], parsed[4], codec_by_id)
                    ok = out == datagrams  # bit-exact split-back of every datagram
                if not ok:
                    all_ok = False
                record(
                    f"batch roundtrip codec={codec_name} {fmt_name} N={N} {kind}",
                    PASS if ok else FAIL,
                    "" if ok else "split-back mismatch",
                )
    # malformed batch body must be rejected, not crash
    ok_bad = gw.parse_batch_frame(gw.CODEC_ZLIB, b"\x00", codec_by_id) is None
    record("batch frame rejects truncated body", PASS if ok_bad else FAIL)
    all_ok = all_ok and ok_bad
    return all_ok


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------


def main() -> int:
    print("=== Stage 2 gateway selftest ===")

    ok = True

    print("\n-- 1. codec round-trip bit-exactness --")
    ok &= run_codec_roundtrip_suite("zlib")
    wavpack_lib = os.environ.get(gwcodecs.WAVPACK_ENV_VAR)
    ok &= run_codec_roundtrip_suite("wavpack", wavpack_lib=wavpack_lib)

    print("\n-- 2. handshake sniffer --")
    ok &= test_handshake_coalesced()
    ok &= test_handshake_split()
    ok &= test_handshake_auth_probe_rejected_for_port_learning()

    print("\n-- 3. tunnel frame round-trip --")
    ok &= test_tunnel_frame_roundtrip()

    print("\n-- 4. header passthrough --")
    ok &= test_header_passthrough()

    print("\n-- 5. batch frame round-trip (split-back bit-exact) --")
    ok &= run_batch_roundtrip_suite("zlib")
    ok &= run_batch_roundtrip_suite("wavpack", wavpack_lib=wavpack_lib)

    print("\n=== summary ===")
    n_pass = sum(1 for _, s, _ in _results if s == PASS)
    n_fail = sum(1 for _, s, _ in _results if s == FAIL)
    n_skip = sum(1 for _, s, _ in _results if s == SKIP)
    print(f"PASS={n_pass} FAIL={n_fail} SKIP={n_skip} (total={len(_results)})")

    if n_fail > 0:
        print("\nFAILED checks:")
        for name, status, detail in _results:
            if status == FAIL:
                print(f"  - {name}: {detail}")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
