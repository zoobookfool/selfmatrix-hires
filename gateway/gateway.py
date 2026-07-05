#!/usr/bin/env python3
"""
Stage 2 compression gateway -- transparent tunnel proxy PoC.

Design reference: docs/stage2-compression-gateway.md (sec.3 architecture,
sec.5 latency budget), docs/requirements.md sec.4.1. This is PoC-grade
code for exit-criteria stage 1 (local/WSL loopback integration test),
NOT production hardened.

--- Protocol scope: PLAINTEXT handshake only -----------------------------
Per the byte-level protocol investigation (see task facts), JackTrip hub
mode's TCP 4464 handshake has two variants:

  1. Plaintext (no -A / --credsfile on hub): client sends a 68-byte
     cleartext blob [0-3: LE int32 dummy port][4-67: name]; hub replies
     with a 4-byte LE int32 = the UDP port it assigned to this client.
     This is what this proxy parses and relays byte-for-byte on TCP.

  2. Authenticated (-A + certfile/keyfile/credsfile on hub): after a
     4-byte plaintext Auth::OK probe/reply round-trip, the TCP stream
     upgrades to TLS (ClientHello sent by the client), and the *real*
     port-exchange bytes (plus username/password) are sent **inside**
     the TLS record layer. A transparent/passthrough proxy like this one
     cannot read anything past the TLS handshake -- it can still splice
     the raw TCP bytes end-to-end (the connection keeps working) but it
     can NOT passively learn the assigned UDP port anymore, because that
     4-byte integer is now encrypted.

This PoC implements and tests scenario (1) only. If the deployment uses
-A/TLS auth, this proxy's TCP relay still forwards bytes transparently
(so JackTrip itself keeps working end-to-end for the *TCP* leg -- but see
big caveat below), but UDP session/port learning will FAIL because the
port announcement never appears in cleartext. Supporting authenticated
mode would require this proxy to *terminate* TLS itself (MITM: present
the same certificate the hub uses, decrypt, read the port, re-encrypt
towards the hub or forward the now-known port out-of-band) -- this is
called out explicitly here and in POC.md as required future work, NOT
implemented in this PoC.

Also note: even in plaintext mode, the 4-byte "client UDP port" field the
*client* sends to the hub is a dummy the hub ignores (see protocol facts).
Only the server->client 4-byte reply (the hub's assigned UDP port) is
actually load-bearing and is what session tracking below keys off of.

--- Roles ------------------------------------------------------------------
  --role client : runs beside the JackTrip client (participant PC). Accepts
                  the local JackTrip client's TCP connection and UDP
                  traffic, relays to the peer gateway (hub side) over one
                  UDP "tunnel" socket.
  --role hub    : runs beside the JackTrip hub (VPS). Accepts the tunnel
                  UDP traffic from the client-side gateway, opens/relays a
                  TCP connection to the local JackTrip hub, and relays UDP
                  to/from the local hub's assigned port.

--- UDP handling -----------------------------------------------------------
Every JackTrip UDP datagram is assumed to start with the 16-byte
DefaultHeader (TimeStamp/SeqNumber/BufferSize/SamplingRate/BitResolution/
NumIncomingChannelsFromNet/NumOutgoingChannelsToNet). This header is
copied byte-for-byte, unmodified, into the tunnel frame. The payload
length implied by the header (BufferSize * effective_channels *
BitResolution/8) is cross-checked against actual payload length; only
payloads that pass this cross-check are compressed (codec applied). All
other datagrams (handshake noise, malformed headers, cross-check
mismatch) are relayed with codec='none' and an explicit flag bit so the
remote end knows not to attempt decompression.

--- Tunnel frame format (between the two gateway processes) ---------------
    magic   : 2 bytes  = b'GW'
    ver     : 1 byte   = 1
    codec   : 1 byte   = 0(none) / 1(zlib) / 2(wavpack)
    flags   : 1 byte   = bit0: PASSTHROUGH (payload not compressed,
                                 cross-check failed or codec='none')
                         bit1: RESERVED (unused, always 0 in this PoC)
    port_tag: 2 bytes  = LE uint16, session key (see below)
    orig_len: 2 bytes  = LE uint16, length of the *original* datagram
                         (header+payload) before any compression, so the
                         far end can size its receive buffer / validate.
    data    : header(16B) as-is + (compressed or raw) payload

port_tag semantics: each gateway maintains a table mapping "local UDP
port it is relaying for" <-> "a compact uint16 tag" so the far end knows
which local JackTrip UDP socket a given tunnel frame belongs to, without
having to send full 5-tuples on every packet. Concretely:
  - hub-side gateway assigns a tag the moment it learns (from the TCP
    handshake sniff) that JackTrip hub allocated port P for a new client;
    it tells the client-side gateway about this mapping implicitly by
    echoing the tag in the first UDP frames it forwards for that session
    (the client-side gateway, which is the one that has to open a local
    UDP listener facing its local JackTrip client, learns "tag <-> hub
    port" from the very first frame it receives from the hub-side
    gateway carrying that tag, and from then on uses the same tag when
    sending back).
  - the client-side gateway also assigns its own tag for the session at
    the moment its local JackTrip client's TCP connection completes the
    handshake sniff (using the low bits of the *hub*-assigned port,
    which is unique per session, as the tag -- this keeps client and hub
    tags identical for a given session, simplifying the PoC).

This is intentionally simple (PoC-scope): one tunnel UDP socket, sessions
distinguished purely by port_tag = (hub-assigned UDP port) & 0xFFFF.
Since JackTrip hub UDP ports are `mBasePort + id` and mBasePort defaults
around 61002, the raw port number fits in uint16 without truncation for
all realistic configurations (id < 1024, so port < 65536 as long as
mBasePort <= 64511); this is documented as a known PoC limitation.

--- Error handling ---------------------------------------------------------
Each relay direction runs in its own asyncio task / protocol; a failure
on one session (e.g. TCP peer closed) tears down only that session's
tasks, not the whole process. SIGINT (Ctrl-C) triggers a clean shutdown
of all sessions and sockets.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


def _load_local_codecs_module():
    """Load gateway/codecs.py by explicit file path.

    Plain `import codecs` would resolve to the stdlib module of the same
    name (already cached in sys.modules before user code ever runs), not
    our local gateway/codecs.py, regardless of sys.path ordering. Loading
    by explicit path sidesteps the name collision entirely.
    """
    this_dir = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("gateway_codecs", os.path.join(this_dir, "codecs.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["gateway_codecs"] = module  # dataclasses etc. look modules up via sys.modules
    spec.loader.exec_module(module)
    return module


_gwcodecs = _load_local_codecs_module()
CodecError = _gwcodecs.CodecError
PayloadFormat = _gwcodecs.PayloadFormat
make_codec = _gwcodecs.make_codec

MAGIC = b"GW"
VERSION = 1

CODEC_NONE = 0
CODEC_ZLIB = 1
CODEC_WAVPACK = 2
CODEC_ID_BY_NAME = {"none": CODEC_NONE, "zlib": CODEC_ZLIB, "wavpack": CODEC_WAVPACK}
CODEC_NAME_BY_ID = {v: k for k, v in CODEC_ID_BY_NAME.items()}

FLAG_PASSTHROUGH = 0x01

TUNNEL_HDR_FMT = "!2sBBBHH"  # magic, ver, codec, flags, port_tag, orig_len
TUNNEL_HDR_LEN = struct.calcsize(TUNNEL_HDR_FMT)
assert TUNNEL_HDR_LEN == 9

JACKTRIP_HEADER_LEN = 16
# DefaultHeaderStruct (native byte order, no explicit endian conversion in
# JackTrip itself -- see protocol facts). We parse it with '=' (native,
# no padding) matching uint64+uint16+uint16+uint8*4 = 16 bytes exactly on
# any conventional compiler/platform.
JACKTRIP_HEADER_FMT = "=QHHBBBB"
assert struct.calcsize(JACKTRIP_HEADER_FMT) == JACKTRIP_HEADER_LEN

GMAX_REMOTE_NAME_LEN = 64
CLIENT_HELLO_LEN = 4 + GMAX_REMOTE_NAME_LEN  # 68 bytes, plaintext mode


def parse_jacktrip_header(data: bytes) -> Optional[Tuple[int, int, int, int, int, int]]:
    """Parse the 16-byte DefaultHeader. Returns None if data too short.

    Returns (timestamp, seqnum, buffer_size, sampling_rate, bit_resolution,
    incoming_channels, outgoing_channels).
    """
    if len(data) < JACKTRIP_HEADER_LEN:
        return None
    ts, seq, bufsize, srate, bitres, in_ch, out_ch = struct.unpack(
        JACKTRIP_HEADER_FMT, data[:JACKTRIP_HEADER_LEN]
    )
    return ts, seq, bufsize, srate, bitres, in_ch, out_ch


def effective_channels(in_ch: int, out_ch: int) -> int:
    """Mirrors DefaultHeader::validatePeerHeader()'s channel-count rule."""
    if out_ch == 0:
        return in_ch
    if out_ch == 0xFF:
        return 0
    return out_ch


