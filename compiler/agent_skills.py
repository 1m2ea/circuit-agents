"""
circuit-agents · compiler.agent_skills
======================================
"每个 agent 都可以调用技能" 的**技能注册表 + 执行层**。

设计要点（与用户对齐确认）：
 · 这是 Phase C「每个电阻 = 独立 LLM 实例」的自然延伸：给每个能力节点配一个
   "技能包"（skills），让子 agent 从"被动处理上下文"升级为"主动选工具完成任务"。
 · 技能本身是**可执行的 Python 函数**（也可将来扩展成子流程 / 另一个 LLM 链）。
   每个技能声明 OpenAI 风格的 function schema（name / description / parameters），
   供 LLMAgentBackend 组装进 chat/completions 的 `tools` 参数做真·工具调用。
 · 执行层只做"按名查表 + 解析参数 + 调 handler + 容错返回字符串"——它**不关心**
   是哪个节点调的、也不关心语义对不对，纯粹的运行时刻执行器。可用性/成败由运行时刻决定，
   封装层（LLMAgentBackend）只负责把技能声明注入提示词 + 装配 tools，符合既有诚实边界。
 · 内核零改动：runtime.Circuit.propagate / 分层延迟 / 开路语义完全不涉及本模块。

安全边界（重要，诚实声明）：
 · run_code 会执行**模型生成的 Python 代码**（subprocess 拉起本机 python）。这是用户显式
   要的"让 agent 能跑代码验证数值"能力，目前**未做沙箱隔离**（仅把 cwd 设为临时目录、
   超时 10s 兜底）。生产环境应进一步限制（seccomp / 禁网络 / 禁危险 import / 资源配额）。
   本模块只提供能力，是否在生产启用由调用方决定。
 · web_search / read_page 是**联网技能**：默认走无 key 的 DuckDuckGo HTML 抓取（真实出网），
   若设置环境变量 `SEARCH_PROVIDER=tavily` + `SEARCH_API_KEY=<key>` 则自动切换为 Tavily
   正式搜索 API（更稳定、合规）。联网失败或限流时优雅降级为「未检索到 / 调用失败」文本，
   不会让一次检索失败炸掉整条链路。query_db 是**本地文档检索**，零外部依赖。
   · 第三层技能包（extract_fields / apply_glossary / classify_taxonomy / unit_convert /
     spreadsheet_calc / apply_template / apply_style_guide）均为**纯 stdlib、零外部依赖**，
     环境健壮，沙箱无网络也能真用；extract_pdf / extract_ocr 为可选库接缝
     （pdfplumber/PyPDF2、pytesseract+Pillow），未安装时返回明确提示、不崩溃，
     生产 `pip install` 后即真可用——与 web_search 的"无 key 真实源 + Tavily 插槽"同哲学。
 · 技能失败统一以「技能 [名称] 调用失败：原因」文本返回，便于下游节点 / 反馈环自行纠正
   （延续内核"开路但不崩"的精神）。
"""
from __future__ import annotations

import ast
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# 技能注册表：name -> {description, parameters, handler}
# ---------------------------------------------------------------------------
def _run_code(code: str) -> str:
    """执行一段 Python 代码，返回 stdout/stderr/returncode 的合并文本。

    安全兜底：cwd 设为一次性临时目录（模型生成的写文件操作不会污染项目目录），
    超时 10s 强杀。仍非完整沙箱——详见模块 docstring 的安全边界。
    """
    if not isinstance(code, str) or not code.strip():
        return "[run_code: 空代码，未执行]"
    try:
        workdir = tempfile.mkdtemp(prefix="agent_skill_")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            cwd=workdir, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "[run_code: 执行超时(>10s)，已中止]"
    except Exception as e:  # 启动失败等
        return f"[run_code: 启动失败 {type(e).__name__}: {e}]"
    parts = []
    if proc.stdout:
        parts.append("stdout:\n" + proc.stdout.rstrip())
    if proc.stderr:
        parts.append("stderr:\n" + proc.stderr.rstrip())
    parts.append(f"returncode={proc.returncode}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 联网 / 本地检索类技能（retrieve 技能包）：web_search / read_page / query_db
# ---------------------------------------------------------------------------
def _web_search_ddg(query: str, max_results: int = 5) -> str:
    """DuckDuckGo HTML 无 key 真实检索（联网）。失败优雅降级。"""
    try:
        q = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = resp.read().decode("utf-8", "ignore")
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', data, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', data, re.S)
        links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', data, re.S)
        items = []
        for i in range(min(max_results, len(titles))):
            title = re.sub(r"<[^>]+>", "", titles[i]).strip()
            snip = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            link = links[i] if i < len(links) else ""
            items.append(f"{i+1}. {title}\n   {snip}\n   {link}")
        if not items:
            return "未检索到：DuckDuckGo 未返回结果（可能被限流或网络受限）。"
        return "搜索结果：\n" + "\n\n".join(items)
    except Exception as e:
        return f"技能 [web_search] 调用失败：{type(e).__name__}: {e}"


