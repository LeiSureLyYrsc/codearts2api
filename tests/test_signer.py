"""Tests for the official SDK-HMAC-SHA256 signer.

Expected canonical requests are handwritten from
https://support.huaweicloud.com/devg-apisign/api-sign-algorithm-001.html
The HMAC/SHA-256 values are derived from those strings.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from codearts2api.signing import (
    ALGORITHM,
    EMPTY_PAYLOAD_HASH,
    canonical_query_string,
    canonical_uri,
    sign_request,
    uri_encode,
)

AK = "access-key-id-example"
SK = "secret-access-key-example"
DATE = "20190318T094751Z"
HOST = "snap-access.cn-north-4.myhuaweicloud.com"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hmac_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def test_empty_payload_hash_is_sha256_of_empty_string() -> None:
    assert EMPTY_PAYLOAD_HASH == _sha256_hex("")
    assert EMPTY_PAYLOAD_HASH == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_canonical_uri_adds_trailing_slash_for_signing() -> None:
    assert canonical_uri("/v1/model/builtin") == "/v1/model/builtin/"
    assert canonical_uri("/") == "/"
    assert canonical_uri("") == "/"


def test_canonical_uri_normalizes_dot_segments() -> None:
    assert canonical_uri("/a/./b/../c") == "/a/c/"
    assert canonical_uri("/v1/ops/delivery") == "/v1/ops/delivery/"


def test_canonical_uri_encodes_segments() -> None:
    assert canonical_uri("/path/a b") == "/path/a%20b/"


def test_uri_encode_rfc3986() -> None:
    assert uri_encode("AZaz09-_.~") == "AZaz09-_.~"
    assert uri_encode("a b") == "a%20b"
    assert uri_encode("*") == "%2A"
    assert uri_encode("/") == "%2F"


def test_canonical_query_string_sort_and_empty_value() -> None:
    # Uppercase names sort before lowercase (character code).
    assert canonical_query_string("b=2&A=1") == "A=1&b=2"
    assert canonical_query_string("parm1=value1&parm2=") == "parm1=value1&parm2="
    assert canonical_query_string("channel=IDE") == "channel=IDE"
    assert canonical_query_string("q=a b") == "q=a%20b"
    assert canonical_query_string("") == ""


def test_sign_get_canonical_request_and_authorization_shape() -> None:
    url = f"https://{HOST}/v1/model/builtin"
    signed = sign_request(method="GET", url=url, ak=AK, sk=SK, sdk_date=DATE)

    expected_canonical = (
        "GET\n"
        "/v1/model/builtin/\n"
        "\n"
        f"host:{HOST}\n"
        f"x-sdk-date:{DATE}\n"
        "\n"
        "host;x-sdk-date\n"
        f"{EMPTY_PAYLOAD_HASH}"
    )
    assert signed.canonical_request == expected_canonical

    expected_sts = f"{ALGORITHM}\n{DATE}\n{_sha256_hex(expected_canonical)}"
    assert signed.string_to_sign == expected_sts
    assert signed.signature == _hmac_hex(SK, expected_sts)

    assert signed.authorization == (
        f"{ALGORITHM} Access={AK}, "
        f"SignedHeaders=host;x-sdk-date, "
        f"Signature={signed.signature}"
    )
    # Official shape: space (no comma) between algorithm and Access=; commas before the rest.
    assert re.match(
        r"^SDK-HMAC-SHA256 Access=[^,\s]+, SignedHeaders=[a-z0-9;-]+, Signature=[0-9a-f]{64}$",
        signed.authorization,
    )
    assert signed.headers["Authorization"] == signed.authorization
    assert signed.headers["X-Sdk-Date"] == DATE
    assert signed.headers["Host"] == HOST
    # The request URL on the wire is unchanged (no forced trailing slash).
    assert signed.url == url


def test_sign_get_with_query() -> None:
    url = f"https://{HOST}/v1/ops/delivery?channel=IDE"
    signed = sign_request(method="GET", url=url, ak=AK, sk=SK, sdk_date=DATE)
    expected_canonical = (
        "GET\n"
        "/v1/ops/delivery/\n"
        "channel=IDE\n"
        f"host:{HOST}\n"
        f"x-sdk-date:{DATE}\n"
        "\n"
        "host;x-sdk-date\n"
        f"{EMPTY_PAYLOAD_HASH}"
    )
    assert signed.canonical_request == expected_canonical


def test_sign_post_includes_content_type_and_payload_hash() -> None:
    body = '{"campaignId":1,"channel":"IDE"}'
    url = f"https://{HOST}/v1/ops/claim"
    signed = sign_request(
        method="POST",
        url=url,
        ak=AK,
        sk=SK,
        sdk_date=DATE,
        body=body,
        headers={"Content-Type": "application/json"},
    )
    payload_hash = _sha256_hex(body)
    expected_canonical = (
        "POST\n"
        "/v1/ops/claim/\n"
        "\n"
        "content-type:application/json\n"
        f"host:{HOST}\n"
        f"x-sdk-date:{DATE}\n"
        "\n"
        "content-type;host;x-sdk-date\n"
        f"{payload_hash}"
    )
    assert signed.canonical_request == expected_canonical
    assert signed.body == body.encode("utf-8")
    assert "content-type" in signed.signed_headers


def test_security_token_is_a_signed_header() -> None:
    signed = sign_request(
        method="GET",
        url=f"https://{HOST}/v1/model/builtin",
        ak=AK,
        sk=SK,
        sdk_date=DATE,
        security_token="temporary-sts-token",
    )
    assert "x-security-token:temporary-sts-token" in signed.canonical_request
    assert "x-security-token" in signed.signed_headers.split(";")
    assert signed.headers["X-Security-Token"] == "temporary-sts-token"
    assert signed.signed_headers == "host;x-sdk-date;x-security-token"


def test_project_id_is_a_signed_header() -> None:
    signed = sign_request(
        method="GET",
        url=f"https://{HOST}/v1/model/builtin",
        ak=AK,
        sk=SK,
        sdk_date=DATE,
        project_id="project-123",
    )
    assert "x-project-id:project-123" in signed.canonical_request
    assert "x-project-id" in signed.signed_headers.split(";")


def test_unsigned_after_headers_are_not_in_canonical_request() -> None:
    signed = sign_request(
        method="GET",
        url=f"https://{HOST}/v1/model/builtin",
        ak=AK,
        sk=SK,
        sdk_date=DATE,
        extra_unsigned_headers={"X-Language": "zh-cn"},
    )
    assert "x-language" not in signed.canonical_request
    assert "x-language" not in signed.signed_headers.split(";")
    assert signed.headers["X-Language"] == "zh-cn"
    assert signed.signed_headers == "host;x-sdk-date"


def test_header_value_trims_only_outer_spaces() -> None:
    signed = sign_request(
        method="GET",
        url=f"https://{HOST}/v1/model/builtin",
        ak=AK,
        sk=SK,
        sdk_date=DATE,
        headers={"My-header1": "    a   b   c  "},
    )
    assert "my-header1:a   b   c\n" in signed.canonical_request
