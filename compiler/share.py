"""circuit-agents · 电路图共享生态（⑬）

把电路拓扑导出为可分享的 JSON（含元数据/校验），并可在本地共享仓库(ShareRepo)中
发布/检索/拉取，形成『电路图共享生态』雏形。

设计（第三层范式升级 · ⑬）：
  · export_topology：spec → 携带 schema_version/作者/标签/校验和的分享包（可进版本库/社区分发）。
  · import_topology：校验后还原 spec（缺 spec/版本不兼容则拒绝）。
  · ShareRepo：本地 JSON 仓库，发布/列表/拉取/删除，带模块级锁（并发安全，复用 ⑥ 经验）。
  · 范围：⑬ 聚焦『拓扑的可分享/可复用』，不含远程 registry（留给平台层）。
"""
from __future__ import annotations

import json
import os
import threading
import hashlib
import time
from typing import Optional

_SCHEMA_VERSION = "circuit-topology/1.0"
_REPO_LOCK = threading.Lock()


def _checksum(spec):
    raw = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def export_topology(spec, author: str = "anonymous",
                    tags: Optional[list] = None, name: Optional[str] = None) -> dict:
    """把一份电路拓扑导出为可分享包（JSON 可序列化）。"""
    name = name or spec.get("name") or "unnamed"
    return {
        "schema_version": _SCHEMA_VERSION,
        "name": name,
        "author": author,
        "tags": list(tags or []),
        "created_at": int(time.time() * 1000),
        "checksum": _checksum(spec),
        "spec": spec,
    }


def import_topology(obj) -> dict:
    """校验并还原 spec；非法/版本不兼容则拒绝。"""
    if not isinstance(obj, dict) or "spec" not in obj:
        raise ValueError("非法拓扑包：缺少 spec 字段")
    if obj.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"拓扑包版本不兼容：{obj.get('schema_version')} != {_SCHEMA_VERSION}")
    return obj["spec"]


class ShareRepo:
    """本地电路图共享仓库（JSON 文件存储，模块级锁保并发安全）。"""

    def __init__(self, path: str = ".topology_repo.json"):
        self.path = path
        self._store = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._store, f, ensure_ascii=False, indent=2)

    def publish(self, spec, author: str = "anonymous",
                tags: Optional[list] = None, name: Optional[str] = None) -> str:
        pkg = export_topology(spec, author=author, tags=tags, name=name)
        with _REPO_LOCK:
            self._store = self._load()
            self._store[pkg["name"]] = pkg
            self._save()
        return pkg["name"]

    def list(self):
        with _REPO_LOCK:
            self._store = self._load()
            return [{"name": k, "author": v.get("author"), "tags": v.get("tags"),
                     "checksum": v.get("checksum")} for k, v in self._store.items()]

    def fetch(self, name: str):
        with _REPO_LOCK:
            self._store = self._load()
            return self._store.get(name)

    def pull(self, name: str):
        pkg = self.fetch(name)
        if pkg is None:
            raise KeyError(f"仓库中无此拓扑：{name}")
        return import_topology(pkg)

    def remove(self, name: str) -> bool:
        with _REPO_LOCK:
            self._store = self._load()
            if name in self._store:
                del self._store[name]
                self._save()
                return True
            return False


def topology_share_selftest():
    """⑬ 电路图共享生态离线自检：导出导入往返 + 仓库发布/拉取/删除。"""
    spec = {"name": "demo", "components": {
        "src": {"type": "power", "label": "src"},
        "A": {"type": "resistor", "label": "A", "model": "small",
              "produced_outputs": ["x"]},
    }, "wires": [["src", "A"]]}

    # 1) export → import 往返等价
    pkg = export_topology(spec, author="alice", tags=["demo"])
    assert pkg["schema_version"] == _SCHEMA_VERSION
    back = import_topology(pkg)
    assert back == spec, "导出→导入应还原等价 spec"
    print("✓ ⑬ export→import 往返等价（校验和一致）")

    # 2) 非法/版本不兼容拒绝
    try:
        import_topology({"foo": "bar"})
        raise AssertionError("缺 spec 应拒绝")
    except ValueError:
        pass
    try:
        import_topology({"spec": {}, "schema_version": "bad/9"})
        raise AssertionError("版本不兼容应拒绝")
    except ValueError:
        pass
    print("✓ ⑬ 非法包/版本不兼容包被拒绝（防错误拓扑注入）")

    # 3) ShareRepo 发布/列表/拉取/删除
    import tempfile
    tmp = tempfile.mktemp(suffix=".json")
    try:
        repo = ShareRepo(tmp)
        repo.publish(spec, author="bob", tags=["x"])
        repo.publish({"name": "t2",
                      "components": {"s": {"type": "power", "label": "s"}},
                      "wires": []}, author="bob")
        names = [e["name"] for e in repo.list()]
        assert "demo" in names and "t2" in names, f"仓库应含 demo/t2，实际 {names}"
        pulled = repo.pull("demo")
        assert pulled == spec, "pull 应还原原 spec"
        assert repo.remove("t2") and not repo.remove("t2"), "删除应幂等（二次删失败）"
        print("✓ ⑬ ShareRepo 发布/列表/拉取/删除 往返成功（本地共享仓库）")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    print("\n⑬ 电路图共享生态 离线自检全部通过 ✓")


if __name__ == "__main__":
    topology_share_selftest()