def _web_search_tavily(query: str, api_key: str, max_results: int = 5) -> str:
    """Tavily 搜索 API（需 SEARCH_API_KEY + SEARCH_PROVIDER=tavily）。"""
    try:
        body = json.dumps({"api_key": api_key, "query": query,
                           "max_results": max_results, "search_depth": "basic"}).encode()
        req = urllib.request.Request("https://api.tavily.com/search",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        results = data.get("results", [])[:max_results]
        if not results:
            return "未检索到：Tavily 未返回结果。"
        items = []
        for i, r in enumerate(results, 1):
            items.append(f"{i}. {r.get('title','')}\n   {r.get('content','')}\n   {r.get('url','')}")
        return "搜索结果(Tavily)：\n" + "\n\n".join(items)
    except Exception as e:
        return f"技能 [web_search] 调用失败：{type(e).__name__}: {e}"


def _web_search(query: str, max_results: int = 5) -> str:
    """联网搜索：优先用配置好的搜索 API（SEARCH_PROVIDER=tavily + SEARCH_API_KEY），
    否则降级到无 key 的 DuckDuckGo HTML 抓取。"""
    provider = (os.environ.get("SEARCH_PROVIDER") or "").lower()
    key = os.environ.get("SEARCH_API_KEY", "")
    if provider == "tavily" and key:
        return _web_search_tavily(query, key, max_results)
    return _web_search_ddg(query, max_results)


def _read_page(url: str) -> str:
    """抓取指定 URL 的全文（联网），做简易去标签。失败优雅降级。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read()
            ctype = resp.headers.get_content_type()
        if ctype and "html" in ctype:
            text = raw.decode("utf-8", "ignore")
            text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
        else:
            text = raw.decode("utf-8", "ignore")
        if len(text) > 4000:
            text = text[:4000] + "\n...[已截断，仅返回前 4000 字符]"
        return f"页面 {url} 全文（前 4000 字符）：\n{text}"
    except Exception as e:
        return f"技能 [read_page] 调用失败：{type(e).__name__}: {e}"


def _query_db(query: str) -> str:
    """本地文档检索：在工作区 docs/ 与 circuit-agents 源码中 grep 命中行（零外部依赖）。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        ca = os.path.dirname(here)  # compiler/ -> circuit-agents/
        roots = [os.path.join(ca, "docs"), ca]
        hits = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dp, _, fns in os.walk(root):
                if any(s in dp for s in (".git", "node_modules", "__pycache__", ".workbuddy")):
                    continue
                for fn in fns:
                    if not fn.lower().endswith((".md", ".txt", ".py", ".json")):
                        continue
                    fp = os.path.join(dp, fn)
                    try:
                        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                            for lineno, line in enumerate(f, 1):
                                if query.lower() in line.lower():
                                    hits.append(f"{fp}:{lineno}: {line.strip()[:200]}")
                                    if len(hits) >= 15:
                                        break
                    except Exception:
                        pass
                    if len(hits) >= 15:
                        break
                if len(hits) >= 15:
                    break
            if len(hits) >= 15:
                break
        if not hits:
            return f"未检索到：本地文档中无匹配「{query}」的内容。"
        return "本地文档检索命中：\n" + "\n".join(hits)
    except Exception as e:
        return f"技能 [query_db] 调用失败：{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 精确计算 / 核对类技能（reason 的 calculator；verify 的 cross_check / diff_text）
# ---------------------------------------------------------------------------
_ALLOWED_AST = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
                ast.USub, ast.UAdd, ast.FloorDiv)


def _safe_eval(expr: str):
    """安全求值算术表达式：仅允许数字与 + - * / ** % 括号，拒绝名称/调用/属性。"""
    try:
        node = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return f"技能 [calculator] 调用失败：表达式语法错误 {e}"
    for n in ast.walk(node):
        if not isinstance(n, _ALLOWED_AST):
            return (f"技能 [calculator] 调用失败：含不允许的符号"
                    f"（{type(n).__name__}，仅支持数字与 + - * / ** % 括号）")
    try:
        val = eval(compile(node, "<calc>", "eval"), {"__builtins__": {}}, {})
    except Exception as e:
        return f"技能 [calculator] 调用失败：求值错误 {type(e).__name__}: {e}"
    if isinstance(val, float):
        val = round(val, 10)
    return f"{expr} = {val}"


def _calculator(expression: str) -> str:
    """精确计算数学表达式（绕过 LLM 心算不准），返回 `expr = 结果`。"""
    if not isinstance(expression, str) or not expression.strip():
        return "[calculator: 空表达式，未计算]"
    return _safe_eval(expression)


def _cross_check(claim: str) -> str:
    """独立取证：把主张拆成关键术语，逐个与本地文档(query_db)比对 + 含等式时数值重算，
    给出「一致 / 不符 / 无佐证」的核验结论。"""
    if not isinstance(claim, str) or not claim.strip():
        return "[cross_check: 空主张，未核验]"
    notes = []
    # 数值重算：仅当主张形如 `算式 = 值` 且左侧为纯算术时
    if "=" in claim:
        lhs, rhs = claim.split("=", 1)
        lhs, rhs = lhs.strip(), rhs.strip()
        if re.fullmatch(r"[\d\s\+\-\*\/\(\)\.\*\*]+", lhs):
            ev = _safe_eval(lhs)
            if ev.startswith(lhs) and "=" in ev:
                computed = ev.split("=", 1)[1].strip()
                notes.append(f"数值重算：{lhs} = {computed}，主张值 {rhs}"
                             f"（{'一致' if computed == rhs else '不符'}）")
    # 关键词取证：抽取关键术语（标识符 / 中文词），逐个查本地文档，统计佐证比例
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}", claim)
    _stop = {"在", "用", "和", "且", "的", "了", "与", "或", "是", "有", "中", "类", "档",
             "型号", "节点", "间", "传递", "消息", "定义", "本", "项目", "请", "使用",
             "核验", "结论", "是否", "一致", "无法", "核实", "独立", "取证", "文档"}
    terms = [t for t in dict.fromkeys(tokens) if t not in _stop and len(t) >= 2]
    evidence, corroborated = [], 0
    for t in terms[:8]:
        r = _query_db(t)
        if "命中" in r:
            corroborated += 1
            evidence.append(f"「{t}」本地文档命中：" + r.splitlines()[0][:120])
        else:
            evidence.append(f"「{t}」本地文档无佐证")
    verdict = "本地文档有佐证" if corroborated > 0 else "本地文档无佐证"
    return (f"交叉核验主张：「{claim}」\n"
            f"- 关键词取证（{corroborated}/{len(terms)} 命中）：{verdict}\n"
            + ("".join(f"  - {n}\n" for n in evidence) if evidence else "")
            + ("".join(f"- {n}\n" for n in notes) if notes else ""))


