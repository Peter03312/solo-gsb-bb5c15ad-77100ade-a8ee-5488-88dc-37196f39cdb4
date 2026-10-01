"""脱敏发布凭证的存储与哈希链（与 HTTP 框架解耦）。

凭证只持久化可以公开的内容：

- 遮盖正文 ``masked_text``；
- 入选区间（``id``/``start``/``end``）；
- 带随机盐的原文 SHA-256 摘要与盐本身。

**原文不落盘**：服务端无法从库中还原原文，摘要仅供持有原文的客户端
自行复核。

每条凭证带一个单调递增编号 ``seq`` 与链值 ``chain``：

    chain_0 = SHA256(b"redact-publish-v1:genesis")            （创世链值）
    chain_n = SHA256(chain_{n-1} || 规范化凭证内容_n)

编号与链值在 SQLite 的 ``BEGIN IMMEDIATE`` 写事务内计算并插入，
事务持锁期间完成“读取上一链值 → 计算新链值 → 写入”全过程，
因此并发请求的编号严格递增、链顺序严格一致（跨进程由 SQLite
写锁串行化，跨线程另由进程内锁串行化）。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from typing import Sequence

from .redaction import Fragment

# 域分隔前缀，避免与其他协议的哈希输入混用。
_DOMAIN = b"redact-publish-v1"

#: 创世链值：第 1 条凭证的“前一链值”。
GENESIS_CHAIN = hashlib.sha256(_DOMAIN + b":genesis").hexdigest()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_credential (
    seq          INTEGER PRIMARY KEY,
    masked_text  TEXT    NOT NULL,
    intervals    TEXT    NOT NULL,
    salt_hex     TEXT    NOT NULL,
    text_digest  TEXT    NOT NULL,
    prev_chain   TEXT    NOT NULL,
    chain        TEXT    NOT NULL
)
"""


def default_db_path() -> str:
    """凭证库路径，可用环境变量 ``REDACT_PUBLISH_DB`` 覆盖。"""
    return os.environ.get("REDACT_PUBLISH_DB", "data/publish.db")


