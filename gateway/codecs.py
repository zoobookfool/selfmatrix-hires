"""
Stage 2 compression gateway -- pluggable payload codecs.

Design (see docs/stage2-compression-gateway.md sec.3.4 / sec.5.1):
  - The JackTrip DefaultHeader (16 bytes: TimeStamp/SeqNumber/BufferSize/
    SamplingRate/BitResolution/NumIncomingChannelsFromNet/
    NumOutgoingChannelsToNet) is NEVER touched by any codec here. codecs.py
    only ever sees the *payload* (raw PCM sample bytes that follow the
    header) and returns bytes that replace that payload inside the tunnel
    frame. gateway.py is responsible for keeping the header byte-for-byte
    intact and only handing the payload slice to encode()/decode().
  - Compression unit is N=1 (single packet == single compression frame),
    per sec.5.1, to keep additional buffering latency at ~0. Every codec
    below is therefore stateless / self-contained per call: encode() must
    produce a self-describing blob that decode() can invert without any
    state carried over from a previous packet, and vice versa.
  - "none": passthrough, payload bytes are copied unchanged. Used both as
    an explicit codec choice and as the gateway's automatic fallback when
    a packet fails the header/payload-length cross-check.
  - "zlib": stdlib-only, level 1 by default (favor speed over ratio, this
    is a real-time path). Correctness (round-trip) is guaranteed by zlib
    itself; this is the safe default for the PoC.
  - "wavpack": ctypes binding against libwavpack's *lossless* codec, used
    in "raw PCM block" streaming mode (WavpackPackSamples /
    WavpackUnpackSamples against an in-memory blockout callback), not the
    CLI. Each packet is encoded as an independent WavPack block (no
    inter-packet prediction state kept), matching the N=1 design. This is
    required to be bit-exact lossless -- selftest.py enforces that. If the
    shared library cannot be located/loaded, the codec factory raises a
    clear error at startup (gateway.py refuses to start rather than
    silently falling back).

All codecs work on interleaved PCM payloads of two possible sample
formats used by JackTrip (see AudioInterface.cpp / requirements.md sec.6):
  - 24-bit signed integer, packed 3 bytes/sample, little-endian
  - 32-bit IEEE754 float, little-endian (JackTrip's "-b 32" mode)
bytes_per_sample and channel count come from the DefaultHeader fields
(BitResolution/8, and NumIncomingChannelsFromNet or
NumOutgoingChannelsToNet) so the codec layer does not need to guess.
"""

from __future__ import annotations

import ctypes
import os
import struct
import zlib
from dataclasses import dataclass
from typing import Optional


class CodecError(Exception):
    """Raised for unrecoverable codec setup/encode/decode errors."""


@dataclass(frozen=True)
class PayloadFormat:
    """Derived from the DefaultHeader fields, describes one packet's payload layout.

    bytes_per_sample: BitResolution // 8 (8/16/24/32 bit -> 1/2/3/4 bytes)
    channels: the "effective" channel count per
        DefaultHeader::validatePeerHeader()'s rule (see stage2 doc sec.3.4 /
        protocol facts): NumOutgoingChannelsToNet if nonzero and != 0xff,
        else NumIncomingChannelsFromNet, else 0.
    buffer_size: BufferSize field (samples per channel per packet).
    is_float: True if this packet's samples should be treated as IEEE754
        float32 (bytes_per_sample == 4). JackTrip's 32-bit mode is always
        real float32 (memcpy of JACK's sample_t), never int32 -- see
        requirements.md sec.6. bytes_per_sample==4 is therefore
        unambiguous. 1/2/3-byte samples are always signed PCM integers.
    """

    bytes_per_sample: int
    channels: int
    buffer_size: int

    @property
    def is_float(self) -> bool:
        return self.bytes_per_sample == 4

    @property
    def expected_payload_len(self) -> int:
        return self.buffer_size * self.channels * self.bytes_per_sample