def _diff_text(original: str, conclusion: str) -> str:
    """对比原文与结论，找出数字 / 关键词层面的增删改（字面快检，非语义等价）。"""
    if not isinstance(original, str) or not isinstance(conclusion, str):
        return "[diff_text: 两个参数都需为文本]"
    o_nums = re.findall(r"\d+(?:\.\d+)?%?", original)
    c_nums = re.findall(r"\d+(?:\.\d+)?%?", conclusion)
    missing = [n for n in o_nums if n not in c_nums]
    added = [n for n in c_nums if n not in o_nums]
    o_words = set(re.findall(r"[\u4e00-\u9fffA-Za-z]+", original))
    c_words = set(re.findall(r"[\u4e00-\u9fffA-Za-z]+", conclusion))
    dropped = sorted(o_words - c_words)
    lines = []
    if missing:
        lines.append(f"原文有但结论缺失的数字：{missing}")
    if added:
        lines.append(f"结论新增但原文没有的数字：{added}")
    if dropped:
        lines.append(f"结论丢失的关键词（前10）：{dropped[:10]}")
    if not lines:
        lines.append("未发现明显数字/关键词层面不一致（仅作字面快检，非语义等价判定）。")
    return "diff_text 不一致快检：\n- " + "\n- ".join(lines)


# ---------------------------------------------------------------------------
# 第三层技能包（extract / translate / classify / calculate / organize /
# summarize 的领域工具）：纯 stdlib 优先，PDF/OCR 走可选库接缝（缺则优雅降级）。
# ---------------------------------------------------------------------------
# --- extract: 从文本 / PDF / 图片抽取结构化内容 ---
def _extract_fields(text: str, patterns_json: str = "{}") -> str:
    """从文本抽取结构化字段（按自定义正则）或内置常见实体（无模式时）。纯 stdlib。

    patterns_json 形如 {"字段名": "正则表达式"}；留空则抽取 邮箱/网址/电话/日期 等。
    """
    if not isinstance(text, str) or not text.strip():
        return "[extract_fields: 空文本，未抽取]"
    try:
        patterns = json.loads(patterns_json) if patterns_json else {}
    except Exception:
        patterns = {}
    if not isinstance(patterns, dict):
        patterns = {}
    out = []
    if patterns:
        for name, pat in patterns.items():
            try:
                m = re.findall(pat, text, flags=re.S)
            except Exception:
                m = []
            if m:
                vals = [x if isinstance(x, str) else "|".join(map(str, x)) for x in m]
                out.append(f"{name}: {', '.join(vals[:10])}")
            else:
                out.append(f"{name}: 未提供")
    else:
        builtin = {
            "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            "url": r"https?://[^\s]+",
            "phone": r"(?:\+?\d{1,3}[-.\s]?)?(?:\d{3,4}[-.\s]?){2,3}\d{3,4}",
            "date": r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?|\d{4}年\d{1,2}月",
        }
        for name, pat in builtin.items():
            ms = re.findall(pat, text)
            if ms:
                out.append(f"{name}: {', '.join(ms[:10])}")
    if not out:
        return "[extract_fields: 未匹配到任何字段]"
    return "抽取字段：\n" + "\n".join(out)


def _extract_pdf(path: str) -> str:
    """从 PDF 抽文本（可选库接缝：优先 pdfplumber，其次 PyPDF2；均无则优雅提示）。"""
    try:
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(path) as pdf:
                txt = "\n".join((p.extract_text() or "") for p in pdf.pages[:20])
            return f"PDF 文本（前 4000 字符）：\n{txt[:4000]}"
        except ImportError:
            pass
        try:
            import PyPDF2  # type: ignore
            reader = PyPDF2.PdfReader(path)
            txt = "\n".join((pg.extract_text() or "") for pg in reader.pages[:20])
            return f"PDF 文本（前 4000 字符，PyPDF2）：\n{txt[:4000]}"
        except ImportError:
            pass
        return ("技能 [extract_pdf] 调用失败：未安装 PDF 解析库（pdfplumber 或 PyPDF2）。"
                "请 `pip install pdfplumber` 后重试；或改用 extract_fields 处理纯文本。")
    except Exception as e:
        return f"技能 [extract_pdf] 调用失败：{type(e).__name__}: {e}"


def _extract_ocr(image_path: str) -> str:
    """对图片做 OCR（可选库接缝：pytesseract + Pillow；缺则优雅提示）。"""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
        img = Image.open(image_path)
        txt = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return f"OCR 文本（前 4000 字符）：\n{txt[:4000]}"
    except ImportError:
        return ("技能 [extract_ocr] 调用失败：未安装 OCR 依赖（pytesseract + Pillow）。"
                "请 `pip install pytesseract pillow` 并安装 Tesseract 后重试。")
    except Exception as e:
        return f"技能 [extract_ocr] 调用失败：{type(e).__name__}: {e}"


# --- translate: 术语表一致性 ---
def _apply_glossary(text: str, glossary_json: str = "{}") -> str:
    """按术语表统一译名/用词（保持术语一致性）。纯 stdlib。

    glossary_json 形如 {"旧词": "新词"} 或 [["旧词","新词"], ...]。
    """
    if not isinstance(text, str) or not text.strip():
        return "[apply_glossary: 空文本，未处理]"
    try:
        g = json.loads(glossary_json) if glossary_json else {}
    except Exception:
        g = {}
    pairs = []
    if isinstance(g, dict):
        pairs = [(str(k), str(v)) for k, v in g.items()]
    elif isinstance(g, list):
        for item in g:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]), str(item[1])))
    if not pairs:
        return "[apply_glossary: 术语表为空，原文原样返回]"
    applied, result = [], text
    for old, new in pairs:
        if old and old in result:
            cnt = result.count(old)
            result = result.replace(old, new)
            applied.append(f"「{old}」→「{new}」×{cnt}")
    if not applied:
        return "[apply_glossary: 术语表中无词命中原文]"
    return ("术语一致性替换完成：\n- " + "\n- ".join(applied) +
            f"\n\n替换后文本：\n{result[:4000]}")


