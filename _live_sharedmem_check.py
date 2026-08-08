"""大规模协作 维度② 共享记忆与知识库 —— 端到端 HTTP 校验（双用户）。

前置：server.py 已在 8765 运行（含 #184 端点）。
用法：python _live_sharedmem_check.py
"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8765"


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _ok(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        raise SystemExit(f"FAILED: {msg}")


def main():
    spec = {
        "name": "kb_demo",
        "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "r1": {"type": "resistor", "label": "a", "model": "small"},
            "adc": {"type": "adc", "label": "adc", "threshold": 0.5},
        },
        "wires": [["src", "r1"], ["r1", "adc"]],
    }
    rid = "sharedmem_demo"

    # 1) erin 建房间
    st, cr = _req("POST", "/rooms", {"spec": spec, "owner_id": "erin", "name": "kb", "room_id": rid})
    _ok(st == 200 and cr["room_id"] == rid, "双用户① erin 建房间 sharedmem_demo")

    # 2) erin(owner) 发布当前拓扑到共享仓库
    st, pub = _req("POST", f"/rooms/{rid}/memory/publish", {"user_id": "erin"})
    _ok(st == 200 and pub.get("published_name"), "双用户② erin 发布拓扑到共享仓库")
    pname = pub["published_name"]

    # 3) erin 看知识库：published 含该条目 + 带 room 标签
    st, mem = _req("GET", f"/rooms/{rid}/memory?user_id=erin")
    _ok(st == 200 and pname in mem["published"], "双用户③ 知识库记录已发布项")
    _ok(any(f"room:{rid}" in (it.get("tags") or []) for it in mem["repo_items"]),
        "双用户③b 仓库条目带 room 标签")

    # 4) frank 加入 observer
    st, join = _req("POST", f"/rooms/{rid}/join", {"user_id": "frank", "desired_role": "observer"})
    _ok(st == 200 and join["role"] == "observer", "双用户④ frank 加入 observer")

    # 5) frank(observer) 可见共享记忆（多人共享知识库）
    st, mem2 = _req("GET", f"/rooms/{rid}/memory?user_id=frank")
    _ok(st == 200 and mem2["repo_items"], "双用户⑤ observer 能看到房间共享记忆")

    # 6) frank 尝试发布 → 403（无 publish 权限）
    st, _ = _req("POST", f"/rooms/{rid}/memory/publish", {"user_id": "frank"})
    _ok(st == 403, "双用户⑥ observer 发布被 403 拦截")

    # 7) frank(observer, read) 拉取已发布拓扑到房间（共享记忆复用）
    st, pull = _req("POST", f"/rooms/{rid}/memory/pull", {"user_id": "frank", "name": pname})
    _ok(st == 200 and pull.get("session_id"), "双用户⑦ observer 能 pull 到房间复用")

    # 8) erin 蒸馏已发布历史为模板（⑭）
    st, dist = _req("POST", f"/rooms/{rid}/memory/distill", {"user_id": "erin"})
    _ok(st == 200 and "templates" in dist, "双用户⑧ erin 蒸馏模板成功")

    # 9) 共享仓库全局可见（⑬ 生态）
    st, repo = _req("GET", "/topology/repo")
    _ok(st == 200 and any(i["name"] == pname for i in repo["items"]),
        "双用户⑨ 共享仓库全局可见该拓扑")

    print("\n全部通过 ✓ 共享记忆与知识库（维度②）端到端 OK")


if __name__ == "__main__":
    main()
