"""探针：抓 WITH 条件下真 LLM 的原始输出，定位教训注入为何反而降分。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import benchmark_surpass as B
from compiler.topology_memory import TopologyMemory

tmpd = tempfile.mkdtemp()
mp = os.path.join(tmpd, "m.json")
_orig = TopologyMemory.__init__
TopologyMemory.__init__ = lambda self, path=None: _orig(self, path if path is not None else mp)

m0 = TopologyMemory(path=mp)
for t in B.TRANSLATION_TASKS:
    rule = (f"翻译约定：句子『{t['src']}』的关键术语必须译为『{t['gold_term']}』"
            + (f"，不得译为『{t['decoy']}』" if t.get("decoy") else "")
            + "（用户既定偏好，必须遵守）")
    m0.record_lesson(rule, tags=["human", "term"])

for idx in (0, 4):   # t1 resistor / t5 transistor
    task = B.TRANSLATION_TASKS[idx]
    cap = {}
    res = B.run_task(task, B.make_backend("real"), True, mp, cap)
    print(f"\n===== {task['id']} gold={task['gold_term']} decoy={task.get('decoy')} =====")
    print("success:", res["success"], "final_quality:", res["final_quality"])
    for cid, sig in cap.items():
        comp = None
        print(f"[{cid}] ok={sig.ok} q={sig.quality}")
        print("value:", repr(sig.value))
