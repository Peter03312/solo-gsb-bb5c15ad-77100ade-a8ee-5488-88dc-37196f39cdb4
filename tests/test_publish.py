"""脱敏发布凭证与哈希链的端到端测试。"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import publish as publish_mod
from app.main import app
from app.publish import GENESIS_CHAIN

client = TestClient(app)


PAYLOAD = {
    "text": "你好，世界！",
    "fragments": [
        {"id": 1, "weight": 5, "start": 0, "end": 3},
        {"id": 2, "weight": 3, "start": 0, "end": 2},
        {"id": 3, "weight": 1, "start": 2, "end": 5},
    ],
}


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每个测试使用独立的临时凭证库。"""
    db_path = tmp_path / "publish.db"
    monkeypatch.setenv("REDACT_PUBLISH_DB", str(db_path))
    publish_mod.reset_store()
    yield db_path
    publish_mod.reset_store()


# ---------------------------------------------------------------------------
# 发布
# ---------------------------------------------------------------------------


def test_publish_returns_credential_with_sequence_and_chain() -> None:
    resp = client.post("/redact/publish", json=PAYLOAD)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == 1
    # 选段与遮盖规则与 /redact 完全一致。
    assert data["selected_ids"] == [1]
    assert data["rejected_ids"] == [2, 3]
    assert data["masked_text"] == "███世界！"
    assert data["previous_chain_value"] == GENESIS_CHAIN
    for field in ("chain_value", "text_digest", "salt"):
        assert len(data[field]) in (32, 64)
    assert len(data["chain_value"]) == 64
    assert len(data["text_digest"]) == 64
    assert len(data["salt"]) == 32


def test_publish_sequence_increments_and_chains_link() -> None:
    first = client.post("/redact/publish", json=PAYLOAD).json()
    second = client.post("/redact/publish", json=PAYLOAD).json()
    assert first["id"] == 1
    assert second["id"] == 2
    assert second["previous_chain_value"] == first["chain_value"]
    assert second["chain_value"] != first["chain_value"]


def test_same_text_gets_distinct_salted_digests() -> None:
    # 随机盐使同一原文两次发布的摘要不同。
    a = client.post("/redact/publish", json=PAYLOAD).json()
    b = client.post("/redact/publish", json=PAYLOAD).json()
    assert a["salt"] != b["salt"]
    assert a["text_digest"] != b["text_digest"]


@pytest.mark.parametrize(
    "payload",
    [
        {"fragments": []},
        {"text": "abc"},
        {"text": 123, "fragments": []},
        {"text": "abc", "fragments": [
            {"id": 1, "weight": 0, "start": 0, "end": 1},
        ]},
        {"text": "abc", "fragments": [
            {"id": 1, "weight": 1, "start": 0, "end": 4},
        ]},
        {"text": "abc", "fragments": [
            {"id": 1, "weight": 1, "start": 1, "end": 1},
        ]},
    ],
)
def test_publish_reuses_redact_validation(payload: object) -> None:
    resp = client.post("/redact/publish", json=payload)
    assert resp.status_code == 400, payload
    assert "detail" in resp.json()