# --- classify: 分类树打分 ---
def _classify_taxonomy(text: str, taxonomy_json: str = "{}") -> str:
    """按分类体系给文本打标签（关键词打分）。纯 stdlib。

    taxonomy_json 形如 [{"name":"类别A","keywords":["词1","词2"]}, ...]。
    """
    if not isinstance(text, str) or not text.strip():
        return "[classify_taxonomy: 空文本，未分类]"
    try:
        tax = json.loads(taxonomy_json) if taxonomy_json else []
    except Exception:
        tax = []
    if not isinstance(tax, list) or not tax:
        return ("[classify_taxonomy: 未提供分类体系，请传入 taxonomy_json；"
                "或自行给出最贴切类别。]")
    lowered = text.lower()
    scored = []
    for cat in tax:
        if not isinstance(cat, dict):
            continue
        name = cat.get("name", "?")
        kws = cat.get("keywords", []) or []
        hits = [k for k in kws if str(k).lower() in lowered]
        scored.append((name, len(hits), hits))
    scored.sort(key=lambda x: -x[1])
    lines = [f"- {name}（命中 {n}）：{', '.join(hits[:8])}"
             for name, n, hits in scored if n > 0]
    if not lines:
        return "[classify_taxonomy: 文本未命中任何分类关键词，无法确定类别]"
    return "分类结果（按命中降序）：\n" + "\n".join(lines)


# --- calculate: 单位换算 / 表格聚合 ---
_UNIT_FACTORS = {
    # 长度（基准 = 米）
    "m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001, "um": 1e-6,
    "mile": 1609.344, "foot": 0.3048, "ft": 0.3048, "inch": 0.0254, "in": 0.0254,
    "yard": 0.9144, "yd": 0.9144,
    # 质量（基准 = 克）
    "g": 1.0, "kg": 1000.0, "mg": 0.001, "t": 1e6, "ton": 1e6,
    "lb": 453.59237, "oz": 28.349523125,
    # 时间（基准 = 秒）
    "s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0, "hr": 3600.0, "day": 86400.0,
    # 数据（基准 = 字节）
    "b": 1.0, "byte": 1.0, "kb": 1024.0, "mb": 1024.0 ** 2,
    "gb": 1024.0 ** 3, "tb": 1024.0 ** 4,
    # 体积（基准 = 升）
    "l": 1.0, "liter": 1.0, "ml": 0.001, "gallon": 3.785411784,
}
_UNIT_ALIASES = {
    "米": "m", "千米": "km", "公里": "km", "厘米": "cm", "毫米": "mm", "英里": "mile",
    "英尺": "foot", "英寸": "inch", "码": "yard", "克": "g", "千克": "kg",
    "公斤": "kg", "毫克": "mg", "吨": "t", "磅": "lb", "盎司": "oz",
    "秒": "s", "分钟": "min", "小时": "h", "天": "day", "字节": "byte",
    "千字节": "kb", "兆字节": "mb", "吉字节": "gb", "太字节": "tb",
    "升": "l", "毫升": "ml", "加仑": "gallon",
}
_TEMP = {"c": "C", "celsius": "C", "摄氏": "C", "f": "F", "fahrenheit": "F",
         "华氏": "F", "k": "K", "kelvin": "K", "开尔文": "K"}


def _unit_convert(value, from_unit: str, to_unit: str) -> str:
    """单位换算（纯 stdlib 换算表）：长度/质量/时间/数据/体积；温度单独处理。"""
    fu = (from_unit or "").strip().lower()
    tu = (to_unit or "").strip().lower()
    fu = _UNIT_ALIASES.get(fu, fu)
    tu = _UNIT_ALIASES.get(tu, tu)
    try:
        val = float(value)
    except Exception:
        return f"技能 [unit_convert] 调用失败：value 非数值 {value!r}"
    if fu in _TEMP or tu in _TEMP:
        fk, tk = _TEMP.get(fu), _TEMP.get(tu)
        if not fk or not tk:
            return (f"技能 [unit_convert] 调用失败：温度换算需双方均为温度单位"
                    f"（c/f/k），收到 from={from_unit} to={to_unit}")
        k = (val + 273.15) if fk == "C" else ((val - 32) * 5 / 9 + 273.15) if fk == "F" else val
        out = (k - 273.15) if tk == "C" else (k - 273.15) * 9 / 5 + 32 if tk == "F" else k
        return f"{val}{fu} = {out:.4g}{tu}"
    ff, tf = _UNIT_FACTORS.get(fu), _UNIT_FACTORS.get(tu)
    if ff is None or tf is None:
        avail = ", ".join(sorted(set(list(_UNIT_FACTORS) + list(_UNIT_ALIASES))))
        return (f"技能 [unit_convert] 调用失败：不支持的单位 from={from_unit}/to={to_unit}。"
                f" 支持：{avail}")
    return f"{val}{fu} = {val * ff / tf:.6g}{tu}"