def _canonical_dumps(obj: object) -> str:
    """确定性 JSON 序列化：键排序、紧凑分隔、不转义非 ASCII。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def digest_original(text: str) -> tuple[str, str]:
    """计算带 16 字节随机盐的原文 SHA-256 摘要。

    返回 ``(盐的十六进制, 摘要的十六进制)``。哈希输入采用长度前缀
    分帧，避免盐与原文边界歧义；每次调用盐都不同，同一原文两次
    发布的摘要也不同。
    """
    salt = secrets.token_bytes(16)
    payload = text.encode("utf-8")
    h = hashlib.sha256()
    h.update(_DOMAIN)
    h.update(b":salted-sha256\n")
    h.update(len(salt).to_bytes(2, "big"))
    h.update(salt)
    h.update(len(payload).to_bytes(8, "big"))
    h.update(payload)
    return salt.hex(), h.hexdigest()


def canonical_record(
    seq: int,
    masked_text: str,
    intervals: Sequence[dict[str, object]],
    salt_hex: str,
    text_digest: str,
) -> bytes:
    """构造参与链值计算的规范化凭证内容（UTF-8 字节）。"""
    record = {
        "seq": seq,
        "masked_text": masked_text,
        "intervals": list(intervals),
        "salt": salt_hex,
        "text_digest": text_digest,
    }
    return _DOMAIN + b":record\n" + _canonical_dumps(record).encode("utf-8")


def compute_chain(
    prev_chain: str,
    seq: int,
    masked_text: str,
    intervals: Sequence[dict[str, object]],
    salt_hex: str,
    text_digest: str,
) -> str:
    """由前一链值与规范化凭证内容计算新链值。"""
    material = (
        prev_chain.encode("ascii")
        + b"\n"
        + canonical_record(seq, masked_text, intervals, salt_hex, text_digest)
    )
    return hashlib.sha256(material).hexdigest()


def selected_intervals(selected: Sequence[Fragment]) -> list[dict[str, object]]:
    """把入选片段转为可持久化的区间列表，按 (start, end, id) 排序。"""
    return [
        {"id": frag.id, "start": frag.start, "end": frag.end}
        for frag in sorted(selected, key=lambda f: (f.start, f.end, f.id))
    ]


@dataclass(frozen=True)
class PublishResult:
    """一次发布的结果。"""

    seq: int
    chain: str
    prev_chain: str


@dataclass(frozen=True)
class VerifyResult:
    """截至某条凭证的链重算结果。

    ``first_break_seq`` 为重算链值与库中链值首次不一致的编号；
    全部一致时为 ``None``。``valid`` 还要求末条重算链值等于
    客户端留存的链值。
    """

    seq: int
    valid: bool
    first_break_seq: int | None
    recomputed_chain: str


class PublishStore:
    """SQLite 凭证库。"""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        # 单连接多线程复用：所有数据库访问由进程内锁串行化，跨进程则由
        # SQLite 的 BEGIN IMMEDIATE 写锁串行化。
        self._conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # 并发写时等待锁而不是立刻 SQLITE_BUSY。
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def publish(
        self,
        masked_text: str,
        intervals: Sequence[dict[str, object]],
        salt_hex: str,
        text_digest: str,
    ) -> PublishResult:
        """在一个即时写事务内分配编号、计算链值并插入凭证。"""
        with self._lock:
            cur = self._conn.cursor()
            # BEGIN IMMEDIATE 立即申请 RESERVED 锁：并发发布在事务层面
            # 排队，保证“最大编号 → 新编号”与“上一链值 → 新链值”不交错。
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(
                    "SELECT seq, chain FROM publish_credential "
                    "ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    seq, prev_chain = 1, GENESIS_CHAIN
                else:
                    seq, prev_chain = row[0] + 1, row[1]
                chain = compute_chain(
                    prev_chain, seq, masked_text, intervals, salt_hex, text_digest
                )
                cur.execute(
                    "INSERT INTO publish_credential "
                    "(seq, masked_text, intervals, salt_hex, text_digest, "
                    "prev_chain, chain) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        seq,
                        masked_text,
                        _canonical_dumps(list(intervals)),
                        salt_hex,
                        text_digest,
                        prev_chain,
                        chain,
                    ),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return PublishResult(seq=seq, chain=chain, prev_chain=prev_chain)

    def verify(self, credential_id: int, client_chain: str) -> VerifyResult | None:
        """从创世链值出发重算 1..credential_id 的全部凭证。

        目标凭证不存在时返回 ``None``。重算不依赖库中的 ``prev_chain``
        列，而是始终由上一条重算链值推导，因此任意一条凭证内容或链值
        被篡改都会在其编号处首次暴露。
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT seq, masked_text, intervals, salt_hex, text_digest, chain "
                "FROM publish_credential WHERE seq <= ? ORDER BY seq",
                (credential_id,),
            )
            rows = cur.fetchall()
        expected = GENESIS_CHAIN
        first_break: int | None = None
        last_seq: int | None = None
        for seq, masked_text, intervals_json, salt_hex, text_digest, stored_chain in rows:
            expected = compute_chain(
                expected,
                seq,
                masked_text,
                json.loads(intervals_json),
                salt_hex,
                text_digest,
            )
            if first_break is None and expected != stored_chain:
                first_break = seq
            last_seq = seq
        if last_seq != credential_id:
            return None
        return VerifyResult(
            seq=credential_id,
            valid=first_break is None and expected == client_chain,
            first_break_seq=first_break,
            recomputed_chain=expected,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# 进程内单例管理（路径可由环境变量切换，便于测试）
# ---------------------------------------------------------------------------

_store: PublishStore | None = None
_store_lock = threading.Lock()


def get_store() -> PublishStore:
    """返回凭证库单例；环境变量改变后下次访问会按新路径重建。"""
    global _store
    path = default_db_path()
    with _store_lock:
        if _store is None or _store.path != path:
            if _store is not None:
                _store.close()
            _store = PublishStore(path)
        return _store


def reset_store() -> None:
    """关闭并清空单例（主要供测试使用）。"""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
