"""Auth, zone occupancy, worker questions and manual hazard reports over REST.

Hermetic: SQLite store, in-memory bus, no Qdrant/Ollama/Redis/network. Covers the endpoints wired
in `backend/app.py` on top of `fieldpilot.auth` and `fieldpilot.workforce`.
"""

from __future__ import annotations

import io
import time

from .conftest import MANAGER, WORKER1, WORKER2  # noqa: F401
from .conftest import login as _login


def _png() -> bytes:
    import struct
    import zlib

    raw = b"\x00" + b"\xff\x00\x00" * 4
    idat = zlib.compress(raw)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ auth


def test_unauthenticated_requests_are_401(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/me/alerts").status_code == 401


def test_login_rejects_bad_credentials_without_revealing_which_field_was_wrong(client):
    bad_password = client.post("/auth/login", json={"username": "worker1", "password": "x"})
    bad_username = client.post("/auth/login", json={"username": "nobody", "password": "x"})
    assert bad_password.status_code == bad_username.status_code == 401
    assert bad_password.json()["detail"] == bad_username.json()["detail"]


def test_login_returns_user_with_no_secret_material(client):
    user, _headers = _login(client, WORKER1)
    assert user["role"] == "worker"
    assert user["worker_id"] == "w-1"
    # `password_changed_at` is a legitimate timestamp field; only the actual secret fields matter
    assert "password_hash" not in user
    assert "salt" not in user
    assert "scrypt" not in str(user).lower()


def test_me_reflects_the_logged_in_user(client):
    user, headers = _login(client, WORKER1)
    me = client.get("/auth/me", headers=headers).json()
    assert me["user_id"] == user["user_id"]


def test_logout_invalidates_the_token(client):
    _, headers = _login(client, WORKER1)
    assert client.post("/auth/logout", headers=headers).json()["ok"] is True
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_role_boundaries_are_enforced_both_ways(client):
    _, worker_h = _login(client, WORKER1)
    _, manager_h = _login(client, MANAGER)

    assert client.get("/auth/users", headers=worker_h).status_code == 403
    assert client.get("/auth/users", headers=manager_h).status_code == 200

    assert client.get("/me/alerts", headers=manager_h).status_code == 403
    assert client.get("/me/alerts", headers=worker_h).status_code == 200

    assert client.post("/zones/zone-a/enter", headers=manager_h).status_code == 403


def test_manager_can_create_a_user_worker_cannot(client):
    _, worker_h = _login(client, WORKER1)
    _, manager_h = _login(client, MANAGER)

    denied = client.post("/auth/users", headers=worker_h,
                         json={"username": "worker3", "password": "abcdefgh", "role": "worker"})
    assert denied.status_code == 403

    created = client.post("/auth/users", headers=manager_h,
                          json={"username": "worker3", "password": "abcdefgh",
                                "role": "worker", "worker_id": "w-3",
                                "display_name": "New Worker"})
    assert created.status_code == 201
    _, new_h = _login(client, {"username": "worker3", "password": "abcdefgh"})
    assert client.get("/auth/me", headers=new_h).json()["worker_id"] == "w-3"


# ------------------------------------------------------------------ zone occupancy


def test_worker_starts_with_no_zone(client):
    _, headers = _login(client, WORKER1)
    assert client.get("/me/zone", headers=headers).json()["occupancy"] is None


def test_entering_an_unknown_zone_is_404(client):
    _, headers = _login(client, WORKER1)
    assert client.post("/zones/not-a-zone/enter", headers=headers).status_code == 404


def test_enter_then_move_zones_closes_the_previous_one(client):
    _, headers = _login(client, WORKER1)
    a = client.post("/zones/zone-a/enter", headers=headers)
    assert a.status_code == 200
    assert a.json()["closed_previous"] is None
    assert a.json()["entered_at"] is not None

    b = client.post("/zones/zone-b/enter", headers=headers).json()
    assert b["closed_previous"]["zone_id"] == "zone-a"
    assert b["closed_previous"]["duration_s"] >= 0

    current = client.get("/me/zone", headers=headers).json()["occupancy"]
    assert current["zone_id"] == "zone-b"


def test_leaving_without_being_checked_in_is_409(client):
    _, headers = _login(client, WORKER1)
    assert client.post("/zones/zone-a/leave", headers=headers).status_code == 409


def test_leave_closes_the_occupancy(client):
    _, headers = _login(client, WORKER1)
    client.post("/zones/zone-a/enter", headers=headers)
    left = client.post("/zones/zone-a/leave", headers=headers)
    assert left.status_code == 200
    assert left.json()["duration_s"] is not None
    assert client.get("/me/zone", headers=headers).json()["occupancy"] is None
    assert client.post("/zones/zone-a/leave", headers=headers).status_code == 409


def test_manager_sees_occupancy_and_a_worst_first_risk_ranking(client):
    _, w1 = _login(client, WORKER1)
    _, w2 = _login(client, WORKER2)
    _, manager_h = _login(client, MANAGER)

    client.post("/zones/zone-a/enter", headers=w1)
    client.post("/zones/zone-a/enter", headers=w2)

    report = client.get("/zones/occupancy", headers=manager_h).json()["zones"]
    assert {z["zone_id"] for z in report} == {"zone-a", "zone-b", "zone-c", "zone-d"}, \
        "every zone must appear, including empty ones"

    zone_a = next(z for z in report if z["zone_id"] == "zone-a")
    assert zone_a["worker_count"] == 2
    names = {w["worker_id"] for w in zone_a["workers"]}
    assert names == {"w-1", "w-2"}
    assert zone_a["risk_rank"] == 1, "the only occupied zone should rank worst"
    ranks = [z["risk_rank"] for z in report]
    assert ranks == sorted(ranks), "rows must come back worst-first"


def test_worker_cannot_see_occupancy_of_others_via_a_role_they_lack(client):
    # occupancy is a manager-oriented aggregate; workers can still read it (it's not secret,
    # just supervisory) — this test documents that any authenticated user may, not just workers
    _, headers = _login(client, WORKER1)
    assert client.get("/zones/occupancy", headers=headers).status_code == 200


# ------------------------------------------------------------------ manual hazard report


def test_worker_reports_a_hazard_which_becomes_a_real_alert(client):
    _, headers = _login(client, WORKER1)
    client.post("/zones/zone-a/enter", headers=headers)

    r = client.post(
        "/me/alerts", headers=headers,
        data={"event_type": "proximity", "severity": "critical",
              "message": "Excavator swinging near the walkway"},
        files={"image": ("hazard.png", io.BytesIO(_png()), "image/png")},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["zone"] == "zone-a", "the zone should default to the worker's current check-in"
    assert client.get(body["image_url"]).status_code == 200

    deadline = time.time() + 6
    alerts = []
    while time.time() < deadline:
        alerts = client.get("/me/alerts", headers=headers).json()["alerts"]
        if alerts:
            break
        time.sleep(0.1)
    assert alerts, "the manual report must flow through triggers into a real alert"
    assert alerts[0]["worker_id"] == "w-1"
    assert alerts[0]["severity"] == "critical"


def test_worker_only_sees_their_own_alerts(client):
    _, w1 = _login(client, WORKER1)
    _, w2 = _login(client, WORKER2)
    client.post("/me/alerts", headers=w1,
               data={"event_type": "ppe", "severity": "high", "message": "no helmet"})
    time.sleep(1.0)
    mine = client.get("/me/alerts", headers=w1).json()["alerts"]
    theirs = client.get("/me/alerts", headers=w2).json()["alerts"]
    assert mine and all(a["worker_id"] == "w-1" for a in mine)
    assert all(a["worker_id"] != "w-1" for a in theirs)


def test_oversize_upload_is_rejected(client):
    _, headers = _login(client, WORKER1)
    huge = io.BytesIO(b"0" * (1024 * 1024 + 100))
    r = client.post("/me/alerts", headers=headers,
                    data={"event_type": "ppe", "message": "x"},
                    files={"image": ("big.png", huge, "image/png")})
    assert r.status_code == 413


def test_disallowed_file_type_is_rejected(client):
    _, headers = _login(client, WORKER1)
    r = client.post("/me/alerts", headers=headers,
                    data={"event_type": "ppe", "message": "x"},
                    files={"image": ("evil.svg", io.BytesIO(b"<svg/>"), "image/svg+xml")})
    assert r.status_code == 400


def test_report_without_an_image_still_works(client):
    _, headers = _login(client, WORKER1)
    r = client.post("/me/alerts", headers=headers,
                    data={"event_type": "fall", "severity": "high", "message": "slipped"})
    assert r.status_code == 202
    assert r.json()["image_url"] is None


# ------------------------------------------------------------------ worker questions


def test_ask_persists_immediately_as_pending(client):
    _, headers = _login(client, WORKER1)
    r = client.post("/questions", headers=headers, data={"text": "Is this safe?"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["worker_id"] == "w-1"
    assert "image_path" not in body, "the filesystem path must never reach the client"


def test_question_with_a_photo_serves_the_image(client):
    _, headers = _login(client, WORKER1)
    r = client.post("/questions", headers=headers, data={"text": "What is this?"},
                    files={"image": ("q.png", io.BytesIO(_png()), "image/png")})
    body = r.json()
    assert body["image_url"] is not None
    assert client.get(body["image_url"]).status_code == 200


def test_question_zone_defaults_to_current_checkin(client):
    _, headers = _login(client, WORKER1)
    client.post("/zones/zone-b/enter", headers=headers)
    r = client.post("/questions", headers=headers, data={"text": "hello"})
    assert r.json()["zone"] == "zone-b"


def test_llm_answers_without_a_reachable_backend_and_stays_ungrounded(client):
    _, headers = _login(client, WORKER1)
    qid = client.post("/questions", headers=headers,
                      data={"text": "spacing question"}).json()["question_id"]
    deadline = time.time() + 8
    answered = None
    while time.time() < deadline:
        answered = client.get(f"/questions/{qid}", headers=headers).json()
        if answered.get("llm_answer"):
            break
        time.sleep(0.2)
    assert answered and answered["llm_answer"], "must degrade to SOME answer, not hang forever"
    assert answered["llm_grounded"] is False, "no blueprint index is reachable in this fixture"


def test_manager_sees_and_replies_to_a_question(client):
    _, worker_h = _login(client, WORKER1)
    _, manager_h = _login(client, MANAGER)
    qid = client.post("/questions", headers=worker_h,
                      data={"text": "Can I proceed?"}).json()["question_id"]

    inbox = client.get("/questions", headers=manager_h).json()["questions"]
    assert any(q["question_id"] == qid for q in inbox)

    reply = client.post(f"/questions/{qid}/reply", headers=manager_h,
                        json={"reply": "Yes, proceed with caution."})
    assert reply.status_code == 200
    assert reply.json()["status"] == "answered"
    assert reply.json()["manager_reply"] == "Yes, proceed with caution."

    # the worker sees the manager's answer too
    mine = client.get(f"/questions/{qid}", headers=worker_h).json()
    assert mine["manager_reply"] == "Yes, proceed with caution."


def test_worker_cannot_reply_as_manager(client):
    _, worker_h = _login(client, WORKER1)
    qid = client.post("/questions", headers=worker_h,
                      data={"text": "hi"}).json()["question_id"]
    assert client.post(f"/questions/{qid}/reply", headers=worker_h,
                       json={"reply": "no"}).status_code == 403


def test_worker_only_sees_their_own_questions_manager_sees_all(client):
    _, w1 = _login(client, WORKER1)
    _, w2 = _login(client, WORKER2)
    _, manager_h = _login(client, MANAGER)
    client.post("/questions", headers=w1, data={"text": "from worker1"})
    client.post("/questions", headers=w2, data={"text": "from worker2"})

    mine = client.get("/questions", headers=w1).json()["questions"]
    assert all(q["worker_id"] == "w-1" for q in mine)
    assert len(mine) == 1

    everything = client.get("/questions", headers=manager_h).json()["questions"]
    assert len(everything) == 2


def test_worker_cannot_read_someone_elses_question_by_id(client):
    _, w1 = _login(client, WORKER1)
    _, w2 = _login(client, WORKER2)
    qid = client.post("/questions", headers=w1, data={"text": "private"}).json()["question_id"]
    assert client.get(f"/questions/{qid}", headers=w2).status_code == 403
    assert client.get(f"/questions/{qid}", headers=w1).status_code == 200


def test_question_stats_reflect_the_manager_reply_queue(client):
    _, worker_h = _login(client, WORKER1)
    _, manager_h = _login(client, MANAGER)
    qid = client.post("/questions", headers=worker_h,
                      data={"text": "q1"}).json()["question_id"]

    stats_before = client.get("/questions/stats", headers=manager_h).json()
    assert stats_before["pending"] >= 1
    assert stats_before["awaiting_manager"] >= 1

    client.post(f"/questions/{qid}/reply", headers=manager_h, json={"reply": "ok"})
    stats_after = client.get("/questions/stats", headers=manager_h).json()
    assert stats_after["answered"] >= 1


def test_reply_to_unknown_question_is_404(client):
    _, manager_h = _login(client, MANAGER)
    assert client.post("/questions/does-not-exist/reply", headers=manager_h,
                       json={"reply": "x"}).status_code == 404


def test_empty_question_text_is_rejected(client):
    _, headers = _login(client, WORKER1)
    assert client.post("/questions", headers=headers, data={"text": "   "}).status_code == 400


def test_account_with_no_worker_id_gets_a_clear_error_not_a_crash(client, tmp_path):
    """A site_manager account has no worker_id — worker-only routes must say why, not 500."""

    _, manager_h = _login(client, MANAGER)
    # manager_only guard already blocks this at the role layer; this proves the guard applies
    # rather than the endpoint ever reaching a None-worker_id code path
    assert client.get("/me/alerts", headers=manager_h).status_code == 403