class Codec:
    """Base interface. encode/decode operate on a single packet's payload bytes."""

    name = "base"

    def encode(self, fmt: PayloadFormat, payload: bytes) -> bytes:
        raise NotImplementedError

    def decode(self, fmt: PayloadFormat, blob: bytes, expected_len: int) -> bytes:
        raise NotImplementedError


class NoneCodec(Codec):
    """Passthrough. Used for the explicit 'none' codec and for cross-check fallback."""

    name = "none"

    def encode(self, fmt: PayloadFormat, payload: bytes) -> bytes:
        return payload

    def decode(self, fmt: PayloadFormat, blob: bytes, expected_len: int) -> bytes:
        return blob


class ZlibCodec(Codec):
    """Stdlib zlib, stateless per-packet. Real-time favoring: level defaults to 1."""

    name = "zlib"

    def __init__(self, level: int = 1):
        self.level = level

    def encode(self, fmt: PayloadFormat, payload: bytes) -> bytes:
        return zlib.compress(payload, self.level)

    def decode(self, fmt: PayloadFormat, blob: bytes, expected_len: int) -> bytes:
        out = zlib.decompress(blob)
        if len(out) != expected_len:
            raise CodecError(
                f"zlib decode length mismatch: got {len(out)} expected {expected_len}"
            )
        return out


# ---------------------------------------------------------------------------
# WavPack ctypes binding (streaming "raw PCM block" API, not the CLI).
#
# We bind to the small subset of libwavpack needed to pack/unpack one
# self-contained lossless block per call:
#   WavpackOpenFileOutput / WavpackSetConfiguration64 / WavpackPackInit /
#   WavpackPackSamples / WavpackFlushSamples for encode (samples are
#   handed to WavPack as int32_t per sample -- for 24-bit PCM the samples
#   are sign-extended into int32 slots as WavPack's integer API expects;
#   for float32 payloads we set config.float_norm_exp=127 (which makes
#   the library itself flag the block as float internally) and pass the
#   *bit pattern* of each float reinterpreted as int32, which is
#   WavPack's documented convention for float passthrough).
#   WavpackOpenFileInputEx64 (via a memory stream reader) / WavpackUnpackSamples
#   for decode.
#
# Every packet is encoded independently (no state kept between calls) to
# match the N=1 design (docs/stage2-compression-gateway.md sec.5.1): each
# call opens a fresh pack/unpack context.
# ---------------------------------------------------------------------------

WAVPACK_ENV_VAR = "GATEWAY_WAVPACK_LIB"

# --- ctypes structures mirroring wavpack.h (ABI-relevant subset only) ------
# (WavpackStreamReader64's actual ctypes.Structure is built per-instance
# in _MemReader.build_struct() below, since its callback fields need
# closures bound to that instance.)


