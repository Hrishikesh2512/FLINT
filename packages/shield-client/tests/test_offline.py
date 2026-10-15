"""The client's own behaviour, with no phone: what it sends, how it fails."""

import base64
import json

import pytest

from shield_client import Shield, ShieldError


class Response:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content = status, body or {}, content
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class Session:
    def __init__(self, reply):
        self.headers, self.sent, self._reply = {}, [], reply

    def request(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs))
        return self._reply(method, url, kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


ITEM = {"id": "1", "kind": "note", "name": "n", "parent": None, "created_by": "bot"}


def test_every_request_carries_the_token_and_who_is_asking():
    session = Session(lambda *_: Response(body={"ok": True}))
    Shield("http://phone:8791/", "secret", agent="bot", session=session).health()
    assert session.headers == {"Authorization": "Bearer secret", "X-Shield-Agent": "bot"}
    assert session.sent[0][1] == "http://phone:8791/v1/health"


def test_small_files_go_inline_and_big_ones_raw():
    session = Session(lambda *_: Response(201, ITEM))
    shield = Shield("http://p", "t", agent="bot", session=session)
    shield.upload("/A", "small.bin", b"abc")
    method, url, kwargs = session.sent[-1]
    assert url.endswith("/v1/items")
    assert kwargs["json"]["data_b64"] == base64.b64encode(b"abc").decode()
    shield.upload("/A", "big.bin", b"x" * (5 * 1024 * 1024))
    method, url, kwargs = session.sent[-1]
    assert url.endswith("/v1/upload") and kwargs["params"]["path"] == "/A"
    assert len(kwargs["data"]) > 4e6


def test_shield_saying_no_is_an_error_with_its_reason():
    session = Session(lambda *_: Response(401, {"error": "a valid Shield token is needed"}))
    with pytest.raises(ShieldError, match="token is needed") as no:
        Shield("http://p", "bad", agent="bot", session=session).health()
    assert no.value.status == 401