def test_publish_invalid_json_rejected_with_400() -> None:
    resp = client.post(
        "/redact/publish",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_original_text_is_never_persisted(isolated_db) -> None:
    client.post("/redact/publish", json=PAYLOAD)
    conn = sqlite3.connect(isolated_db)
    rows = conn.execute(
        "SELECT masked_text, intervals, salt_hex, text_digest, prev_chain, chain "
        "FROM publish_credential"
    ).fetchall()
    assert len(rows) == 1
    masked_text, intervals_json, *_ = rows[0]
    assert masked_text == "███世界！"
    intervals = json.loads(intervals_json)
    assert intervals == [{"id": 1, "start": 0, "end": 3}]
    # 库中不得出现被遮盖内容（入选片段覆盖的“你好，”）或完整原文；
    # 未遮盖部分（“世界！”）随遮盖正文保留是预期行为。
    for row in rows:
        for cell in row:
            assert "你好" not in str(cell)
            assert "你好，世界！" not in str(cell)


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------


def _publish() -> dict:
    return client.post("/redact/publish", json=PAYLOAD).json()


def test_verify_accepts_returned_chain() -> None:
    cred = _publish()
    resp = client.get(
        f"/redact/publish/{cred['id']}/verify",
        params={"chain_value": cred["chain_value"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["valid"] is True
    assert data["first_break_id"] is None
    assert data["recomputed_chain_value"] == cred["chain_value"]


def test_verify_chains_across_multiple_publications() -> None:
    first = _publish()
    second = _publish()
    resp = client.get(
        f"/redact/publish/{second['id']}/verify",
        params={"chain_value": second["chain_value"]},
    )
    data = resp.json()
    assert data["valid"] is True
    assert data["first_break_id"] is None
    # 只核验到编号 1 时，重算链值应等于首条链值。
    resp = client.get(
        f"/redact/publish/{first['id']}/verify",
        params={"chain_value": first["chain_value"]},
    )
    assert resp.json()["valid"] is True


def test_verify_wrong_client_chain_reports_no_internal_break() -> None:
    cred = _publish()
    wrong = "0" * 64
    resp = client.get(
        f"/redact/publish/{cred['id']}/verify",
        params={"chain_value": wrong},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["first_break_id"] is None
    assert data["recomputed_chain_value"] == cred["chain_value"]
    assert "客户端留存的链值不符" in data["reason"]


def test_verify_unknown_credential_is_404() -> None:
    _publish()
    resp = client.get(
        "/redact/publish/99/verify", params={"chain_value": "a" * 64}
    )
    assert resp.status_code == 404


def test_verify_malformed_chain_is_400() -> None:
    cred = _publish()
    for bad in ["abc", "g" * 64, "A" * 63, ""]:
        resp = client.get(
            f"/redact/publish/{cred['id']}/verify",
            params={"chain_value": bad},
        )
        assert resp.status_code == 400, bad
    # 缺少 chain_value 同样按统一风格返回 400（而非框架默认 422）。
    resp = client.get(f"/redact/publish/{cred['id']}/verify")
    assert resp.status_code == 400


def test_verify_detects_tampered_record_and_reports_first_break(isolated_db) -> None:
    first = _publish()
    _publish()
    third = _publish()

    # 直接篡改库中第 2 条凭证的遮盖正文（模拟存储层被改）。
    conn = sqlite3.connect(isolated_db)
    conn.execute(
        "UPDATE publish_credential SET masked_text = ? WHERE seq = 2",
        ("篡改",),
    )
    conn.commit()
    conn.close()

    resp = client.get(
        f"/redact/publish/{third['id']}/verify",
        params={"chain_value": third["chain_value"]},
    )
    data = resp.json()
    assert data["valid"] is False
    # 第 1 条仍完好；首个断裂点是被篡改的编号 2。
    assert data["first_break_id"] == 2

    resp_first = client.get(
        f"/redact/publish/{first['id']}/verify",
        params={"chain_value": first["chain_value"]},
    )
    assert resp_first.json()["valid"] is True


def test_verify_detects_forged_chain_column(isolated_db) -> None:
    _publish()
    cred2 = _publish()
    conn = sqlite3.connect(isolated_db)
    conn.execute("UPDATE publish_credential SET chain = ? WHERE seq = 1", ("f" * 64,))
    conn.commit()
    conn.close()

    resp = client.get(
        f"/redact/publish/{cred2['id']}/verify",
        params={"chain_value": cred2["chain_value"]},
    )
    data = resp.json()
    assert data["valid"] is False
    assert data["first_break_id"] == 1


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------


def test_concurrent_publishes_get_contiguous_sequence_and_consistent_chain(
    isolated_db,
) -> None:
    count = 30
    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            data = client.post("/redact/publish", json=PAYLOAD).json()
            with lock:
                results.append(data)
        except Exception as exc:  # noqa: BLE001 - 测试中记录任何线程异常
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == count
    ids = sorted(r["id"] for r in results)
    assert ids == list(range(1, count + 1))
    # 链值互不相同。
    assert len({r["chain_value"] for r in results}) == count

    # 顺序读取库中每条链值并逐号重放，必须全部自洽。
    conn = sqlite3.connect(isolated_db)
    stored = dict(conn.execute("SELECT seq, chain FROM publish_credential").fetchall())
    conn.close()
    store = publish_mod.get_store()
    for seq in range(1, count + 1):
        verdict = store.verify(seq, stored[seq])
        assert verdict is not None
        assert verdict.valid is True, seq
        assert verdict.first_break_seq is None