class WavpackConfig(ctypes.Structure):
    # Layout verified against the upstream WavPack header
    # (include/wavpack.h, dbry/WavPack master, fetched 2026-07-06):
    #
    #   typedef struct {
    #       float bitrate, shaping_weight;
    #       int bits_per_sample, bytes_per_sample;
    #       int qmode, flags, xmode, num_channels, float_norm_exp;
    #       int32_t block_samples, worker_threads, sample_rate, channel_mask;
    #       unsigned char md5_checksum[16], md5_read;
    #       int num_tag_strings;
    #       char **tag_strings;
    #   } WavpackConfig;
    #
    # Only fields we explicitly set are meaningful for this PoC; the rest
    # are zeroed (via ctypes.memset before use) which WavPack treats as
    # "unset"/default.
    _fields_ = [
        ("bitrate", ctypes.c_float),
        ("shaping_weight", ctypes.c_float),
        ("bits_per_sample", ctypes.c_int),
        ("bytes_per_sample", ctypes.c_int),
        ("qmode", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("xmode", ctypes.c_int),
        ("num_channels", ctypes.c_int),
        ("float_norm_exp", ctypes.c_int),
        ("block_samples", ctypes.c_int32),
        ("worker_threads", ctypes.c_int32),
        ("sample_rate", ctypes.c_int32),
        ("channel_mask", ctypes.c_int32),
        ("md5_checksum", ctypes.c_ubyte * 16),
        ("md5_read", ctypes.c_ubyte),
        ("num_tag_strings", ctypes.c_int),
        ("tag_strings", ctypes.c_void_p),
    ]


# Values verified against upstream wavpack.h (fetched 2026-07-06) and
# src/pack_utils.c's WavpackSetConfiguration64 implementation. Note:
# CONFIG_FLOAT_DATA is NOT a public macro in wavpack.h at all -- it is an
# internal flag that WavpackSetConfiguration64() sets on wpc->config.flags
# by itself whenever config->float_norm_exp is nonzero (after validating
# bytes_per_sample==4 && bits_per_sample==32). Callers must NOT try to set
# it themselves; float mode is requested purely via float_norm_exp.
CONFIG_HIGH_FLAG = 0x800
CONFIG_FAST_FLAG = 0x200

# OPEN_STREAMING: "blindly unpacks blocks without header position
# verification" -- appropriate here since every packet is its own
# self-contained WavPack block with no continuous stream position to
# verify against (matches the N=1 stateless-per-packet design).
OPEN_STREAMING = 0x20
WAVPACK_ERROR_BUF_LEN = 256

# qmode flag: total sample count is unknown/not meaningful (streaming),
# matching the WavPack CLI's own handling of piped/stdin input.
QMODE_IGNORE_LENGTH = 0x800

READ_BUF_CB = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)
WRITE_BLOCK_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)


class _WavpackLib:
    """Lazily-loaded ctypes handle + prototypes. Raises CodecError if unavailable."""

    _instance: Optional["_WavpackLib"] = None

    def __init__(self, path: str):
        try:
            self.lib = ctypes.CDLL(path)
        except OSError as exc:
            raise CodecError(f"failed to load WavPack shared library '{path}': {exc}")
        self.path = path
        self._bind()

    def _bind(self):
        lib = self.lib
        lib.WavpackOpenFileOutput.restype = ctypes.c_void_p
        lib.WavpackOpenFileOutput.argtypes = [
            WRITE_BLOCK_CB, ctypes.c_void_p, ctypes.c_void_p
        ]
        lib.WavpackSetConfiguration64.restype = ctypes.c_int
        lib.WavpackSetConfiguration64.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(WavpackConfig), ctypes.c_int64, ctypes.c_void_p
        ]
        lib.WavpackPackInit.restype = ctypes.c_int
        lib.WavpackPackInit.argtypes = [ctypes.c_void_p]
        lib.WavpackPackSamples.restype = ctypes.c_int
        lib.WavpackPackSamples.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32), ctypes.c_uint32
        ]
        lib.WavpackFlushSamples.restype = ctypes.c_int
        lib.WavpackFlushSamples.argtypes = [ctypes.c_void_p]
        lib.WavpackCloseFile.restype = ctypes.c_void_p  # actually returns WavpackContext*
        lib.WavpackCloseFile.argtypes = [ctypes.c_void_p]
        lib.WavpackGetErrorMessage.restype = ctypes.c_char_p
        lib.WavpackGetErrorMessage.argtypes = [ctypes.c_void_p]

        lib.WavpackOpenFileInputEx64.restype = ctypes.c_void_p
        lib.WavpackOpenFileInputEx64.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_int, ctypes.c_int
        ]
        lib.WavpackUnpackSamples.restype = ctypes.c_uint32
        lib.WavpackUnpackSamples.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32), ctypes.c_uint32
        ]

    @classmethod
    def get(cls, explicit_path: Optional[str] = None) -> "_WavpackLib":
        if cls._instance is not None and (explicit_path is None or explicit_path == cls._instance.path):
            return cls._instance
        path = explicit_path or os.environ.get(WAVPACK_ENV_VAR)
        candidates = []
        if path:
            candidates.append(path)
        else:
            if os.name == "nt":
                candidates += ["wavpackdll.dll", "libwavpack.dll"]
            else:
                candidates += ["libwavpack.so.1", "libwavpack.so"]
        last_err = None
        for cand in candidates:
            try:
                inst = cls(cand)
                cls._instance = inst
                return inst
            except CodecError as exc:
                last_err = exc
        raise CodecError(
            "WavPack shared library not found. Set "
            f"{WAVPACK_ENV_VAR}=<path to wavpackdll.dll / libwavpack.so> "
            f"(last error: {last_err})"
        )


