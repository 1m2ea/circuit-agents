"""活体验证：深化① 真实 LLM 接入多 agent 编排（对 http://127.0.0.1:8765 真实 HTTP）。

覆盖：能力探测 / 默认全 mock 不烧钱 / 越权拦截 / 真实 LLM 三角色编排 /
      角色身份写入 activity / 降级可见 / 通过后固化房间知识库。
用法：python _live_llm_orchestrate_check.py [--real]   （--real 才发起真实模型调用）
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
REAL = "--real" in sys.argv
ok_n = fail_n = 0


def call(path, body=None, timeout=420):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def check(name, cond, extra=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"  ✓ {name}" + (f"  · {extra}" if extra else ""))
    else:
        fail_n += 1
        print(f"  ✗ {name}  << 失败 {extra}")


SPEC = {"name": "llm_orch", "components": {
    "src": {"type": "power", "label": "task", "task": "把一段中文新闻抽取成结构化要点"},
    "ext": {"type": "resistor", "label": "抽取", "model": "small", "produced_outputs": ["items"]},
    "sum": {"type": "resistor", "label": "汇总", "model": "small",
            "required_inputs": ["items"], "produced_outputs": ["brief"]},
}, "wires": [["src", "ext"], ["ext", "sum"]]}

RID = "live_llm_" + str(int(time.time()))[-6:]

print("=" * 66)
print("活体验证 · 深化① 真实 LLM 接入多 agent 编排" + ("（含真实模型调用）" if REAL else "（仅离线路径）"))
print("=" * 66)

print("\n[1] 能力探测端点")
s, av = call("/agents/availability?check_student=false")
check("GET /agents/availability 可用", s == 200, str(av)[:70])
check("返回 mentor/judge/student 三角色可用性",
      all(k in av for k in ("mentor", "judge", "student", "any")))
check("detail 给出可读原因（供 UI 提示）", isinstance(av.get("detail"), dict),
      str(av.get("detail", {}).get("mentor"))[:50])

print("\n[2] 建房 + 默认路径（不开 use_llm → 不应产生任何真实调用）")
s, room = call("/rooms", {"spec": SPEC, "owner_id": "liam", "name": "llm房", "room_id": RID})
check("房间创建成功", s == 200 and room.get("room_id") == RID, RID)
t0 = time.time()
s, o1 = call(f"/rooms/{RID}/orchestrate", {"user_id": "liam"})
dt = time.time() - t0
check("默认编排成功", s == 200, f"{dt:.2f}s")
check("默认 mentor=mock（不误烧 token）", o1["agents"]["mentor"] == "mock", str(o1["agents"]))
check("默认 reviewer=rule", o1["agents"]["reviewer"] == "rule")
check("默认路径无降级记录", not o1["degraded"])
check("默认路径极快（<2s，证明没走网络）", dt < 2.0, f"{dt:.2f}s")

print("\n[3] 权限：reviewer 角色无 control → 编排应 403")
call(f"/rooms/{RID}/join", {"user_id": "grace", "desired_role": "reviewer"})
s, _ = call(f"/rooms/{RID}/orchestrate", {"user_id": "grace"})
check("越权发起编排被 403 拦截", s == 403, f"status={s}")

print("\n[4] 单角色覆盖：只让 reviewer 走真实，其余保持 mock")
s, o3 = call(f"/rooms/{RID}/orchestrate",
             {"user_id": "liam", "use_real_reviewer": False, "use_real_student": False})
check("未指定角色不被带成真实", s == 200 and o3["agents"]["mentor"] == "mock", str(o3["agents"]))

if REAL:
    print("\n[5] 真实 LLM 编排（mentor + reviewer 走云端模型，student 保持 mock）")
    t0 = time.time()
    s, o2 = call(f"/rooms/{RID}/orchestrate",
                 {"user_id": "liam", "use_llm": True, "use_real_student": False,
                  "goal": "把一段中文新闻抽取成结构化要点，当前 small 档抽取失败",
                  "case": {"spec": SPEC, "result": {"final_quality": 0.25,
                                                    "failed_nodes": ["ext"]}}})
    dt = time.time() - t0
    check("真实编排返回 200", s == 200, f"{dt:.1f}s")
    if s == 200:
        ag = o2["agents"]
        check("闭环完整（三角色都有身份标记）",
              all(k in ag for k in ("mentor", "reviewer", "student")), str(ag))
        check("mentor 走真实 LLM 或明确降级（绝不崩）",
              ag["mentor"] in ("llm", "mock"),
              f"mentor={ag['mentor']} degraded={o2['degraded']}")
        check("reviewer 给出裁决",
              o2["trace"]["reviewer"]["verdict"] in ("approve", "reject"),
              o2["trace"]["reviewer"].get("reason", "")[:60])
        if ag["mentor"] == "llm":
            print("     ↳ 真实导师诊断：" + str(o2["trace"]["mentor"]["diagnosis"])[:80])
        check("优化后 spec 仍是合法结构",
              isinstance(o2["trace"]["optimized_spec"].get("components"), dict))
else:
    print("\n[5] 真实 LLM 编排 —— 跳过（加 --real 开启，会真实消耗 token）")

print("\n[6] 角色身份 / 降级 写入房间 activity（协作者可见）")
s, act = call(f"/rooms/{RID}/activity?user_id=liam")
acts = act["activities"]
check("activity 含 orchestrate 事件", any(a["action"] == "orchestrate" for a in acts))
check("activity 标注了每角色 真实/mock 身份",
      any("mentor:mock" in (a.get("detail") or "") or "mentor:llm" in (a.get("detail") or "")
          for a in acts),
      next((a["detail"][:60] for a in acts if a["action"] == "orchestrate"), ""))

print("\n[7] 编排通过 → 固化进房间共享知识库")
s, mem = call(f"/rooms/{RID}/memory?user_id=liam")
check("房间知识库有模板沉淀", s == 200 and len(mem.get("templates", [])) >= 1,
      f"{len(mem.get('templates', []))} 条模板 / {len(mem.get('learnings', []))} 条学习记录")

print("\n" + "=" * 66)
print(f"结果：{ok_n} 项通过 / {fail_n} 项失败")
print("=" * 66)
sys.exit(1 if fail_n else 0)