def _spreadsheet_calc(csv_text: str, column: str = "", op: str = "sum") -> str:
    """对 CSV 表格做聚合计算（纯 stdlib csv 模块）。纯本地，零依赖。

    column 留空 → 对所有数值单元格聚合；否则按列名（首行表头）或列序号（0 起）定位。
    op ∈ sum/avg/min/max/count。
    """
    import csv as _csv
    if not isinstance(csv_text, str) or not csv_text.strip():
        return "[spreadsheet_calc: 空表格，未计算]"
    try:
        rows = list(_csv.reader(csv_text.splitlines()))
    except Exception as e:
        return f"技能 [spreadsheet_calc] 调用失败：CSV 解析错误 {e}"
    if not rows:
        return "[spreadsheet_calc: 表格无数据行]"
    header, data = rows[0], rows[1:]
    if column:
        if str(column).isdigit():
            idx = int(column)
        else:
            idx = next((i for i, h in enumerate(header)
                        if h.strip().lower() == str(column).strip().lower()), None)
            if idx is None:
                return f"技能 [spreadsheet_calc] 调用失败：未找到列 {column!r}（表头={header}）"
    else:
        idx = None
    nums = []
    for r in data:
        cells = r if idx is None else ([r[idx]] if idx < len(r) else [])
        for c in cells:
            c = str(c).strip().replace(",", "")
            try:
                nums.append(float(c))
            except Exception:
                pass
    if not nums:
        return "[spreadsheet_calc: 未发现可聚合的数值单元格]"
    op = (op or "sum").lower()
    table = {"sum": sum, "avg": lambda x: sum(x) / len(x),
             "min": min, "max": max, "count": lambda x: float(len(x))}
    if op not in table:
        return f"技能 [spreadsheet_calc] 调用失败：不支持的 op={op}（sum/avg/min/max/count）"
    res = table[op](nums)
    return (f"spreadsheet_calc：列={column or '全部数值'} op={op} → {res:.6g}"
            f"（共 {len(nums)} 个数值，范围 [{min(nums):.6g}, {max(nums):.6g}]）")


# --- organize: 套用模板塑形 ---
_TEMPLATES = {
    "bullet": "将每条信息转为独立要点行（以「- 」开头）。",
    "numbered": "将信息按逻辑顺序编号（1. 2. 3.）。",
    "sections": "按主题分段，每段加「## 标题」。",
    "qa": "整理为「问：… 答：…」对。",
}


def _apply_template(content: str, template_name: str = "bullet") -> str:
    """把内容套入指定结构模板（纯 stdlib）。template_name ∈ bullet/numbered/sections/qa。"""
    if not isinstance(content, str) or not content.strip():
        return "[apply_template: 空内容，未套用]"
    tn = (template_name or "bullet").strip().lower()
    if tn not in _TEMPLATES:
        return f"[apply_template: 未知模板 {template_name!r}；可用：{', '.join(_TEMPLATES)}]"
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if tn == "bullet":
        out = "\n".join(f"- {l}" for l in lines)
    elif tn == "numbered":
        out = "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines))
    elif tn == "sections":
        out = "## 要点\n\n" + "\n".join(f"- {l}" for l in lines)
    else:  # qa
        out = "\n\n".join(f"问：{l}\n答：（请补充对应回答）" for l in lines)
    return f"[模板 {tn}] {_TEMPLATES[tn]}\n\n{out}"


# --- summarize: 文体约束后处理 ---
def _apply_style_guide(text: str, guide: str = "concise", max_length: int = 0) -> str:
    """按文体约束后处理文本（纯 stdlib 结构助手）。

    guide ∈ concise（去冗余空白）/ bullets（转要点）/ no_jargon（标出疑似行话）。
    max_length>0 时截断。
    """
    if not isinstance(text, str) or not text.strip():
        return "[apply_style_guide: 空文本，未处理]"
    guide = (guide or "concise").strip().lower()
    t = re.sub(r"\s+", " ", text).strip()
    notes = [f"应用文体：{guide}"]
    if guide == "bullets":
        lines = [l.strip() for l in re.split(r"[。\n.!?]", t) if l.strip()]
        t = "\n".join(f"- {l}" for l in lines)
    elif guide == "no_jargon":
        jargon = ["赋能", "闭环", "对齐", "抓手", "颗粒度", "方法论", "底层逻辑",
                  "组合拳", "心智", "链路", "沉淀", "生态", "范式"]
        hits = [w for w in jargon if w in t]
        if hits:
            notes.append(f"疑似行话（建议替换）：{', '.join(hits)}")
    elif guide != "concise":
        notes.append(f"未知文体 {guide}，按 concise 处理")
    try:
        ml = int(max_length)
    except Exception:
        ml = 0
    if ml and len(t) > ml:
        t = t[:ml] + "…(已截断)"
        notes.append(f"已截断到 {ml} 字")
    return "文体约束：" + "；".join(notes) + f"\n\n{t[:4000]}"


