#!/usr/bin/env python3
"""Strict, rate-limited DNS relay for the protected Tart worker."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable

import dns.edns
import dns.flags
import dns.message
import dns.name
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
from dns.exception import DNSException

LISTEN_PORT = 55353
UPSTREAM_ADDRESS = "1.1.1.1"
UPSTREAM_SERVER_NAME = "cloudflare-dns.com"
UPSTREAM_PORT = 853
UPSTREAM_TIMEOUT_SECONDS = 3.0
MAX_UDP_QUERY_SIZE = 1232
MAX_TCP_QUERY_SIZE = 4096
MAX_RESPONSE_SIZE = 4096
MAX_IN_FLIGHT = 8
RATE_BURST = 20.0
RATE_PER_SECOND = 10.0
ALLOWED_QUERY_FLAGS = dns.flags.RD | dns.flags.AD | dns.flags.CD
BLOCKED_QUERY_TYPES = {dns.rdatatype.AXFR, dns.rdatatype.IXFR, dns.rdatatype.ANY}


class InvalidDNSQueryError(ValueError):
    """The input is not one supported DNS query."""


class TokenBucket:
    def __init__(
        self,
        *,
        capacity: float = RATE_BURST,
        refill_per_second: float = RATE_PER_SECOND,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._monotonic = monotonic
        self._tokens = capacity
        self._updated_at = monotonic()
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            now = self._monotonic()
            elapsed = max(0.0, now - self._updated_at)
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
            self._updated_at = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


def validate_query(packet: bytes, *, tcp: bool = False) -> dns.message.Message:
    max_size = MAX_TCP_QUERY_SIZE if tcp else MAX_UDP_QUERY_SIZE
    if len(packet) < 12 or len(packet) > max_size:
        raise InvalidDNSQueryError("query size is outside the allowed range")
    try:
        query = dns.message.from_wire(packet, ignore_trailing=False, one_rr_per_rrset=True)
    except (DNSException, ValueError, struct.error) as exc:
        raise InvalidDNSQueryError("malformed DNS wire message") from exc

    if query.flags & dns.flags.QR or query.opcode() != dns.opcode.QUERY:
        raise InvalidDNSQueryError("only standard DNS queries are accepted")
    if query.flags & ~ALLOWED_QUERY_FLAGS:
        raise InvalidDNSQueryError("unsupported DNS header flags")
    if len(query.question) != 1 or query.answer or query.authority or query.had_tsig:
        raise InvalidDNSQueryError("query must contain exactly one question and no other records")
    question = query.question[0]
    if question.rdclass != dns.rdataclass.IN or question.rdtype in BLOCKED_QUERY_TYPES:
        raise InvalidDNSQueryError("unsupported DNS question type or class")
    _validate_qname(question.name)

    if query.edns >= 0:
        options = getattr(query, "options", ())
        if query.edns != 0 or query.payload > MAX_UDP_QUERY_SIZE or any(
            not isinstance(option, dns.edns.CookieOption) for option in options
        ):
            raise InvalidDNSQueryError("unsupported EDNS version, size, or options")
        if query.additional:
            raise InvalidDNSQueryError("only a single empty EDNS OPT record is accepted")
    elif query.additional:
        raise InvalidDNSQueryError("additional records are not accepted")
    return query


def _validate_qname(name: dns.name.Name) -> None:
    labels = name.labels[:-1]
    wire_name = name.to_wire()
    if not labels or wire_name is None or len(wire_name) > 255:
        raise InvalidDNSQueryError("root or overlong DNS name is not accepted")
    for label in labels:
        if not label or len(label) > 63:
            raise InvalidDNSQueryError("DNS label length is invalid")
        try:
            text = label.decode("ascii")
        except UnicodeDecodeError as exc:
            raise InvalidDNSQueryError("DNS labels must use ASCII/IDNA encoding") from exc
        if any(not (char.isascii() and (char.isalnum() or char in "-_")) for char in text):
            raise InvalidDNSQueryError("DNS label contains unsupported characters")


def validate_response(packet: bytes, query: dns.message.Message) -> dns.message.Message:
    if len(packet) < 12 or len(packet) > MAX_RESPONSE_SIZE:
        raise ValueError("upstream response size is outside the allowed range")
    try:
        response = dns.message.from_wire(packet, ignore_trailing=False, one_rr_per_rrset=True)
    except (DNSException, ValueError, struct.error) as exc:
        raise ValueError("upstream returned malformed DNS") from exc
    if (
        not response.flags & dns.flags.QR
        or response.id != query.id
        or response.opcode() != dns.opcode.QUERY
        or response.question != query.question
        or response.had_tsig
    ):
        raise ValueError("upstream response does not match the query")
    return response


def forward_dot(packet: bytes, query: dns.message.Message) -> bytes:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection(
        (UPSTREAM_ADDRESS, UPSTREAM_PORT), timeout=UPSTREAM_TIMEOUT_SECONDS
    ) as upstream:
        with context.wrap_socket(upstream, server_hostname=UPSTREAM_SERVER_NAME) as secure:
            secure.settimeout(UPSTREAM_TIMEOUT_SECONDS)
            secure.sendall(struct.pack("!H", len(packet)) + packet)
            length = struct.unpack("!H", _recv_exact(secure, 2))[0]
            if length < 12 or length > MAX_RESPONSE_SIZE:
                raise ValueError("upstream response frame is outside the allowed range")
            response = _recv_exact(secure, length)
    return validate_response(response, query).to_wire()


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise ConnectionError("upstream closed a DNS-over-TLS frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _servfail(query: dns.message.Message) -> bytes:
    response = dns.message.make_response(query)
    response.set_rcode(dns.rcode.SERVFAIL)
    return response.to_wire(max_size=MAX_RESPONSE_SIZE)


def _udp_wire(response: dns.message.Message, query: dns.message.Message) -> bytes:
    wire = response.to_wire()
    if len(wire) <= MAX_UDP_QUERY_SIZE:
        return wire
    truncated = dns.message.make_response(query)
    truncated.flags = response.flags | dns.flags.TC
    truncated.set_rcode(response.rcode())
    truncated.answer = []
    truncated.authority = []
    truncated.additional = []
    if response.edns >= 0:
        truncated.use_edns(edns=0, ednsflags=response.ednsflags, payload=MAX_UDP_QUERY_SIZE)
    return truncated.to_wire(max_size=MAX_UDP_QUERY_SIZE)


class DNSForwarder:
    def __init__(self, bind_address: str, client_address: str) -> None:
        self.bind_address = str(ipaddress.IPv4Address(bind_address))
        self.client_address = str(ipaddress.IPv4Address(client_address))
        self._limiter = TokenBucket()
        self._active = 0

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPProtocol(self), local_addr=(self.bind_address, LISTEN_PORT)
        )
        tcp_server = await asyncio.start_server(
            self._handle_tcp,
            self.bind_address,
            LISTEN_PORT,
            family=socket.AF_INET,
            limit=MAX_TCP_QUERY_SIZE + 2,
            backlog=16,
        )
        print(
            f"DNS forwarder ready on {self.bind_address}:{LISTEN_PORT} for {self.client_address}",
            flush=True,
        )
        try:
            async with tcp_server:
                await tcp_server.serve_forever()
        finally:
            udp_transport.close()

    def _is_client(self, peer_ip: str) -> bool:
        return peer_ip == self.client_address

    def _reserve_valid_query(self) -> bool:
        if self._active >= MAX_IN_FLIGHT:
            return False
        if not self._limiter.take():
            return False
        self._active += 1
        return True

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if not peer or not self._is_client(str(peer[0])):
            writer.close()
            await writer.wait_closed()
            return
        reserved = False
        try:
            if self._active >= MAX_IN_FLIGHT:
                return
            self._active += 1
            reserved = True
            prefix = await asyncio.wait_for(reader.readexactly(2), timeout=2.0)
            length = struct.unpack("!H", prefix)[0]
            if length < 12 or length > MAX_TCP_QUERY_SIZE:
                return
            packet = await asyncio.wait_for(reader.readexactly(length), timeout=2.0)
            query = validate_query(packet, tcp=True)
            if not self._limiter.take():
                return
            try:
                response = validate_response(await asyncio.to_thread(forward_dot, packet, query), query)
            except Exception:
                response = dns.message.from_wire(_servfail(query))
            wire = response.to_wire(max_size=MAX_RESPONSE_SIZE)
            writer.write(struct.pack("!H", len(wire)) + wire)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, InvalidDNSQueryError, ValueError):
            pass
        finally:
            if reserved:
                self._active -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def accept_udp(self, packet: bytes, peer: tuple[str, int], transport: asyncio.DatagramTransport) -> None:
        if not self._is_client(peer[0]):
            return
        try:
            query = validate_query(packet)
        except InvalidDNSQueryError:
            return
        if not self._reserve_valid_query():
            return
        asyncio.create_task(self._finish_udp(packet, query, peer, transport))

    async def _finish_udp(
        self,
        packet: bytes,
        query: dns.message.Message,
        peer: tuple[str, int],
        transport: asyncio.DatagramTransport,
    ) -> None:
        try:
            try:
                response = validate_response(await asyncio.to_thread(forward_dot, packet, query), query)
            except Exception:
                response = dns.message.from_wire(_servfail(query))
            transport.sendto(_udp_wire(response, query), peer)
        finally:
            self._active -= 1


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: DNSForwarder) -> None:
        self._server = server

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        transport = self._transport
        if isinstance(transport, asyncio.DatagramTransport):
            self._server.accept_udp(data, addr, transport)

    def error_received(self, exc: Exception) -> None:
        return


async def _main(bind_address: str, client_address: str) -> None:
    await DNSForwarder(bind_address, client_address).serve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=True)
    parser.add_argument("--client", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(_main(args.bind, args.client))
    except (OSError, ValueError) as exc:
        print(f"DNS forwarder startup failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
