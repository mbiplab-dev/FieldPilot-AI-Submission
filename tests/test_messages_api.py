"""Direct manager↔worker messaging: routing, isolation, voice notes and unread counts.

The isolation tests matter most. A worker must never be able to read a colleague's conversation
with the site manager, and the API is the only thing enforcing that.
"""

from __future__ import annotations

import io

import pytest

from .conftest import MANAGER, WORKER1, WORKER2
from .conftest import login as _login


def _m4a() -> bytes:
    """Plausible bytes for a voice note. Content is never parsed — only the extension is checked."""

    return b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64


def _send_from_worker(client, headers, text="", audio=False):
    files = {"audio": ("note.m4a", io.BytesIO(_m4a()), "audio/mp4")} if audio else None
    return client.post("/me/messages", data={"text": text}, files=files, headers=headers)


def _send_from_manager(client, headers, worker_id, text="", audio=False):
    files = {"audio": ("reply.m4a", io.BytesIO(_m4a()), "audio/mp4")} if audio else None
    return client.post(f"/messages/{worker_id}", data={"text": text}, files=files, headers=headers)


# --------------------------------------------------------------------------- round trip


def test_a_worker_and_manager_hold_one_conversation(client):
    _, wh = _login(client, WORKER1)
    _, mh = _login(client, MANAGER)

    assert _send_from_worker(client, wh, "The rebar here looks wrong.").status_code == 201
    assert _send_from_manager(client, mh, "w-1", "Stay clear, I'm coming over.").status_code == 201

    thread = client.get("/me/messages", headers=wh).json()["messages"]
    assert [m["sender_role"] for m in thread] == ["worker", "site_manager"]
    assert thread[0]["text"] == "The rebar here looks wrong."
    assert thread[1]["text"] == "Stay clear, I'm coming over."
    # Oldest first — the order a conversation is read in.
    assert thread[0]["created_at"] <= thread[1]["created_at"]

    # The manager reads the same thread by worker id.
    same = client.get("/messages/w-1", headers=mh).json()["messages"]
    assert [m["message_id"] for m in same] == [m["message_id"] for m in thread]


def test_a_voice_note_comes_back_as_a_playable_url(client):
    _, wh = _login(client, WORKER1)

    body = _send_from_worker(client, wh, audio=True).json()
    assert body["audio_url"] and body["audio_url"].startswith("/uploads/")
    assert body["audio_url"].endswith(".m4a")
    # The filesystem path is never exposed.
    assert "audio_path" not in body

    served = client.get(body["audio_url"])
    assert served.status_code == 200
    assert served.content.startswith(b"\x00\x00\x00 ftyp")


def test_a_voice_note_needs_no_text(client):
    _, wh = _login(client, WORKER1)
    # Gloves on, loud site: holding a button must be enough.
    assert _send_from_worker(client, wh, text="", audio=True).status_code == 201


def test_an_empty_message_is_rejected(client):
    _, wh = _login(client, WORKER1)
    _, mh = _login(client, MANAGER)
    assert _send_from_worker(client, wh, text="   ").status_code == 400
    assert _send_from_manager(client, mh, "w-1", text="").status_code == 400


