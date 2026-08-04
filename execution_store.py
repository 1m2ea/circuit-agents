"""
circuit-agents · execution_store — SQLite 持久化执行历史 + 回放

用法:
  from execution_store import ExecutionStore

  store = ExecutionStore("my_executions.db")
  store.save(run_id, spec, events, result)
  record = store.load(run_id)
  recent = store.list_recent(limit=10)
  store.replay(run_id)  # 加载 spec 重新执行（不保证确定性）
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Optional


class ExecutionStore:
    """SQLite 持久化：保存/加载/列显/回放 circuit-agents 执行记录。"""

    def __init__(self, db_path: str = "executions.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS executions (
                    run_id       TEXT PRIMARY KEY,
                    goal         TEXT NOT NULL DEFAULT '',
                    status       TEXT NOT NULL DEFAULT 'unknown',
                    spec         TEXT NOT NULL DEFAULT '{}',
                    events       TEXT NOT NULL DEFAULT '[]',
                    result       TEXT NOT NULL DEFAULT '{}',
                    created_at   TEXT NOT NULL,
                    finished_at  TEXT,
                    tags         TEXT NOT NULL DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_created
                ON executions(created_at DESC)
            """)
            conn.commit()

    # ── CRUD ────────────────────────────────────────────────

    def save(self, run_id: str, goal: str, status: str,
             spec: dict, events: list, result: dict,
             tags: Optional[list] = None) -> str:
        """保存一次执行记录。返回 run_id。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO executions
                   (run_id, goal, status, spec, events, result, created_at, finished_at, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, goal, status,
                 json.dumps(spec, ensure_ascii=False),
                 json.dumps(events, ensure_ascii=False, default=str),
                 json.dumps(result, ensure_ascii=False, default=str),
                 now, now,
                 json.dumps(tags or [], ensure_ascii=False)),
            )
            conn.commit()
        return run_id

    def load(self, run_id: str) -> Optional[dict]:
        """加载一条执行记录。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_dict(row)

    def update_status(self, run_id: str, status: str, result: Optional[dict] = None):
        """更新执行状态和结果。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with self._connect() as conn:
            params = [status, now, run_id]
            if result is not None:
                conn.execute(
                    """UPDATE executions
                       SET status=?, finished_at=?, result=?
                       WHERE run_id=?""",
                    (status, now, json.dumps(result, ensure_ascii=False, default=str),
                     run_id),
                )
            else:
                conn.execute(
                    "UPDATE executions SET status=?, finished_at=? WHERE run_id=?",
                    params,
                )
            conn.commit()

    def list_recent(self, limit: int = 20) -> list[dict]:
        """列出最近的执行记录（摘要，不含 spec/events/result 全文）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT run_id, goal, status, created_at, finished_at, tags
                   FROM executions ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                {
                    "run_id": r["run_id"],
                    "goal": r["goal"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "finished_at": r["finished_at"],
                    "tags": json.loads(r["tags"]),
                }
                for r in rows
            ]

    def delete(self, run_id: str) -> bool:
        """删除一条记录。返回是否实际删除了行。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM executions WHERE run_id = ?", (run_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    # ── 回放 ────────────────────────────────────────────────

    def replay(self, run_id: str) -> Optional[dict]:
        """加载历史 spec 并用 SimBackend 重新执行（非确定性回放，用于验证管线完整性）。"""
        record = self.load(run_id)
        if record is None:
            return None

        spec = json.loads(record["spec"]) if isinstance(record["spec"], str) else record["spec"]

        import random
        from runtime import Circuit, CircuitExecutor, SimBackend

        backend = SimBackend(random.Random(int(time.time() * 1000) % (2**31)))
        circuit = Circuit(spec, backend)

        events = []
        executor = CircuitExecutor(
            circuit,
            on_node_done=lambda c, s, i: events.append(i),
            events=events,
            memory_enabled=False,
        )
        result = executor.run()
        return {"spec": spec, "events": events, "result": result}

    # ── 辅助 ────────────────────────────────────────────────

    def _row_to_dict(self, row) -> dict:
        return {
            "run_id": row["run_id"],
            "goal": row["goal"],
            "status": row["status"],
            "spec": json.loads(row["spec"]) if row["spec"] else {},
            "events": json.loads(row["events"]) if row["events"] else [],
            "result": json.loads(row["result"]) if row["result"] else {},
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
        }

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]


# ──────────────────────────────────────────────────────────
# selftest
# ──────────────────────────────────────────────────────────

def selftest():
    """离线自检（无需 key/网络）。"""
    import os
    import tempfile
    import random

    print("=== execution_store 离线自检 ===")

    # 用临时文件
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="exec_store_")
    os.close(fd)
    try:
        store = ExecutionStore(tmp)

        # S1: save + load
        rid = "test-run-001"
        spec = {"name": "test", "components": {"src": {"type": "power", "label": "src"}}}
        events = [{"type": "start", "t": 0.0}]
        result = {"final_quality": 0.85, "success": True}
        store.save(rid, "测试任务", "done", spec, events, result, tags=["test", "smoke"])

        rec = store.load(rid)
        assert rec is not None, "save 后 load 应用非空"
        assert rec["goal"] == "测试任务"
        assert rec["status"] == "done"
        assert rec["result"]["final_quality"] == 0.85
        assert "test" in rec["tags"]
        print(f"✓ S1 save+load: goal={rec['goal']}, quality={rec['result']['final_quality']}")

        # S2: list_recent
        store.save("test-run-002", "任务B", "running", spec, events,
                   {"final_quality": 0.0}, tags=[])
        recent = store.list_recent(5)
        assert len(recent) >= 2, f"应有 ≥2 条记录，实际 {len(recent)}"
        # 倒序，最新先出（同秒 timestamp 顺序不保证）
        ids = {r["run_id"] for r in recent}
        assert "test-run-001" in ids and "test-run-002" in ids
        print(f"✓ S2 list_recent: {len(recent)} 条记录")

        # S3: count
        assert store.count() >= 2
        print(f"✓ S3 count: {store.count()} 条")

        # S4: update_status
        store.update_status("test-run-002", "done",
                            {"final_quality": 0.70, "success": True})
        rec2 = store.load("test-run-002")
        assert rec2["status"] == "done"
        assert rec2["result"]["final_quality"] == 0.70
        print(f"✓ S4 update_status: {rec2['status']}, quality={rec2['result']['final_quality']}")

        # S5: delete
        store.save("temp-run", "临时", "done", spec, events, result)
        assert store.load("temp-run") is not None
        store.delete("temp-run")
        assert store.load("temp-run") is None
        print("✓ S5 delete: 删除后 load 返回 None")

        # S6: replay（端到端：持久化 spec → 重新 CircuitExecutor.run）
        rid6 = "replay-test"
        spec6 = {
            "name": "replay",
            "components": {
                "src": {"type": "power", "label": "src"},
                "A": {"type": "resistor", "label": "A", "model": "small",
                      "produced_outputs": ["x"]},
                "B": {"type": "resistor", "label": "B", "model": "small",
                      "required_inputs": ["x"]},
            },
            "wires": [["src", "A"], ["A", "B"]],
        }
        store.save(rid6, "回放测试", "done", spec6, events, result)
        replay_result = store.replay(rid6)
        assert replay_result is not None
        assert replay_result["result"]["final_quality"] > 0
        assert len(replay_result["events"]) >= 3
        print(f"✓ S6 replay: 重新执行成功, quality={replay_result['result']['final_quality']:.3f}, "
              f"events={len(replay_result['events'])}")

        # S7: load 不存在的
        assert store.load("nonexistent") is None
        print("✓ S7 缺失记录: load 非存在 id 返回 None")

        print(f"\nexecution_store 离线自检全部通过 ✓（db={tmp}）")

    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


if __name__ == "__main__":
    selftest()