# ---------------------------------------------------------------------------
# 第一层能力深化（2026-08-04）：draw_chart / send_email / query_database
# ---------------------------------------------------------------------------
def _draw_chart(data: str, chart_type: str = "bar", title: str = "") -> str:
    """从结构化数据生成 SVG 图表（纯 stdlib，零外部依赖）。

    data 形如 JSON: [{"label":"A","value":100}, {"label":"B","value":200}]。
    chart_type ∈ bar/line/pie。返回自包含 SVG 文本。
    """
    if not isinstance(data, str) or not data.strip():
        return "[draw_chart: 空数据，未绘图]"
    try:
        items = json.loads(data)
        if not isinstance(items, list) or not items:
            return "[draw_chart: 数据非非空列表]"
    except Exception as e:
        return f"技能 [draw_chart] 调用失败：JSON 解析错误 {e}"

    ct = (chart_type or "bar").strip().lower()
    valid = {"bar", "line", "pie"}
    if ct not in valid:
        return f"[draw_chart: 未知图表类型 {chart_type!r}；可用 {', '.join(valid)}]"

    # 提取 label/value
    pairs = []
    for item in items:
        lbl = str(item.get("label", item.get("name", "?")))
        val = item.get("value", item.get("val", 0))
        try:
            val = float(val)
        except Exception:
            val = 0.0
        pairs.append((lbl, val))

    if not pairs:
        return "[draw_chart: 无有效数据点]"

    title = title or "Chart"
    W, H = 480, 320
    margin_l, margin_b, margin_t = 50, 40, 30
    plot_w = W - margin_l - 20
    plot_h = H - margin_b - margin_t
    max_val = max(v for _, v in pairs) or 1.0

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" style="font-family:sans-serif;font-size:12px">',
        f'<rect width="{W}" height="{H}" fill="#fafafa"/>',
        f'<text x="{W//2}" y="18" text-anchor="middle" font-size="14" '
        f'font-weight="bold">{html.escape(title)}</text>',
    ]

    if ct == "bar":
        bw = plot_w / len(pairs) * 0.7
        gap = plot_w / len(pairs) * 0.3
        for i, (lbl, val) in enumerate(pairs):
            x = margin_l + i * (bw + gap) + gap / 2
            bh = (val / max_val) * plot_h if max_val else 0
            y = margin_t + plot_h - bh
            color = f"hsl({(i * 137) % 360},60%,55%)"
            svg_parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                             f'height="{bh:.1f}" fill="{color}" rx="2"/>')
            svg_parts.append(f'<text x="{x+bw/2:.1f}" y="{margin_t+plot_h+15}" '
                             f'text-anchor="middle">{html.escape(lbl)}</text>')
            svg_parts.append(f'<text x="{x+bw/2:.1f}" y="{y-3:.1f}" '
                             f'text-anchor="middle" font-size="10">{val:g}</text>')
        # Y 轴
        svg_parts.append(f'<line x1="{margin_l}" y1="{margin_t}" '
                         f'x2="{margin_l}" y2="{margin_t+plot_h}" stroke="#999"/>')
        svg_parts.append(f'<line x1="{margin_l}" y1="{margin_t+plot_h}" '
                         f'x2="{margin_l+plot_w}" y2="{margin_t+plot_h}" stroke="#999"/>')

    elif ct == "line":
        pts = []
        for i, (lbl, val) in enumerate(pairs):
            x = margin_l + (i / max(len(pairs) - 1, 1)) * plot_w
            y = margin_t + plot_h - (val / max_val) * plot_h
            pts.append((x, y))
            svg_parts.append(f'<text x="{x:.1f}" y="{margin_t+plot_h+15}" '
                             f'text-anchor="middle" font-size="10">{html.escape(lbl)}</text>')
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        svg_parts.append(f'<polyline points="{path}" fill="none" stroke="#4a90d9" '
                         f'stroke-width="2"/>')
        for x, y in pts:
            svg_parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#4a90d9"/>')
        svg_parts.append(f'<line x1="{margin_l}" y1="{margin_t}" '
                         f'x2="{margin_l}" y2="{margin_t+plot_h}" stroke="#999"/>')
        svg_parts.append(f'<line x1="{margin_l}" y1="{margin_t+plot_h}" '
                         f'x2="{margin_l+plot_w}" y2="{margin_t+plot_h}" stroke="#999"/>')

    elif ct == "pie":
        total = sum(v for _, v in pairs) or 1.0
        cx, cy, r = W // 2, H // 2 + 5, min(plot_w, plot_h) // 2
        angle = -90  # 从顶部开始
        for i, (lbl, val) in enumerate(pairs):
            sweep = (val / total) * 360
            color = f"hsl({(i * 137) % 360},60%,55%)"
            # 简化：用 stroke-dasharray 画饼图段
            rad_start = math_radians(angle)
            rad_end = math_radians(angle + sweep)
            x1 = cx + r * math_cos(rad_start)
            y1 = cy + r * math_sin(rad_start)
            x2 = cx + r * math_cos(rad_end)
            y2 = cy + r * math_sin(rad_end)
            large = 1 if sweep > 180 else 0
            svg_parts.append(
                f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{r},{r} 0 {large},1 '
                f'{x2:.1f},{y2:.1f} Z" fill="{color}" stroke="white" stroke-width="1"/>')
            # 标签
            mid = math_radians(angle + sweep / 2)
            lx = cx + (r + 15) * math_cos(mid)
            ly = cy + (r + 15) * math_sin(mid)
            pct = val / total * 100
            svg_parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                             f'font-size="10">{html.escape(lbl)} {pct:.0f}%</text>')
            angle += sweep

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def _math_radians(deg):
    import math
    return math.radians(deg)


def _math_cos(rad):
    import math
    return math.cos(rad)


def _math_sin(rad):
    import math
    return math.sin(rad)


math_radians = _math_radians
math_cos = _math_cos
math_sin = _math_sin


def _send_email(to: str, subject: str, body: str) -> str:
    """通过 SMTP 发送邮件（需环境变量配置 SMTP_HOST/SMTP_USER/SMTP_PASS）。

    未配置时返回明确提示（不崩溃）；配置后用 smtplib 真发。
    安全：SMTP_PASS 只从环境变量读，绝不记录到日志/返回值。
    """
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    port = int(os.environ.get("SMTP_PORT", "587"))

    if not host or not user:
        return ("技能 [send_email] 未配置：需设环境变量 SMTP_HOST + SMTP_USER + SMTP_PASS。"
                "配置后即可真实发送邮件。")

    if not to or not subject:
        return "[send_email: 收件人(to)和主题(subject)不能为空]"

    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body or "", "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        with smtplib.SMTP(host, port, timeout=10) as srv:
            srv.starttls()
            srv.login(user, pwd)
            srv.sendmail(user, [to], msg.as_string())
        return f"邮件已发送：{to} | 主题：{subject}"
    except Exception as e:
        return f"技能 [send_email] 调用失败：{type(e).__name__}: {e}"


def _query_database(query: str, db_path: str = "") -> str:
    """执行 SQL 查询（sqlite3 stdlib，零依赖；可选 PostgreSQL/MySQL）。

    db_path 留空 → 读环境变量 DATABASE_PATH（默认 :memory:）。
    安全：只允许 SELECT 查询（拒绝 INSERT/UPDATE/DELETE/DROP 等）。
    """
    if not isinstance(query, str) or not query.strip():
        return "[query_database: 空 SQL，未执行]"

    # 安全：只允许 SELECT
    q_upper = query.strip().upper()
    forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
                 "TRUNCATE", "REPLACE", "ATTACH", "DETACH")
    for kw in forbidden:
        if q_upper.startswith(kw):
            return f"[query_database: 安全拒绝——不允许 {kw} 语句，只允许 SELECT]"

    db_path = db_path or os.environ.get("DATABASE_PATH", ":memory:")

    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query)
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return f"技能 [query_database] 调用失败：{type(e).__name__}: {e}"

    if not rows:
        return "查询结果：无数据（空集）"

    cols = [d[0] for d in cur.description] if cur.description else []
    lines = ["\t".join(cols)]
    for row in rows[:50]:  # 最多返回 50 行
        lines.append("\t".join(str(v) for v in row))
    if len(rows) > 50:
        lines.append(f"...(共 {len(rows)} 行，仅显示前 50 行)")
    return "查询结果：\n" + "\n".join(lines)


