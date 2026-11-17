import dns.edns
import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype
import pytest

from ops.worker.dns_forwarder import (
    InvalidDNSQueryError,
    TokenBucket,
    validate_query,
    validate_response,
)


def test_valid_standard_and_empty_edns_queries_are_accepted() -> None:
    for query in (dns.message.make_query("example.com", "A"), dns.message.make_query("www.example.com", "AAAA")):
        assert validate_query(query.to_wire()).question == query.question

    edns_query = dns.message.make_query("example.com", "A", use_edns=0, payload=1232)
    assert validate_query(edns_query.to_wire()).question == edns_query.question

    cookie_query = dns.message.make_query("example.com", "A", use_edns=0, payload=1232)
    cookie_query.use_edns(edns=0, payload=1232, options=[dns.edns.CookieOption(b"12345678", b"")])
    assert validate_query(cookie_query.to_wire()).question == cookie_query.question


@pytest.mark.parametrize(
    "packet",
    [
        b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",
        b"\x00" * 11,
        b"\x00" * 1233,
    ],
)
def test_malformed_and_non_dns_payloads_are_rejected(packet: bytes) -> None:
    with pytest.raises(InvalidDNSQueryError):
        validate_query(packet)


def test_dns_responses_must_match_the_question_and_id() -> None:
    query = dns.message.make_query("example.com", "A")
    response = dns.message.make_response(query)
    assert validate_response(response.to_wire(), query).question == query.question

    wrong_id = dns.message.make_response(query)
    wrong_id.id ^= 1
    with pytest.raises(ValueError, match="does not match"):
        validate_response(wrong_id.to_wire(), query)

    wrong_question = dns.message.make_response(dns.message.make_query("other.example", "A"))
    with pytest.raises(ValueError, match="does not match"):
        validate_response(wrong_question.to_wire(), query)


def test_transfer_and_any_queries_are_rejected() -> None:
    for qtype in (dns.rdatatype.AXFR, dns.rdatatype.IXFR, dns.rdatatype.ANY):
        with pytest.raises(InvalidDNSQueryError):
            validate_query(dns.message.make_query("example.com", qtype).to_wire())


def test_unapproved_edns_options_are_rejected() -> None:
    query = dns.message.make_query("example.com", "A", use_edns=0, payload=1232)
    query.use_edns(edns=0, payload=1232, options=[dns.edns.GenericOption(12, b"\x00")])

    with pytest.raises(InvalidDNSQueryError, match="unsupported EDNS"):
        validate_query(query.to_wire())


def test_rate_limiter_enforces_burst_and_refill_without_external_calls() -> None:
    now = [0.0]
    bucket = TokenBucket(capacity=2, refill_per_second=1, monotonic=lambda: now[0])
    assert bucket.take()
    assert bucket.take()
    assert not bucket.take()
    now[0] += 1.0
    assert bucket.take()


def test_servfail_is_a_valid_dns_response() -> None:
    from ops.worker.dns_forwarder import _servfail

    query = dns.message.make_query("example.com", "A")
    response = dns.message.from_wire(_servfail(query))
    assert response.flags & dns.flags.QR
    assert response.rcode() == dns.rcode.SERVFAIL