class _MemWriter:
    """Accumulates blocks written by WavpackPackSamples via the write_bytes callback."""

    def __init__(self):
        self.buf = bytearray()

    def make_callback(self):
        def _cb(id_, data, bcount):
            self.buf += ctypes.string_at(data, bcount)
            return 1
        self._keepalive = WRITE_BLOCK_CB(_cb)
        return self._keepalive


class _MemReader:
    """Feeds a fixed in-memory blob to WavpackOpenFileInputEx64 via read_bytes."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _read_bytes(self, id_, buf, bcount):
        remaining = len(self.data) - self.pos
        n = min(bcount, remaining) if remaining > 0 else 0
        if n > 0:
            chunk = self.data[self.pos:self.pos + n]
            ctypes.memmove(buf, chunk, n)
            self.pos += n
        return n

    def _get_pos(self, id_):
        return self.pos

    def _set_pos_abs(self, id_, pos):
        self.pos = pos
        return 0

    def _set_pos_rel(self, id_, delta, mode):
        # mode: 0=SEEK_SET 1=SEEK_CUR 2=SEEK_END (only used defensively)
        if mode == 0:
            self.pos = delta
        elif mode == 1:
            self.pos += delta
        else:
            self.pos = len(self.data) + delta
        return 0

    def _push_back_byte(self, id_, c):
        if self.pos > 0:
            self.pos -= 1
        return c

    def _get_length(self, id_):
        return len(self.data)

    def _can_seek(self, id_):
        return 1

    def build_struct(self):
        READ_CB = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)
        POS_CB = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p)
        SETPOS_ABS_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int64)
        SETPOS_REL_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int64, ctypes.c_int)
        PUSHBACK_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        LEN_CB = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p)
        CANSEEK_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
        TRUNC_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
        CLOSE_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)

        class Reader64(ctypes.Structure):
            _fields_ = [
                ("read_bytes", READ_CB),
                ("write_bytes", ctypes.c_void_p),
                ("get_pos", POS_CB),
                ("set_pos_abs", SETPOS_ABS_CB),
                ("set_pos_rel", SETPOS_REL_CB),
                ("push_back_byte", PUSHBACK_CB),
                ("get_length", LEN_CB),
                ("can_seek", CANSEEK_CB),
                ("truncate_here", ctypes.c_void_p),
                ("close", ctypes.c_void_p),
            ]

        self._cbs = (
            READ_CB(self._read_bytes),
            POS_CB(self._get_pos),
            SETPOS_ABS_CB(self._set_pos_abs),
            SETPOS_REL_CB(self._set_pos_rel),
            PUSHBACK_CB(self._push_back_byte),
            LEN_CB(self._get_length),
            CANSEEK_CB(self._can_seek),
        )
        s = Reader64()
        s.read_bytes = self._cbs[0]
        s.write_bytes = None
        s.get_pos = self._cbs[1]
        s.set_pos_abs = self._cbs[2]
        s.set_pos_rel = self._cbs[3]
        s.push_back_byte = self._cbs[4]
        s.get_length = self._cbs[5]
        s.can_seek = self._cbs[6]
        s.truncate_here = None
        s.close = None
        self._struct = s
        return s


def _samples_to_int32(fmt: PayloadFormat, payload: bytes) -> ctypes.Array:
    n_samples = fmt.buffer_size * fmt.channels
    arr = (ctypes.c_int32 * n_samples)()
    if fmt.is_float:
        floats = struct.unpack("<%df" % n_samples, payload)
        for i, f in enumerate(floats):
            arr[i] = struct.unpack("<i", struct.pack("<f", f))[0]
    elif fmt.bytes_per_sample == 3:
        for i in range(n_samples):
            off = i * 3
            b = payload[off:off + 3]
            v = b[0] | (b[1] << 8) | (b[2] << 16)
            if v & 0x800000:
                v -= 0x1000000
            arr[i] = v
    elif fmt.bytes_per_sample == 2:
        vals = struct.unpack("<%dh" % n_samples, payload)
        for i, v in enumerate(vals):
            arr[i] = v
    elif fmt.bytes_per_sample == 1:
        vals = struct.unpack("<%db" % n_samples, payload)
        for i, v in enumerate(vals):
            arr[i] = v
    else:
        raise CodecError(f"unsupported bytes_per_sample={fmt.bytes_per_sample}")
    return arr


def _int32_to_bytes(fmt: PayloadFormat, arr, n_samples: int) -> bytes:
    if fmt.is_float:
        out = bytearray(n_samples * 4)
        for i in range(n_samples):
            struct.pack_into("<f", out, i * 4, struct.unpack("<f", struct.pack("<i", arr[i]))[0])
        return bytes(out)
    elif fmt.bytes_per_sample == 3:
        out = bytearray(n_samples * 3)
        for i in range(n_samples):
            v = arr[i] & 0xFFFFFF
            out[i * 3] = v & 0xFF
            out[i * 3 + 1] = (v >> 8) & 0xFF
            out[i * 3 + 2] = (v >> 16) & 0xFF
        return bytes(out)
    elif fmt.bytes_per_sample == 2:
        return struct.pack("<%dh" % n_samples, *[ctypes.c_int16(arr[i]).value for i in range(n_samples)])
    elif fmt.bytes_per_sample == 1:
        return struct.pack("<%db" % n_samples, *[ctypes.c_int8(arr[i]).value for i in range(n_samples)])
    else:
        raise CodecError(f"unsupported bytes_per_sample={fmt.bytes_per_sample}")


class WavPackCodec(Codec):
    """Lossless, stateless-per-packet WavPack codec via ctypes streaming API.

    Each encode()/decode() call is fully self-contained (opens its own
    pack/unpack context), matching the N=1 compression-unit design. This
    is required to be bit-exact reversible; selftest.py verifies this.
    """

    name = "wavpack"

    def __init__(self, lib_path: Optional[str] = None, fast: bool = True):
        self._lib = _WavpackLib.get(lib_path)
        self.fast = fast

    def encode(self, fmt: PayloadFormat, payload: bytes) -> bytes:
        lib = self._lib.lib
        n_samples_total = fmt.buffer_size * fmt.channels
        samples = _samples_to_int32(fmt, payload)

        writer = _MemWriter()
        cb = writer.make_callback()
        ctx = lib.WavpackOpenFileOutput(cb, None, None)
        if not ctx:
            raise CodecError("WavpackOpenFileOutput failed")
        try:
            cfg = WavpackConfig()
            ctypes.memset(ctypes.byref(cfg), 0, ctypes.sizeof(cfg))
            cfg.bytes_per_sample = fmt.bytes_per_sample
            cfg.bits_per_sample = fmt.bytes_per_sample * 8
            cfg.num_channels = fmt.channels
            # sample_rate is WavPack block metadata only -- it does not
            # affect the (de)compression math/ratio at all, only what a
            # file-level player would show. We don't reliably know the
            # real Hz value here (DefaultHeader.SamplingRate is an enum,
            # frequently UNDEF per requirements.md sec.3.1/sec.6), so a
            # fixed placeholder is used. This is safe because the gateway
            # never writes a .wv file; it only round-trips raw blocks.
            cfg.sample_rate = 192000
            # Standard L/R channel mask for <=2 channels, matching the CLI's
            # own convention (0x5 - num_channels: mono->0x4, stereo->0x3).
            if fmt.channels <= 2:
                cfg.channel_mask = 0x5 - fmt.channels
            cfg.flags = CONFIG_FAST_FLAG if self.fast else 0
            if fmt.is_float:
                # Setting float_norm_exp alone is sufficient: the library
                # itself sets its internal CONFIG_FLOAT_DATA flag when
                # this is nonzero (see comment above). 127 means "already
                # normalized to +/-1.0 full scale" (2**(127-127) == 1),
                # matching the CLI's convention for float raw-PCM input.
                cfg.float_norm_exp = 127
            # Streaming/unknown total length, per the CLI's own convention
            # (total_samples=-1 + QMODE_IGNORE_LENGTH) for piped input --
            # each packet is one independent block, there is no "total".
            cfg.qmode |= QMODE_IGNORE_LENGTH

            if not lib.WavpackSetConfiguration64(ctx, ctypes.byref(cfg), ctypes.c_int64(-1), None):
                raise CodecError(f"WavpackSetConfiguration64 failed: {lib.WavpackGetErrorMessage(ctx)}")
            if not lib.WavpackPackInit(ctx):
                raise CodecError(f"WavpackPackInit failed: {lib.WavpackGetErrorMessage(ctx)}")
            if not lib.WavpackPackSamples(ctx, samples, fmt.buffer_size):
                raise CodecError(f"WavpackPackSamples failed: {lib.WavpackGetErrorMessage(ctx)}")
            if not lib.WavpackFlushSamples(ctx):
                raise CodecError(f"WavpackFlushSamples failed: {lib.WavpackGetErrorMessage(ctx)}")
        finally:
            lib.WavpackCloseFile(ctx)
        return bytes(writer.buf)

    def decode(self, fmt: PayloadFormat, blob: bytes, expected_len: int) -> bytes:
        lib = self._lib.lib
        n_samples_total = fmt.buffer_size * fmt.channels

        reader = _MemReader(blob)
        reader_struct = reader.build_struct()
        error_buf = ctypes.create_string_buffer(WAVPACK_ERROR_BUF_LEN)
        ctx = lib.WavpackOpenFileInputEx64(
            ctypes.byref(reader_struct), None, None, error_buf, OPEN_STREAMING, 0
        )
        if not ctx:
            raise CodecError(f"WavpackOpenFileInputEx64 failed to open block: {error_buf.value.decode('utf-8', 'replace')}")
        try:
            out_arr = (ctypes.c_int32 * n_samples_total)()
            got = lib.WavpackUnpackSamples(ctx, out_arr, fmt.buffer_size)
            if got != fmt.buffer_size:
                raise CodecError(f"WavpackUnpackSamples returned {got}, expected {fmt.buffer_size}")
        finally:
            lib.WavpackCloseFile(ctx)
        out_bytes = _int32_to_bytes(fmt, out_arr, n_samples_total)
        if len(out_bytes) != expected_len:
            raise CodecError(
                f"wavpack decode length mismatch: got {len(out_bytes)} expected {expected_len}"
            )
        return out_bytes


def make_codec(name: str, wavpack_lib: Optional[str] = None) -> Codec:
    """Factory. Raises CodecError with a clear message if the codec is unavailable."""
    name = name.lower()
    if name == "none":
        return NoneCodec()
    if name == "zlib":
        return ZlibCodec()
    if name == "wavpack":
        # Constructing WavPackCodec loads the shared library eagerly so
        # startup fails fast/clearly instead of failing on first packet.
        return WavPackCodec(lib_path=wavpack_lib)
    raise CodecError(f"unknown codec '{name}' (expected: none, zlib, wavpack)")