# 注册表：新增技能只需在此加一项（并实现 handler）。
SKILLS = {
    "run_code": {
        "name": "run_code",
        "description": (
            "执行一段 Python 代码并返回其 stdout / stderr / 退出码。"
            "用于验证数值计算、跑小型算法、检查推理中的算术逻辑。"
            "只接受纯计算/打印类代码；不要执行有副作用的操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码（含 print 以输出结果）",
                }
            },
            "required": ["code"],
        },
        "handler": _run_code,
    },
    "web_search": {
        "name": "web_search",
        "description": (
            "根据查询主动搜索网络，返回若干条结果摘要与来源链接。"
            "用于获取实时 / 外部信息。无结果时返回「未检索到」。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询词"},
                "max_results": {"type": "integer",
                                "description": "返回结果条数（默认 5）"},
            },
            "required": ["query"],
        },
        "handler": _web_search,
    },
    "read_page": {
        "name": "read_page",
        "description": (
            "读取指定 URL 的网页全文（联网），返回去标签后的正文文本"
            "（截断至前 4000 字符）。用于深入阅读检索到的页面。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要读取的网页 URL"},
            },
            "required": ["url"],
        },
        "handler": _read_page,
    },
    "query_db": {
        "name": "query_db",
        "description": (
            "查询本地项目文档与源码（在工作区 docs/ 与 circuit-agents 中按关键词检索），"
            "返回命中行与文件路径。用于检索内部知识库，零外部依赖。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
            },
            "required": ["query"],
        },
        "handler": _query_db,
    },
    "calculator": {
        "name": "calculator",
        "description": (
            "精确计算数学表达式（支持 + - * / ** % 与括号），绕过 LLM 心算不准。"
            "返回「表达式 = 结果」。涉及数值时优先用它而非心算。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string",
                               "description": "算术表达式，如 (10000*(1+0.035*5))"},
            },
            "required": ["expression"],
        },
        "handler": _calculator,
    },
    "cross_check": {
        "name": "cross_check",
        "description": (
            "独立取证核验：把一条主张与本地项目文档比对，若含「算式 = 值」则重算核对数值，"
            "给出「一致 / 不符 / 无佐证」结论。用于 verify 节点的客观核验。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "claim": {"type": "string",
                          "description": "待核验的主张 / 结论文本"},
            },
            "required": ["claim"],
        },
        "handler": _cross_check,
    },
    "diff_text": {
        "name": "diff_text",
        "description": (
            "对比「原文」与「结论」，找出数字与关键词层面的增删改（字面快检），"
            "帮助 verify 节点发现结论对原文的偏离。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "original": {"type": "string", "description": "原始证据文本"},
                "conclusion": {"type": "string", "description": "待核对结论文本"},
            },
            "required": ["original", "conclusion"],
        },
        "handler": _diff_text,
    },
    "extract_fields": {
        "name": "extract_fields",
        "description": (
            "从文本中抽取结构化字段：可传入自定义正则模式 {字段名: 正则}，"
            "或留空自动抽取邮箱/网址/电话/日期等常见实体。用于 extract 节点把非结构化"
            "文本转成键值记录。纯本地，零依赖。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待抽取的原始文本"},
                "patterns_json": {"type": "string",
                                  "description": "可选，{字段名: 正则} 的 JSON 字符串"},
            },
            "required": ["text"],
        },
        "handler": _extract_fields,
    },
    "extract_pdf": {
        "name": "extract_pdf",
        "description": (
            "从 PDF 文件抽取文本（前 20 页）。优先 pdfplumber、其次 PyPDF2；"
            "两者均未安装时返回明确提示（需 pip install pdfplumber）。用于 extract 节点处理 PDF 源。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "PDF 文件路径"},
            },
            "required": ["path"],
        },
        "handler": _extract_pdf,
    },
    "extract_ocr": {
        "name": "extract_ocr",
        "description": (
            "对图片做 OCR 识别文字（中英文）。依赖 pytesseract + Pillow；"
            "未安装时返回明确提示（需 pip install pytesseract pillow 并装 Tesseract）。"
            "用于 extract 节点处理图片源。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "图片文件路径"},
            },
            "required": ["image_path"],
        },
        "handler": _extract_ocr,
    },
    "apply_glossary": {
        "name": "apply_glossary",
        "description": (
            "按术语表统一译名/用词，保持术语一致性。glossary_json 形如 {旧词: 新词} 或"
            "[[旧词,新词],...]。翻译/本地化场景保证专有名词统一。纯本地，零依赖。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待处理文本"},
                "glossary_json": {"type": "string",
                                  "description": "可选，术语表 JSON 字符串"},
            },
            "required": ["text"],
        },
        "handler": _apply_glossary,
    },
    "classify_taxonomy": {
        "name": "classify_taxonomy",
        "description": (
            "按分类体系给文本打标签（关键词打分）。taxonomy_json 形如 "
            "[{name:类别, keywords:[词...]}, ...]，返回按命中降序的类别+依据。纯本地，零依赖。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待分类文本"},
                "taxonomy_json": {"type": "string",
                                  "description": "可选，分类体系 JSON 字符串"},
            },
            "required": ["text"],
        },
        "handler": _classify_taxonomy,
    },
    "unit_convert": {
        "name": "unit_convert",
        "description": (
            "单位换算（纯本地换算表）：长度/质量/时间/数据/体积 + 温度（含偏移）。"
            "绕过 LLM 单位混乱。返回「值 原单位 = 结果 目标单位」。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "value": {"type": "number", "description": "待换算数值"},
                "from_unit": {"type": "string", "description": "原单位（如 km/m/kg/C/F）"},
                "to_unit": {"type": "string", "description": "目标单位"},
            },
            "required": ["value", "from_unit", "to_unit"],
        },
        "handler": _unit_convert,
    },
    "spreadsheet_calc": {
        "name": "spreadsheet_calc",
        "description": (
            "对 CSV 表格做聚合计算（纯本地 csv 模块）：sum/avg/min/max/count。"
            "column 留空则聚合全部数值单元格，否则按列名或列序号(0起)定位。calculate 节点用其算表。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "csv_text": {"type": "string", "description": "CSV 文本（含表头行）"},
                "column": {"type": "string",
                            "description": "可选，列名或列序号；留空=全部数值"},
                "op": {"type": "string", "description": "聚合操作 sum/avg/min/max/count（默认 sum）"},
            },
            "required": ["csv_text"],
        },
        "handler": _spreadsheet_calc,
    },
    "apply_template": {
        "name": "apply_template",
        "description": (
            "把零散内容套入结构模板塑形（纯本地）：bullet/numbered/sections/qa。"
            "organize 节点用它把信息重排成清晰结构。未知模板返回可用列表。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "待整理的内容（按行拆分）"},
                "template_name": {"type": "string",
                                  "description": "模板名 bullet/numbered/sections/qa（默认 bullet）"},
            },
            "required": ["content"],
        },
        "handler": _apply_template,
    },
    "apply_style_guide": {
        "name": "apply_style_guide",
        "description": (
            "按文体约束后处理文本（纯本地）：concise（去冗余空白）/ bullets（转要点）/"
            "no_jargon（标疑似行话）；max_length>0 时截断。summarize 节点用它落实交付文体。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待处理文本"},
                "guide": {"type": "string",
                           "description": "文体 concise/bullets/no_jargon（默认 concise）"},
                "max_length": {"type": "integer",
                               "description": "可选，最大长度；超出截断"},
            },
            "required": ["text"],
        },
        "handler": _apply_style_guide,
    },
    # 第一层能力深化（2026-08-04）：draw_chart / send_email / query_database
    "draw_chart": {
        "name": "draw_chart",
        "description": (
            "从结构化数据生成 SVG 图表（纯本地，零依赖）：bar（柱状图）/"
            "line（折线图）/pie（饼图）。data 为 JSON 数组 [{label,value}, ...]。"
            "compare/organize 节点可用它可视化对比结果。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data": {"type": "string",
                          "description": "JSON 数组，如 [{\"label\":\"A\",\"value\":100}]"},
                "chart_type": {"type": "string",
                                "description": "图表类型 bar/line/pie（默认 bar）"},
                "title": {"type": "string", "description": "图表标题（可选）"},
            },
            "required": ["data"],
        },
        "handler": _draw_chart,
    },
    "send_email": {
        "name": "send_email",
        "description": (
            "通过 SMTP 发送邮件：需环境变量 SMTP_HOST + SMTP_USER + SMTP_PASS 配置。"
            "未配置时返回明确提示不崩溃。organize 节点可用它交付最终结果。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人邮箱地址"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文"},
            },
            "required": ["to", "subject"],
        },
        "handler": _send_email,
    },
    "query_database": {
        "name": "query_database",
        "description": (
            "执行 SQL SELECT 查询（sqlite3 stdlib，零依赖）：db_path 留空时读 "
            "DATABASE_PATH 环境变量（默认 :memory:）。安全限制：只允许 SELECT，"
            "拒绝写入/修改语句。retrieve 节点可用它从数据库取数据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SQL SELECT 查询语句"},
                "db_path": {"type": "string",
                             "description": "数据库文件路径（可选，默认读 DATABASE_PATH 环境变量）"},
            },
            "required": ["query"],
        },
        "handler": _query_database,
    },
}