def test_a_non_audio_upload_is_refused_as_a_voice_note(client):
    _, wh = _login(client, WORKER1)
    r = client.post(
        "/me/messages",
        data={"text": "hi"},
        files={"audio": ("payload.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        headers=wh,
    )
    assert r.status_code == 400
    assert "unsupported" in r.text.lower()


# --------------------------------------------------------------------------- isolation


def test_a_worker_cannot_read_another_workers_thread(client):
    _, w1 = _login(client, WORKER1)
    _, w2 = _login(client, WORKER2)
    _send_from_worker(client, w1, "something private")

    assert client.get("/messages/w-1", headers=w2).status_code == 403
    # ...and their own thread does not leak it.
    assert client.get("/me/messages", headers=w2).json()["messages"] == []


def test_a_worker_cannot_mark_another_workers_thread_read(client):
    _, w1 = _login(client, WORKER1)
    _, w2 = _login(client, WORKER2)
    _send_from_worker(client, w1, "hello")
    assert client.post("/messages/w-1/read", headers=w2).status_code == 403


def test_a_worker_cannot_write_into_another_workers_thread(client):
    """The worker send endpoint takes no worker id — it is always derived from the token."""

    _, w2 = _login(client, WORKER2)
    assert _send_from_manager(client, w2, "w-1", "impersonation attempt").status_code == 403


def test_a_worker_cannot_list_every_thread(client):
    _, w1 = _login(client, WORKER1)
    assert client.get("/messages/threads", headers=w1).status_code == 403


def test_unauthenticated_requests_are_refused(client):
    assert client.get("/messages/threads").status_code == 401
    assert client.get("/me/messages").status_code == 401
    assert client.post("/me/messages", data={"text": "hi"}).status_code == 401


# --------------------------------------------------------------------------- inbox + unread


def test_the_manager_inbox_summarises_each_thread_newest_first(client):
    _, w1 = _login(client, WORKER1)
    _, w2 = _login(client, WORKER2)
    _, mh = _login(client, MANAGER)

    _send_from_worker(client, w1, "first from ravi")
    _send_from_worker(client, w2, "later from anita")

    threads = client.get("/messages/threads", headers=mh).json()["threads"]
    assert [t["worker_id"] for t in threads] == ["w-2", "w-1"]

    anita = threads[0]
    assert anita["last_text"] == "later from anita"
    assert anita["last_sender_role"] == "worker"
    assert anita["messages"] == 1
    assert anita["unread"] == 1
    assert anita["worker_name"] == "Anita Sharma"


def test_unread_counts_only_the_other_sides_messages(client):
    _, wh = _login(client, WORKER1)
    _, mh = _login(client, MANAGER)

    _send_from_worker(client, wh, "one")
    _send_from_worker(client, wh, "two")
    assert client.get("/messages/unread", headers=mh).json()["unread"] == 2
    # The manager's own messages are not counted back at them.
    assert client.get("/messages/unread", headers=wh).json()["unread"] == 0

    _send_from_manager(client, mh, "w-1", "acknowledged")
    assert client.get("/messages/unread", headers=wh).json()["unread"] == 1
    assert client.get("/messages/unread", headers=mh).json()["unread"] == 2


def test_marking_a_thread_read_clears_only_the_other_sides_messages(client):
    _, wh = _login(client, WORKER1)
    _, mh = _login(client, MANAGER)

    _send_from_worker(client, wh, "worker says hi")
    _send_from_manager(client, mh, "w-1", "manager replies")

    assert client.post("/messages/w-1/read", headers=mh).json()["marked_read"] == 1
    assert client.get("/messages/unread", headers=mh).json()["unread"] == 0
    # The worker still has the manager's reply unread.
    assert client.get("/messages/unread", headers=wh).json()["unread"] == 1

    assert client.post("/messages/w-1/read", headers=wh).json()["marked_read"] == 1
    assert client.get("/messages/unread", headers=wh).json()["unread"] == 0


def test_marking_read_twice_is_a_no_op(client):
    _, wh = _login(client, WORKER1)
    _, mh = _login(client, MANAGER)
    _send_from_worker(client, wh, "hello")

    assert client.post("/messages/w-1/read", headers=mh).json()["marked_read"] == 1
    assert client.post("/messages/w-1/read", headers=mh).json()["marked_read"] == 0


def test_an_empty_inbox_is_an_empty_list_not_an_error(client):
    _, mh = _login(client, MANAGER)
    assert client.get("/messages/threads", headers=mh).json()["threads"] == []
    assert client.get("/messages/unread", headers=mh).json()["unread"] == 0
    assert client.get("/messages/w-1", headers=mh).json()["messages"] == []


@pytest.mark.parametrize("path", ["/messages/threads", "/messages/unread"])
def test_the_fixed_routes_are_not_shadowed_by_the_worker_id_route(client, path):
    """`/messages/threads` must not be read as a thread for a worker called "threads"."""

    _, mh = _login(client, MANAGER)
    body = client.get(path, headers=mh).json()
    assert "worker_id" not in body
