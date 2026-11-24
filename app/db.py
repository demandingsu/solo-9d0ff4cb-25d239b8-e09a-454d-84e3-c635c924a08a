"""SQLite 持久化层。

交接相关三张表：

* ``evidence``：证据表，每条含证据号和判为同件的两个登记号，
  撤回只是置 ``withdrawn`` 标记，行保留以备追溯；
* ``handover``：交接申请及其 *已保存的结论*（供数/待补/暂停、
  两条链的登记号与证据号）。查询接口直接读这里，不临时重算；
* ``handover_evidence``：交接对当前依据所用证据的索引，
  撤回证据时凭它一次定位需要重核的交接对。

盘点索引三张表：

* ``inventory_batch``：每次成功发布的批次与统计；
* ``inventory_key``：登记号 -> 永久标本键（首次入索引时分配，
  永不改派；``seq`` 是分配顺序，供“最早分配的成员键”比较）；
* ``inventory_member``：当前已发布索引，登记号 -> 当前组键。
  与 ``inventory_key`` 在同一事务内整体替换/追加，一起生效。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .graph import Edge, find_two_chains

STATUS_SUPPLIED = "supplied"  # 供数中：两条链齐备
STATUS_PENDING = "pending"  # 待补：申请时证据不足
STATUS_PAUSED = "paused"  # 暂停：撤回后无法再凑齐两条链

KEY_PREFIX = "SK-"  # 永久标本键形如 SK-00000001

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    reg_a       TEXT NOT NULL,
    reg_b       TEXT NOT NULL,
    withdrawn   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    withdrawn_at TEXT
);

CREATE TABLE IF NOT EXISTS handover (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    reg_a      TEXT NOT NULL,
    reg_b      TEXT NOT NULL,
    status     TEXT NOT NULL,
    chain1_regs     TEXT,
    chain1_evidence TEXT,
    chain2_regs     TEXT,
    chain2_evidence TEXT,
    reason     TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(reg_a, reg_b)
);

CREATE TABLE IF NOT EXISTS handover_evidence (
    handover_id INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (handover_id, evidence_id),
    FOREIGN KEY (handover_id) REFERENCES handover(id)
);

CREATE INDEX IF NOT EXISTS idx_he_evidence
    ON handover_evidence(evidence_id);

CREATE TABLE IF NOT EXISTS inventory_key (
    reg         TEXT PRIMARY KEY,
    specimen_key TEXT NOT NULL UNIQUE,
    seq         INTEGER NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_member (
    reg        TEXT PRIMARY KEY,
    specimen_key TEXT NOT NULL UNIQUE,
    group_key  TEXT NOT NULL,
    FOREIGN KEY (reg) REFERENCES inventory_key(reg),
    FOREIGN KEY (specimen_key) REFERENCES inventory_key(specimen_key)
);

CREATE INDEX IF NOT EXISTS idx_inventory_member_group
    ON inventory_member(group_key);

CREATE TABLE IF NOT EXISTS inventory_batch (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    published_at TEXT NOT NULL,
    handover_count INTEGER NOT NULL,
    supplied_count INTEGER NOT NULL,
    member_count INTEGER NOT NULL,
    group_count  INTEGER NOT NULL,
    new_key_count INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_pair(reg_a: str, reg_b: str) -> tuple[str, str]:
    """交接对规范化：按字典序排列，(A,B) 与 (B,A) 视为同一申请。"""
    return tuple(sorted((reg_a, reg_b)))  # type: ignore[return-value]


class Database:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _connect_write(self) -> sqlite3.Connection:
        """写事务连接：``BEGIN IMMEDIATE`` 让读快照与写锁在同一事务内。

        SQLite 默认延迟事务在第一次写时才升级锁，若期间别的连接已提交，
        升级会失败（SQLITE_BUSY）或读到的快照与写时不符。盘点发布要求
        “取数 -> 核对 -> 写入”期间数据不被改动，故发布事务一开头就取写锁。
        """
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            conn.close()
            raise
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # 证据
    # ------------------------------------------------------------------
    def insert_evidence(
        self, evidence_id: str, reg_a: str, reg_b: str
    ) -> dict:
        if reg_a == reg_b:
            raise ValueError("一条证据的两个登记号不能相同")
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO evidence(evidence_id, reg_a, reg_b, "
                    "created_at) VALUES (?, ?, ?, ?)",
                    (evidence_id, reg_a, reg_b, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateEvidence(evidence_id) from exc
        return self.get_evidence(evidence_id)  # type: ignore[return-value]

    def get_evidence(self, evidence_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        return _evidence_row_to_dict(row) if row else None

    def list_evidence(self, include_withdrawn: bool = False) -> list[dict]:
        sql = "SELECT * FROM evidence"
        if not include_withdrawn:
            sql += " WHERE withdrawn = 0"
        sql += " ORDER BY evidence_id"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_evidence_row_to_dict(r) for r in rows]

    def _active_edges(self, conn: sqlite3.Connection) -> list[Edge]:
        rows = conn.execute(
            "SELECT evidence_id, reg_a, reg_b FROM evidence "
            "WHERE withdrawn = 0"
        ).fetchall()
        return [Edge(r["evidence_id"], r["reg_a"], r["reg_b"]) for r in rows]

    # ------------------------------------------------------------------
    # 交接申请与重核
    # ------------------------------------------------------------------
    def create_handover(self, reg_a: str, reg_b: str) -> dict:
        a, b = canonical_pair(reg_a, reg_b)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM handover WHERE reg_a = ? AND reg_b = ?",
                (a, b),
            ).fetchone()
            if existing:
                raise DuplicateHandover(existing["id"])
            now = _now()
            cur = conn.execute(
                "INSERT INTO handover(reg_a, reg_b, status, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (a, b, STATUS_PENDING, now, now),
            )
            handover_id = cur.lastrowid
            self._recompute(conn, handover_id, a, b)
        return self.get_handover(handover_id)  # type: ignore[return-value]

    def get_handover(self, handover_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handover WHERE id = ?", (handover_id,)
            ).fetchone()
        return _handover_row_to_dict(row) if row else None

    def list_handovers(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM handover ORDER BY id"
            ).fetchall()
        return [_handover_row_to_dict(r) for r in rows]

    def reverify_handover(self, handover_id: int) -> dict | None:
        """手工重核（例如补入新证据后，让待补/暂停的申请重新判定）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handover WHERE id = ?", (handover_id,)
            ).fetchone()
            if row is None:
                return None
            self._recompute(conn, handover_id, row["reg_a"], row["reg_b"])
        return self.get_handover(handover_id)

    def handovers_using_evidence(self, evidence_id: str) -> list[int]:
        """当前依据中引用了某证据的交接对 id（撤回前/后均可查）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT handover_id FROM handover_evidence "
                "WHERE evidence_id = ? ORDER BY handover_id",
                (evidence_id,),
            ).fetchall()
        return [r["handover_id"] for r in rows]

    def withdraw_evidence(self, evidence_id: str) -> dict | None:
        """撤回证据并只重核用到它的交接对；撤回与重核在同一事务内生效。

        任一步骤失败则整体回滚，已保存的旧结论不受影响，查询仍可读。
        返回被撤回的证据；证据不存在或已撤回返回 ``None``。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if row is None or row["withdrawn"]:
                return None

            affected = [
                r["handover_id"]
                for r in conn.execute(
                    "SELECT handover_id FROM handover_evidence "
                    "WHERE evidence_id = ? ORDER BY handover_id",
                    (evidence_id,),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE evidence SET withdrawn = 1, withdrawn_at = ? "
                "WHERE evidence_id = ?",
                (_now(), evidence_id),
            )
            for handover_id in affected:
                hr = conn.execute(
                    "SELECT reg_a, reg_b FROM handover WHERE id = ?",
                    (handover_id,),
                ).fetchone()
                self._recompute(
                    conn, handover_id, hr["reg_a"], hr["reg_b"],
                    on_failure=STATUS_PAUSED,
                )
        return self.get_evidence(evidence_id)

    # ------------------------------------------------------------------
    def _recompute(
        self,
        conn: sqlite3.Connection,
        handover_id: int,
        a: str,
        b: str,
        on_failure: str = STATUS_PENDING,
    ) -> None:
        """基于当前全部有效证据重算一个交接对，并把结论落库。"""
        edges = self._active_edges(conn)
        chains = find_two_chains(a, b, edges)

        if chains is None:
            conn.execute(
                "UPDATE handover SET status = ?, chain1_regs = NULL, "
                "chain1_evidence = NULL, chain2_regs = NULL, "
                "chain2_evidence = NULL, reason = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    on_failure,
                    "无法在当前有效证据中找到两条登记号、证据均不共用"
                    "的同件证据链",
                    _now(),
                    handover_id,
                ),
            )
            conn.execute(
                "DELETE FROM handover_evidence WHERE handover_id = ?",
                (handover_id,),
            )
            return

        (c1_regs, c1_ev), (c2_regs, c2_ev) = (
            _chain_to_json(a, chains[0]),
            _chain_to_json(a, chains[1]),
        )
        conn.execute(
            "UPDATE handover SET status = ?, chain1_regs = ?, "
            "chain1_evidence = ?, chain2_regs = ?, chain2_evidence = ?, "
            "reason = NULL, updated_at = ? WHERE id = ?",
            (
                STATUS_SUPPLIED,
                json.dumps(c1_regs, ensure_ascii=False),
                json.dumps(c1_ev, ensure_ascii=False),
                json.dumps(c2_regs, ensure_ascii=False),
                json.dumps(c2_ev, ensure_ascii=False),
                _now(),
                handover_id,
            ),
        )
        conn.execute(
            "DELETE FROM handover_evidence WHERE handover_id = ?",
            (handover_id,),
        )
        conn.executemany(
            "INSERT INTO handover_evidence(handover_id, evidence_id) "
            "VALUES (?, ?)",
            [(handover_id, ev) for ev in c1_ev + c2_ev],
        )

    # ------------------------------------------------------------------
    # 盘点索引
    # ------------------------------------------------------------------
    def publish_inventory(self) -> dict:
        """从当前已保存的交接数据发布一期盘点索引。

        分三段：

        1. **取数**（只读、不持写锁）：读出所有交接申请的两端登记号、
           正在供数的配对、证据表与已分配的永久键；
        2. **编组**（不持锁）：期间交接照常办理、已发布索引照常查询；
        3. **核对并写入**（单个写事务）：重新取数并与第 1 段比对，登记号
           或供数配对只要有变化就整次取消（沿用上期）；一致则把新键、
           成员与本批记录一起提交，任一步失败整体回滚。

        编组规则：所有交接申请的两端登记号都进入索引；仅 ``supplied``
        的配对把两端连成一组，没连上的号各自成组。首次入索引的登记号
        领永久键，同一批新键按登记号升序分配；每组以最早分配的成员键
        作组键。
        """
        snapshot = self._read_inventory_snapshot()
        _verify_inventory_snapshot(snapshot)
        plan = _build_inventory_plan(snapshot)

        conn = self._connect_write()
        try:
            # 核对：取数并编组期间是否有交接被办理/撤回/换链，改变了
            # 登记号或供数配对；同时核对键表未被并发发布改动。
            current = self._inventory_snapshot(conn)
            _check_inventory_snapshot_unchanged(snapshot, current)
            if current["keys"] != snapshot["keys"]:
                raise InventoryConflict(
                    "键表在发布期间已被改动，本次发布取消，沿用上期索引")
            keys = current["keys"]

            now = _now()
            new_rows = [
                {
                    "reg": reg,
                    "specimen_key": _specimen_key(seq),
                    "seq": seq,
                }
                for reg, seq in plan["new_keys"]
            ]
            conn.executemany(
                "INSERT INTO inventory_key(reg, specimen_key, seq, "
                "created_at) VALUES (?, ?, ?, ?)",
                [(r["reg"], r["specimen_key"], r["seq"], now)
                 for r in new_rows],
            )
            for row in new_rows:
                keys[row["reg"]] = row

            member_rows = []
            for members in plan["groups"]:
                group_key = keys[min(
                    members, key=lambda reg: keys[reg]["seq"])]["specimen_key"]
                for reg in members:
                    member_rows.append(
                        (reg, keys[reg]["specimen_key"], group_key)
                    )

            # 索引整表替换与键表追加在同一事务内：一起生效或整体回滚，
            # 成员表保持上期内容。
            conn.execute("DELETE FROM inventory_member")
            conn.executemany(
                "INSERT INTO inventory_member(reg, specimen_key, group_key) "
                "VALUES (?, ?, ?)",
                sorted(member_rows),
            )
            cursor = conn.execute(
                "INSERT INTO inventory_batch(published_at, handover_count, "
                "supplied_count, member_count, group_count, new_key_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, snapshot["handover_count"], len(snapshot["supplied"]),
                 len(member_rows), len(plan["groups"]), len(new_rows)),
            )
            batch_id = cursor.lastrowid
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

        return self.get_inventory_batch(batch_id)  # type: ignore[arg-type]

    def _read_inventory_snapshot(self) -> dict:
        """在一个读事务里取数，保证“同一份已提交数据”。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            return self._inventory_snapshot(conn)
        finally:
            conn.close()

    def _inventory_snapshot(self, conn: sqlite3.Connection) -> dict:
        """读取发布所需数据：交接申请端点、供数配对、链依据与键表。"""
        rows = conn.execute(
            "SELECT * FROM handover ORDER BY id"
        ).fetchall()
        endpoints: set[str] = set()
        supplied: list[dict] = []
        supplied_pairs: list[tuple[str, str]] = []
        for row in rows:
            endpoints.update((row["reg_a"], row["reg_b"]))
            if row["status"] != STATUS_SUPPLIED:
                continue
            c1_regs = json.loads(row["chain1_regs"])
            c2_regs = json.loads(row["chain2_regs"])
            c1_ev = json.loads(row["chain1_evidence"])
            c2_ev = json.loads(row["chain2_evidence"])
            supplied.append({
                "id": row["id"],
                "reg_a": row["reg_a"],
                "reg_b": row["reg_b"],
                "chain1": (c1_regs, c1_ev),
                "chain2": (c2_regs, c2_ev),
            })
            supplied_pairs.append((row["reg_a"], row["reg_b"]))

        evidence_by_id = {
            r["evidence_id"]: (r["reg_a"], r["reg_b"], bool(r["withdrawn"]))
            for r in conn.execute(
                "SELECT evidence_id, reg_a, reg_b, withdrawn FROM evidence"
            ).fetchall()
        }
        keys = {
            r["reg"]: {"specimen_key": r["specimen_key"], "seq": r["seq"]}
            for r in conn.execute(
                "SELECT reg, specimen_key, seq FROM inventory_key"
            ).fetchall()
        }
        return {
            "handover_count": len(rows),
            "endpoints": frozenset(endpoints),
            "supplied": supplied,
            "supplied_pairs": tuple(sorted(supplied_pairs)),
            "evidence": evidence_by_id,
            "keys": keys,
        }

    def get_inventory_batch(self, batch_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_batch WHERE id = ?", (batch_id,)
            ).fetchone()
        return _batch_row_to_dict(row) if row else None

    def list_inventory(self) -> dict:
        """列出当前已发布索引（按组、组内按键的分配顺序）。"""
        with self._connect() as conn:
            # 同一读事务里取批次与成员，避免读到两期发布拼接的结果
            conn.execute("BEGIN")
            batch = _latest_batch(conn)
            rows = conn.execute(
                "SELECT im.reg, im.specimen_key, im.group_key, ik.seq "
                "FROM inventory_member im "
                "JOIN inventory_key ik ON ik.reg = im.reg "
                "ORDER BY ik.seq, im.reg"
            ).fetchall()
        groups: list[dict] = []
        by_key: dict[str, dict] = {}
        for row in rows:
            group = by_key.get(row["group_key"])
            if group is None:
                group = {"group_key": row["group_key"], "members": []}
                by_key[row["group_key"]] = group
                groups.append(group)
            group["members"].append({
                "reg": row["reg"],
                "specimen_key": row["specimen_key"],
            })
        return {"batch": batch, "groups": groups}

    def lookup_inventory(self, query: str) -> dict | None:
        """按登记号或任一已分配键查询当前归属（只读已发布内容）。

        解析顺序：登记号 -> 永久键 -> 组键。永久键永不改派，因此旧明细
        里的键在合组、拆组后仍能查到该登记号当前的组与全组成员。
        登记号或键从未入过索引时返回 ``None``。
        """
        with self._connect() as conn:
            # 同一读事务里取批次、定位与成员，避免读到两期发布拼接的结果
            conn.execute("BEGIN")
            batch = _latest_batch(conn)
            member = conn.execute(
                "SELECT reg, specimen_key, group_key FROM inventory_member "
                "WHERE reg = ?", (query,),
            ).fetchone()
            if member is None:
                member = conn.execute(
                    "SELECT reg, specimen_key, group_key FROM "
                    "inventory_member WHERE specimen_key = ?", (query,),
                ).fetchone()
            if member is None:
                member = conn.execute(
                    "SELECT im.reg, im.specimen_key, im.group_key "
                    "FROM inventory_member im "
                    "JOIN inventory_key ik ON ik.reg = im.reg "
                    "WHERE im.group_key = ? ORDER BY ik.seq, im.reg LIMIT 1",
                    (query,),
                ).fetchone()
            if member is None:
                return None

            members = conn.execute(
                "SELECT im.reg, im.specimen_key FROM inventory_member im "
                "JOIN inventory_key ik ON ik.reg = im.reg "
                "WHERE im.group_key = ? ORDER BY ik.seq, im.reg",
                (member["group_key"],),
            ).fetchall()
        return {
            "query": query,
            "reg": member["reg"],
            "specimen_key": member["specimen_key"],
            "group_key": member["group_key"],
            "members": [
                {"reg": r["reg"], "specimen_key": r["specimen_key"]}
                for r in members
            ],
            "batch": batch,
        }


def _build_inventory_plan(snapshot: dict) -> dict:
    """在不持锁的编组段算好新键与分组，写入段只做核对与落库。"""
    nodes, adjacency = _pair_graph(snapshot)
    keys = snapshot["keys"]
    new_regs = sorted(reg for reg in nodes if reg not in keys)
    next_seq = max((k["seq"] for k in keys.values()), default=0) + 1
    return {
        "new_keys": [(reg, next_seq + offset)
                     for offset, reg in enumerate(new_regs)],
        "groups": _connected_components(nodes, adjacency),
    }


def _pair_graph(snapshot: dict) -> tuple[set[str], dict[str, list[str]]]:
    """所有交接端点作顶点，正在供数的配对作无向边。"""
    nodes = set(snapshot["endpoints"])
    adjacency: dict[str, list[str]] = {reg: [] for reg in nodes}
    for a, b in snapshot["supplied_pairs"]:
        adjacency[a].append(b)
        adjacency[b].append(a)
    for neighbours in adjacency.values():
        neighbours.sort()
    return nodes, adjacency


def _verify_inventory_snapshot(snapshot: dict) -> None:
    """发布前的数据核对：登记号、供数配对与已保存链结论彼此一致。

    已保存结论理应自洽；写入前再验一遍，任何不一致都拒绝发布，
    以免把错误的编组写进索引。
    """
    evidence = snapshot["evidence"]
    for handover in snapshot["supplied"]:
        if not (handover["chain1"][0] and handover["chain2"][0]):
            raise InventoryConflict(
                f"交接对 id={handover['id']} 缺少链依据")
        for regs, evs in (handover["chain1"], handover["chain2"]):
            if len(regs) != len(evs) + 1:
                raise InventoryConflict(
                    f"交接对 id={handover['id']} 的登记号与证据数不匹配")
            if regs[0] != handover["reg_a"] or regs[-1] != handover["reg_b"]:
                raise InventoryConflict(
                    f"交接对 id={handover['id']} 的链端点与申请登记号不符")
        used: set[str] = set()
        for regs, evs in (handover["chain1"], handover["chain2"]):
            for left, right, ev in zip(regs, regs[1:], evs):
                if ev in used:
                    raise InventoryConflict(
                        f"交接对 id={handover['id']} 两条链共用证据 {ev}")
                used.add(ev)
                actual = evidence.get(ev)
                if actual is None or actual[2]:
                    raise InventoryConflict(
                        f"交接对 id={handover['id']} 依据的证据 {ev} "
                        "不存在或已撤回")
                if {actual[0], actual[1]} != {left, right}:
                    raise InventoryConflict(
                        f"交接对 id={handover['id']} 依据的证据 {ev} "
                        "端点与链不符")


def _check_inventory_snapshot_unchanged(before: dict, after: dict) -> None:
    """核对登记号与供数配对仍与取数时一致。"""
    before_fp = (before["endpoints"], before["supplied_pairs"])
    after_fp = (after["endpoints"], after["supplied_pairs"])
    if before_fp != after_fp:
        raise InventoryConflict(
            "取数后登记号或供数配对已变化，本次发布取消，沿用上期索引")


def _connected_components(nodes: set[str],
                          adjacency: dict[str, list[str]]) -> list[list[str]]:
    """按供数配对把登记号连成组；未连上的号各自成组。"""
    seen: set[str] = set()
    groups: list[list[str]] = []
    for start in sorted(nodes):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        members: list[str] = []
        while stack:
            reg = stack.pop()
            members.append(reg)
            for neighbour in adjacency.get(reg, []):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        groups.append(sorted(members))
    return groups


def _specimen_key(seq: int) -> str:
    return f"{KEY_PREFIX}{seq:08d}"


def _latest_batch(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM inventory_batch ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _batch_row_to_dict(row) if row else None


def _batch_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "batch_id": row["id"],
        "published_at": row["published_at"],
        "handover_count": row["handover_count"],
        "supplied_count": row["supplied_count"],
        "member_count": row["member_count"],
        "group_count": row["group_count"],
        "new_key_count": row["new_key_count"],
    }


def _chain_to_json(
    start: str, chain: list[tuple[str, str]]
) -> tuple[list[str], list[str]]:
    regs = [start]
    evidence: list[str] = []
    for reg, ev in chain:
        regs.append(reg)
        evidence.append(ev)
    return regs, evidence


def _evidence_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "evidence_id": row["evidence_id"],
        "reg_a": row["reg_a"],
        "reg_b": row["reg_b"],
        "withdrawn": bool(row["withdrawn"]),
        "created_at": row["created_at"],
        "withdrawn_at": row["withdrawn_at"],
    }


def _handover_row_to_dict(row: sqlite3.Row) -> dict:
    def loads(value: str | None) -> list[str] | None:
        return json.loads(value) if value is not None else None

    return {
        "id": row["id"],
        "reg_a": row["reg_a"],
        "reg_b": row["reg_b"],
        "status": row["status"],
        "chain1": {
            "registrations": loads(row["chain1_regs"]),
            "evidence": loads(row["chain1_evidence"]),
        },
        "chain2": {
            "registrations": loads(row["chain2_regs"]),
            "evidence": loads(row["chain2_evidence"]),
        },
        "reason": row["reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class DuplicateEvidence(Exception):
    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        super().__init__(f"证据号已存在：{evidence_id}")


class DuplicateHandover(Exception):
    def __init__(self, handover_id: int) -> None:
        self.handover_id = handover_id
        super().__init__(f"该登记号对的交接申请已存在：id={handover_id}")


class InventoryConflict(Exception):
    """发布核对未通过：取数后登记号或供数配对已变化。"""