# ---------------------------------------------------------------------------
# 装配 / 执行辅助
# ---------------------------------------------------------------------------
def build_tools_schema(skill_names) -> list:
    """把技能名列表转成 OpenAI `tools` 参数（只含已注册的）。"""
    out = []
    if not skill_names:
        return out
    for name in skill_names:
        spec = SKILLS.get(name)
        if not spec:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        })
    return out


def execute_skill(name: str, arguments_json: str) -> str:
    """按名执行技能，返回字符串结果（供回灌为 tool 消息）。

    任何解析/执行异常都被吞掉并转成可读错误文本——让模型有机会自行纠正，
    而不是让一次技能失败炸掉整条链路（延续内核"开路但不崩"的精神）。
    """
    spec = SKILLS.get(name)
    if not spec:
        return f"[技能未注册: {name}]"
    try:
        args = json.loads(arguments_json) if arguments_json else {}
        if not isinstance(args, dict):
            return f"[技能参数非对象: {type(args).__name__}]"
    except Exception as e:
        return f"[技能参数 JSON 解析失败: {e}]（原始参数: {arguments_json!r}）"
    try:
        result = spec["handler"](**args)
    except TypeError as e:
        return f"技能 [{name}] 调用失败：参数不匹配 {type(e).__name__}: {e}"
    except Exception as e:
        return f"技能 [{name}] 调用失败：{type(e).__name__}: {e}"
    if not isinstance(result, str):
        try:
            result = str(result)
        except Exception:
            result = repr(result)
    return result


def skill_declaration_text(skill_names) -> str:
    """生成注入 system 提示词的技能声明文本（让模型知道"你可以用这些技能"）。"""
    lines = []
    for name in skill_names:
        spec = SKILLS.get(name)
        if not spec:
            continue
        lines.append(f"- {name}：{spec['description']}")
    if not lines:
        return ""
    return ("\n\n你可调用以下技能（需要时主动调用，禁止编造不存在的技能）：\n"
            + "\n".join(lines))