def payload_format_for(data: bytes) -> Optional[PayloadFormat]:
    """Derive a PayloadFormat from a datagram's header, or None if unparseable."""
    parsed = parse_jacktrip_header(data)
    if parsed is None:
        return None
    _ts, _seq, bufsize, _srate, bitres, in_ch, out_ch = parsed
    if bitres not in (8, 16, 24, 32):
        return None
    ch = effective_channels(in_ch, out_ch)
    if ch <= 0:
        return None
    return PayloadFormat(bytes_per_sample=bitres // 8, channels=ch, buffer_size=bufsize)


def cross_check_datagram(data: bytes) -> Tuple[bool, Optional[PayloadFormat]]:
    """Returns (ok, fmt). ok=True means len(data) == 16 + fmt.expected_payload_len."""
    fmt = payload_format_for(data)
    if fmt is None:
        return False, None
    expected_total = JACKTRIP_HEADER_LEN + fmt.expected_payload_len
    return (len(data) == expected_total), fmt


# --- TCP handshake sniffing (plaintext mode only, see module docstring) ----


class HandshakeSniffer:
    """Incrementally parses the plaintext JackTrip TCP handshake stream.

    Handles both split delivery (68-byte client hello arriving in several
    TCP reads) and coalesced delivery (multiple logical messages in one
    read). Only extracts the hub->client 4-byte UDP port reply, since
    that's the only value actually used by the hub (the client->hub 4
    bytes are a dummy per the protocol facts).

    Direction must be specified: 'from_client' feeds bytes the JackTrip
    client sent (68-byte hello we mostly skip over) or 'from_hub' feeds
    bytes the JackTrip hub sent (4-byte port reply, or its own further
    traffic once that's consumed -- for this PoC we stop caring about
    from_hub bytes once the first 4 are consumed, TCP relay is transparent
    regardless).
    """

    def __init__(self):
        self._client_buf = bytearray()
        self._client_hello_consumed = False
        self._hub_buf = bytearray()
        self.assigned_udp_port: Optional[int] = None

    def feed_from_client(self, data: bytes) -> None:
        if self._client_hello_consumed:
            return
        self._client_buf += data
        # We don't strictly need to fully parse the 68-byte hello (the
        # 4-byte port-from-client is a dummy anyway), but we track how
        # many bytes of it we've seen so we know when the hub's reply
        # bytes are unambiguously the port reply and not something else.
        if len(self._client_buf) >= CLIENT_HELLO_LEN:
            self._client_hello_consumed = True

    def feed_from_hub(self, data: bytes) -> Optional[int]:
        """Returns the assigned UDP port the instant 4 bytes are available."""
        if self.assigned_udp_port is not None:
            return None
        self._hub_buf += data
        if len(self._hub_buf) >= 4:
            port = struct.unpack("<i", bytes(self._hub_buf[:4]))[0]
            if 0 <= port <= 65535:
                self.assigned_udp_port = port
                return port
            # Auth response codes (e.g. Auth::OK=65536) exceed 65535 --
            # this would indicate authenticated mode, which this PoC does
            # not support for port learning (see module docstring).
            self.assigned_udp_port = -1
            return None
        return None


# --- Statistics --------------------------------------------------------


@dataclass
class Stats:
    packets_relayed: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    cross_check_mismatches: int = 0
    last_report: float = field(default_factory=time.monotonic)

    def record(self, raw_len: int, wire_len: int, mismatched: bool) -> None:
        self.packets_relayed += 1
        self.bytes_in += raw_len
        self.bytes_out += wire_len
        if mismatched:
            self.cross_check_mismatches += 1

    def maybe_report(self, interval: float = 5.0) -> None:
        now = time.monotonic()
        if now - self.last_report < interval:
            return
        ratio = (self.bytes_out / self.bytes_in * 100.0) if self.bytes_in else 0.0
        print(
            f"[stats] pkts={self.packets_relayed} in={self.bytes_in}B "
            f"out={self.bytes_out}B ratio={ratio:.1f}% mismatches={self.cross_check_mismatches}",
            file=sys.stderr,
            flush=True,
        )
        self.last_report = now
        self.packets_relayed = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.cross_check_mismatches = 0


def hexdump16(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data[:16])


# --- Tunnel frame encode/decode -----------------------------------------


def build_tunnel_frame(codec_id: int, flags: int, port_tag: int, orig_len: int, data: bytes) -> bytes:
    hdr = struct.pack(TUNNEL_HDR_FMT, MAGIC, VERSION, codec_id, flags, port_tag & 0xFFFF, orig_len & 0xFFFF)
    return hdr + data


def parse_tunnel_frame(frame: bytes):
    """Returns (codec_id, flags, port_tag, orig_len, data) or None if malformed."""
    if len(frame) < TUNNEL_HDR_LEN:
        return None
    magic, ver, codec_id, flags, port_tag, orig_len = struct.unpack(
        TUNNEL_HDR_FMT, frame[:TUNNEL_HDR_LEN]
    )
    if magic != MAGIC or ver != VERSION:
        return None
    return codec_id, flags, port_tag, orig_len, frame[TUNNEL_HDR_LEN:]


# --- Datagram compression helpers ---------------------------------------


def compress_datagram(data: bytes, codec, codec_id: int, debug_ctr=None, debug_n: int = 0) -> Tuple[int, int, bytes]:
    """Returns (codec_id_used, flags, wire_payload_after_header).

    wire_payload_after_header is: header(16B) + (compressed-or-raw payload).
    """
    ok, fmt = cross_check_datagram(data)
    header = data[:JACKTRIP_HEADER_LEN]
    if debug_ctr is not None and debug_ctr[0] < debug_n:
        print(f"[debug-header] #{debug_ctr[0]} {hexdump16(header)}", file=sys.stderr, flush=True)
        debug_ctr[0] += 1
    if not ok or fmt is None:
        # Cross-check failed: relay untouched, flagged passthrough.
        return CODEC_NONE, FLAG_PASSTHROUGH, data
    payload = data[JACKTRIP_HEADER_LEN:]
    try:
        compressed = codec.encode(fmt, payload)
    except CodecError as exc:
        print(f"[warn] codec encode failed, falling back to passthrough: {exc}", file=sys.stderr)
        return CODEC_NONE, FLAG_PASSTHROUGH, data
    return codec_id, 0, header + compressed


def decompress_datagram(codec_id: int, flags: int, orig_len: int, wire: bytes, codec_by_id) -> bytes:
    if flags & FLAG_PASSTHROUGH or codec_id == CODEC_NONE:
        return wire
    header = wire[:JACKTRIP_HEADER_LEN]
    blob = wire[JACKTRIP_HEADER_LEN:]
    fmt = payload_format_for(header)
    if fmt is None:
        # Shouldn't happen (sender cross-checked before compressing), but
        # fail safe: return header + blob untouched rather than crash.
        return wire
    codec = codec_by_id[codec_id]
    expected_len = orig_len - JACKTRIP_HEADER_LEN
    payload = codec.decode(fmt, blob, expected_len)
    return header + payload


# --- Session bookkeeping -------------------------------------------------


@dataclass
class Session:
    """One JackTrip client<->hub pairing, keyed by port_tag."""

    port_tag: int
    local_udp_port: int  # the local (loopback) UDP port this gateway relays for
    peer_addr: Optional[Tuple[str, int]] = None  # tunnel-side remote addr, once known
    local_peer_addr: Optional[Tuple[str, int]] = None  # observed local JackTrip UDP peer addr


class TunnelUdpProtocol(asyncio.DatagramProtocol):
    """The single UDP socket used to talk to the peer gateway process."""

    def __init__(self, gateway: "Gateway"):
        self.gateway = gateway
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        self.gateway.on_tunnel_datagram(data, addr)

    def error_received(self, exc):
        print(f"[warn] tunnel udp error: {exc}", file=sys.stderr)


class LocalUdpProtocol(asyncio.DatagramProtocol):
    """Local-facing UDP socket, one per session, talking to the local JackTrip process."""

    def __init__(self, gateway: "Gateway", session: Session):
        self.gateway = gateway
        self.session = session
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        self.session.local_peer_addr = addr
        self.gateway.on_local_datagram(self.session, data)

    def error_received(self, exc):
        print(f"[warn] local udp error ({self.session.port_tag}): {exc}", file=sys.stderr)


class Gateway:
    def __init__(self, args):
        self.args = args
        self.codec = make_codec(args.codec, wavpack_lib=args.wavpack_lib)
        self.codec_id = CODEC_ID_BY_NAME[args.codec]
        self.codec_by_id = {
            CODEC_NONE: make_codec("none"),
            CODEC_ZLIB: make_codec("zlib"),
        }
        if args.codec == "wavpack":
            self.codec_by_id[CODEC_WAVPACK] = self.codec
        self.sessions: Dict[int, Session] = {}
        self.stats = Stats()
        self.debug_headers_n = args.debug_headers
        self._debug_ctr = [0]
        self.tunnel_transport: Optional[asyncio.DatagramTransport] = None
        self.peer_tunnel_addr: Optional[Tuple[str, int]] = args.peer_addr
        self._loop = None
        self._local_transports: Dict[int, asyncio.DatagramTransport] = {}
        self._pending_local_frames: Dict[int, list] = {}
        self._open_udp_tasks: set = set()

    # -- setup ------------------------------------------------------------

    async def start(self):
        self._loop = asyncio.get_running_loop()
        transport, protocol = await self._loop.create_datagram_endpoint(
            lambda: TunnelUdpProtocol(self),
            local_addr=(self.args.bind_host, self.args.tunnel_port),
        )
        self.tunnel_transport = transport
        print(
            f"[gateway] role={self.args.role} tunnel udp listening on "
            f"{self.args.bind_host}:{self.args.tunnel_port} codec={self.args.codec}",
            file=sys.stderr,
        )
        if self.args.role == "client":
            await self._start_client_role()
        else:
            await self._start_hub_role()

    async def _start_client_role(self):
        server = await asyncio.start_server(
            self._handle_client_tcp, self.args.bind_host, self.args.local_tcp_port
        )
        print(
            f"[gateway] client role: TCP listener on "
            f"{self.args.bind_host}:{self.args.local_tcp_port} "
            f"-> tunnel peer {self.peer_tunnel_addr}",
            file=sys.stderr,
        )
        async with server:
            await server.serve_forever()

    async def _start_hub_role(self):
        # Hub role: wait for tunnel-forwarded TCP bytes is not applicable;
        # instead the hub-side gateway proactively connects to the local
        # JackTrip hub TCP port whenever the client-side gateway signals a
        # new session via a control frame. For this PoC, we implement the
        # simpler symmetric approach: the hub-side gateway also listens
        # for an inbound TCP "trigger" connection is unnecessary because
        # the actual TCP relay is end-to-end (client gateway <-> hub
        # gateway <-> local hub) using a dedicated relay TCP connection
        # established on demand. See handle_hub_tcp_trigger below, invoked
        # from the tunnel control channel.
        print(
            f"[gateway] hub role: local jacktrip hub assumed at "
            f"{self.args.jacktrip_host}:{self.args.jacktrip_tcp_port}, "
            f"local udp base assumed dynamic (learned via TCP sniff)",
            file=sys.stderr,
        )
        # Idle loop; work happens in callbacks triggered by tunnel control
        # frames (TCP_OPEN) and datagrams.
        while True:
            await asyncio.sleep(3600)

    # -- TCP relay (client role: local JackTrip client -> tunnel) --------

    async def _handle_client_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """A local JackTrip client connected to us. Relay to hub via a
        dedicated TCP-over-tunnel control stream, sniffing the handshake.

        For PoC simplicity, the TCP bytes themselves are relayed through
        a *direct* TCP connection this gateway makes to the peer gateway's
        TCP relay port (args.peer_tcp_port), which the hub-side gateway in
        turn splices to the real local JackTrip hub. This keeps TCP
        byte-transparency trivial (plain TCP<->TCP splice) while still
        letting each side sniff the bytes as they pass through.
        """
        peer_host, _peer_udp_port = self.peer_tunnel_addr
        peer_tcp_port = self.args.peer_tcp_port
        sniffer = HandshakeSniffer()
        try:
            remote_reader, remote_writer = await asyncio.open_connection(peer_host, peer_tcp_port)
        except OSError as exc:
            print(f"[error] client role: cannot reach hub gateway TCP relay {peer_host}:{peer_tcp_port}: {exc}", file=sys.stderr)
            writer.close()
            return

        async def pump_client_to_remote():
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    sniffer.feed_from_client(data)
                    remote_writer.write(data)
                    await remote_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                remote_writer.close()

        async def pump_remote_to_client():
            try:
                while True:
                    data = await remote_reader.read(4096)
                    if not data:
                        break
                    port = sniffer.feed_from_hub(data)
                    if port is not None and port > 0:
                        self._register_client_session(port)
                    writer.write(data)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                writer.close()

        try:
            await asyncio.gather(pump_client_to_remote(), pump_remote_to_client())
        except Exception as exc:
            print(f"[warn] client tcp relay session ended with error: {exc}", file=sys.stderr)

    def _register_client_session(self, hub_udp_port: int):
        port_tag = hub_udp_port & 0xFFFF
        if port_tag in self.sessions:
            return
        session = Session(port_tag=port_tag, local_udp_port=0)
        self.sessions[port_tag] = session
        self._spawn_open_local_udp(session, listen_port=0)
        print(f"[gateway] client role: learned hub udp port {hub_udp_port} -> session tag {port_tag}", file=sys.stderr)

    def _spawn_open_local_udp(self, session: Session, listen_port: int) -> None:
        """Schedule _open_local_udp_for_session and make sure failures are
        visible and don't leak sockets or vanish into an unretrieved-
        exception warning at GC time (see design doc sec.3.3/sec.8 open
        issue about first-packet loss on session setup).
        """
        task = asyncio.ensure_future(self._open_local_udp_for_session(session, listen_port))
        self._open_udp_tasks.add(task)

        def _on_done(t: "asyncio.Task", port_tag=session.port_tag):
            self._open_udp_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                print(
                    f"[error] failed to open local udp relay for session {port_tag}: {exc!r}",
                    file=sys.stderr,
                )

        task.add_done_callback(_on_done)

    async def _open_local_udp_for_session(self, session: Session, listen_port: int):
        transport = None
        try:
            transport, protocol = await self._loop.create_datagram_endpoint(
                lambda: LocalUdpProtocol(self, session),
                local_addr=(self.args.bind_host, listen_port),
            )
            actual_port = transport.get_extra_info("sockname")[1]
            session.local_udp_port = actual_port
            self._local_transports[session.port_tag] = transport
            if self.args.role == "client":
                # Tell local JackTrip client to talk UDP to this port; PoC
                # exposes it via stderr so an operator/launcher script can
                # wire it up (in the local integration test, the JackTrip
                # client's peer UDP port is exactly what the TCP handshake
                # sniff already reported -- see POC.md for the wiring detail).
                print(f"[gateway] client role: local udp relay for session {session.port_tag} bound on port {actual_port}", file=sys.stderr)
            else:
                print(f"[gateway] hub role: local udp relay for session {session.port_tag} bound on port {actual_port}", file=sys.stderr)
        except Exception:
            if transport is not None:
                transport.close()
            raise
        # Flush any tunnel frames that arrived for this session before the
        # local transport was ready, in port_tag arrival order.
        pending = self._pending_local_frames.pop(session.port_tag, None)
        if pending:
            for data in pending:
                self._forward_to_local_transport(session, transport, data)

    # -- Hub role TCP relay (accept connections forwarded from client gateway) --

    async def start_hub_tcp_relay_listener(self):
        server = await asyncio.start_server(
            self._handle_hub_tcp, self.args.bind_host, self.args.local_tcp_port
        )
        print(
            f"[gateway] hub role: TCP relay listener on {self.args.bind_host}:{self.args.local_tcp_port}",
            file=sys.stderr,
        )
        async with server:
            await server.serve_forever()

    async def _handle_hub_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """A client-side gateway connected to relay a JackTrip client's TCP
        session. Splice to the real local JackTrip hub, sniffing the reply.
        """
        sniffer = HandshakeSniffer()
        try:
            remote_reader, remote_writer = await asyncio.open_connection(
                self.args.jacktrip_host, self.args.jacktrip_tcp_port
            )
        except OSError as exc:
            print(f"[error] hub role: cannot reach local jacktrip hub {self.args.jacktrip_host}:{self.args.jacktrip_tcp_port}: {exc}", file=sys.stderr)
            writer.close()
            return

        async def pump_in_to_hub():
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    sniffer.feed_from_client(data)
                    remote_writer.write(data)
                    await remote_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                remote_writer.close()

        async def pump_hub_to_in():
            try:
                while True:
                    data = await remote_reader.read(4096)
                    if not data:
                        break
                    port = sniffer.feed_from_hub(data)
                    if port is not None and port > 0:
                        self._register_hub_session(port)
                    writer.write(data)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                writer.close()

        try:
            await asyncio.gather(pump_in_to_hub(), pump_hub_to_in())
        except Exception as exc:
            print(f"[warn] hub tcp relay session ended with error: {exc}", file=sys.stderr)

    def _register_hub_session(self, hub_udp_port: int):
        port_tag = hub_udp_port & 0xFFFF
        if port_tag in self.sessions:
            return
        session = Session(port_tag=port_tag, local_udp_port=hub_udp_port)
        session.local_peer_addr = (self.args.jacktrip_host, hub_udp_port)
        self.sessions[port_tag] = session
        self._spawn_open_local_udp(session, listen_port=0)
        print(f"[gateway] hub role: learned local hub udp port {hub_udp_port} -> session tag {port_tag}", file=sys.stderr)

    # -- UDP relay: local JackTrip <-> tunnel -----------------------------

    def on_local_datagram(self, session: Session, data: bytes):
        """A datagram arrived from the local JackTrip process for this session."""
        codec_id, flags, wire = compress_datagram(
            data, self.codec, self.codec_id, self._debug_ctr, self.debug_headers_n
        )
        frame = build_tunnel_frame(codec_id, flags, session.port_tag, len(data), wire)
        if self.tunnel_transport and self.peer_tunnel_addr:
            self.tunnel_transport.sendto(frame, self.peer_tunnel_addr)
            self.stats.record(len(data), len(frame), bool(flags & FLAG_PASSTHROUGH) and codec_id == CODEC_NONE and self.codec_id != CODEC_NONE)
        else:
            print(
                f"[warn] dropping local datagram for session {session.port_tag}: "
                f"tunnel peer not known yet (tunnel_transport={bool(self.tunnel_transport)}, "
                f"peer_tunnel_addr={self.peer_tunnel_addr})",
                file=sys.stderr,
            )
        self.stats.maybe_report()

    def on_tunnel_datagram(self, frame: bytes, addr):
        """A frame arrived from the peer gateway over the tunnel."""
        parsed = parse_tunnel_frame(frame)
        if parsed is None:
            print(f"[warn] dropping malformed tunnel frame from {addr} ({len(frame)}B)", file=sys.stderr)
            return
        codec_id, flags, port_tag, orig_len, wire = parsed
        # Learn/refresh the peer tunnel address opportunistically (PoC:
        # single peer assumed, see args.peer_addr / --peer for the static
        # config; this also supports NAT'd peers that move source ports).
        self.peer_tunnel_addr = self.peer_tunnel_addr or addr

        session = self.sessions.get(port_tag)
        if session is None:
            print(f"[warn] tunnel frame for unknown session tag={port_tag}, dropping", file=sys.stderr)
            return
        try:
            data = decompress_datagram(codec_id, flags, orig_len, wire, self.codec_by_id)
        except CodecError as exc:
            print(f"[warn] decode failed for session {port_tag}: {exc}", file=sys.stderr)
            return
        transport = self._local_transports.get(port_tag)
        if transport is None:
            # Session is known but its local UDP transport hasn't finished
            # opening yet (asynchronous bind still in flight). Buffer the
            # frame instead of dropping it outright so the very first
            # relayed packet(s) of a new session -- which JackTrip's
            # two-stage first-packet handshake can be sensitive to -- aren't
            # silently lost; _open_local_udp_for_session flushes this queue
            # once the transport is ready.
            pending = self._pending_local_frames.setdefault(port_tag, [])
            pending.append(data)
            print(
                f"[warn] no local udp transport yet for session {port_tag}, buffering ({len(pending)} pending)",
                file=sys.stderr,
            )
            return
        self._forward_to_local_transport(session, transport, data)

    def _forward_to_local_transport(self, session: Session, transport: asyncio.DatagramTransport, data: bytes) -> None:
        if session.local_peer_addr is not None:
            transport.sendto(data, session.local_peer_addr)
        elif self.args.role == "hub":
            transport.sendto(data, (self.args.jacktrip_host, session.local_udp_port))
        # else (client role, local peer not yet observed): drop; JackTrip
        # client hasn't sent its first UDP packet to us yet so we don't
        # know its ephemeral source port (mirrors hub's own NAT-learning
        # behavior per protocol facts).


# --- CLI ------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Stage 2 compression gateway PoC (transparent JackTrip UDP tunnel proxy)."
    )
    p.add_argument("--role", choices=["client", "hub"], required=True)
    p.add_argument("--bind-host", default="127.0.0.1", help="local bind address for all sockets this gateway owns")
    p.add_argument("--tunnel-port", type=int, required=True, help="UDP port this gateway listens on for the tunnel to the peer gateway")
    p.add_argument("--peer", dest="peer_addr_str", default=None, help="host:port of peer gateway's tunnel UDP socket (required for client role; optional for hub role, learned from first packet if omitted)")
    p.add_argument("--peer-tcp-port", type=int, default=None, help="(client role) TCP port on the peer (hub) gateway that relays to the real JackTrip hub")
    p.add_argument("--local-tcp-port", type=int, default=4464, help="TCP port this gateway listens on (client role: faces local JackTrip client; hub role: faces the client-side gateway)")
    p.add_argument("--jacktrip-host", default="127.0.0.1", help="(hub role) address of the real local JackTrip hub")
    p.add_argument("--jacktrip-tcp-port", type=int, default=4464, help="(hub role) TCP port of the real local JackTrip hub")
    p.add_argument("--codec", choices=["none", "zlib", "wavpack"], default="zlib")
    p.add_argument("--wavpack-lib", default=None, help="path to wavpackdll.dll / libwavpack.so (overrides GATEWAY_WAVPACK_LIB env var)")
    p.add_argument("--debug-headers", type=int, default=0, metavar="N", help="hex-dump the first N relayed packet headers (16 bytes) to stderr")
    args = p.parse_args(argv)

    if args.peer_addr_str:
        host, _, port_s = args.peer_addr_str.rpartition(":")
        args.peer_addr = (host, int(port_s))
    else:
        args.peer_addr = None

    if args.role == "client" and args.peer_addr is None:
        p.error("--peer is required for --role client")
    if args.role == "client" and args.peer_tcp_port is None:
        p.error("--peer-tcp-port is required for --role client")

    return args


async def async_main(args):
    gw = Gateway(args)
    tasks = []
    if args.role == "hub":
        tasks.append(asyncio.ensure_future(gw.start()))
        tasks.append(asyncio.ensure_future(gw.start_hub_tcp_relay_listener()))
    else:
        tasks.append(asyncio.ensure_future(gw.start()))

    stop_event = asyncio.Event()

    def _on_signal():
        print("\n[gateway] shutting down...", file=sys.stderr)
        stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _on_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_signal)
    except NotImplementedError:
        # Windows: add_signal_handler for SIGINT/SIGTERM isn't supported
        # on the proactor event loop; rely on KeyboardInterrupt instead.
        pass

    done, pending = await asyncio.wait(
        [asyncio.ensure_future(stop_event.wait())] + tasks, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    for t in pending:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


def main():
    args = parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n[gateway] interrupted, exiting.", file=sys.stderr)


if __name__ == "__main__":
    main()
