"""circuit-agents · 跨平台部署导出（⑫）

把一份电路拓扑 Spec 导出为可独立部署的产物：
  · Dockerfile            —— 容器化部署（FastAPI + uvicorn）
  · requirements.txt      —— 依赖锁
  · runner.py             —— 内嵌 Spec 的 standalone 运行器（server / cli 两种模式）
  · <name>_cli.py         —— 单文件 CLI 封装（直接 python 跑）
  · README.md             —— 部署说明

设计（第二层边界扩展 · ⑫）：
  · 全部为『纯文本生成』，无 Docker/网络依赖，离线可用、可 CI 断言。
  · 生成的 runner/CLI 内嵌 Spec JSON，运行时 from runtime import ... 直接执行，
    因此导出的产物不带源码差异、可复制分发。
  · 范围：⑫ 聚焦『导出可部署包』，不含真实 build/push 镜像（留给 CI）。
"""
from __future__ import annotations

import json
import os
from typing import Optional


class DeploymentExporter:
    """⑫ 跨平台部署：把 Spec 导出为 Dockerfile / runner / CLI 封装。"""

    BASE_REQUIREMENTS = [
        "fastapi>=0.110",
        "uvicorn>=0.29",
        "pydantic>=2.6",
        "requests>=2.31",
    ]

    # ---- Dockerfile ----
    @classmethod
    def generate_dockerfile(cls, python_version: str = "3.13", port: int = 8000,
                            entry: str = "server:app",
                            requirements_file: str = "requirements.txt") -> str:
        return (
            f"FROM python:{python_version}-slim\n"
            f"WORKDIR /app\n"
            f"ENV PYTHONUNBUFFERED=1\n"
            f"COPY {requirements_file} .\n"
            f"RUN pip install --no-cache-dir -r {requirements_file}\n"
            f"COPY . .\n"
            f"EXPOSE {port}\n"
            f'CMD ["uvicorn", "{entry}", "--host", "0.0.0.0", "--port", "{port}"]\n'
        )

    # ---- requirements ----
    @classmethod
    def generate_requirements(cls, extra: Optional[list] = None) -> list:
        reqs = list(cls.BASE_REQUIREMENTS)
        if extra:
            reqs.extend(extra)
        return reqs

    # ---- runner（内嵌 Spec）----
    @classmethod
    def generate_runner(cls, spec: dict, mode: str = "server",
                        port: int = 8000, name: str = "circuit-app") -> str:
        spec_json = json.dumps(spec, ensure_ascii=False)
        if mode == "server":
            return (
                "import json\n"
                "from fastapi import FastAPI\n"
                "from runtime import Circuit, SimBackend, CircuitExecutor\n"
                "import random\n\n"
                f"SPEC = json.loads({spec_json!r})\n"
                "app = FastAPI(title=" + repr(name) + ")\n\n"
                "@app.post('/run')\n"
                "def run():\n"
                "    circuit = Circuit(SPEC, SimBackend(random.Random(0)))\n"
                "    return CircuitExecutor(circuit, memory_enabled=False).run()\n\n"
                "if __name__ == '__main__':\n"
                "    import uvicorn\n"
                f"    uvicorn.run(app, host='0.0.0.0', port={port})\n"
            )
        # cli 模式
        return (
            "import json\nimport sys\nimport random\n"
            "from runtime import Circuit, SimBackend, CircuitExecutor\n\n"
            f"SPEC = json.loads({spec_json!r})\n\n"
            "def main():\n"
            "    circuit = Circuit(SPEC, SimBackend(random.Random(0)))\n"
            "    res = CircuitExecutor(circuit, memory_enabled=False).run()\n"
            "    print(json.dumps(res, ensure_ascii=False, indent=2))\n"
            "    return 0 if res.get('success') else 1\n\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        )

    # ---- 单文件 CLI 封装 ----
    @classmethod
    def make_cli(cls, spec: dict, out_path: str, name: str = "circuit-cli") -> str:
        code = cls.generate_runner(spec, mode="cli", name=name)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(code)
        return out_path

    # ---- 整套导出包 ----
    @classmethod
    def export_bundle(cls, spec: dict, out_dir: str, name: str = "circuit-app",
                      mode: str = "server", port: int = 8000,
                      extra_reqs: Optional[list] = None) -> dict:
        os.makedirs(out_dir, exist_ok=True)
        paths = {}

        df_path = os.path.join(out_dir, "Dockerfile")
        with open(df_path, "w", encoding="utf-8") as f:
            f.write(cls.generate_dockerfile(port=port, entry=f"{name}_runner:app"))
        paths["dockerfile"] = df_path

        req_path = os.path.join(out_dir, "requirements.txt")
        with open(req_path, "w", encoding="utf-8") as f:
            f.write("\n".join(cls.generate_requirements(extra_reqs)) + "\n")
        paths["requirements"] = req_path

        runner_path = os.path.join(out_dir, f"{name}_runner.py")
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(cls.generate_runner(spec, mode=mode, port=port, name=name))
        paths["runner"] = runner_path

        readme = (
            f"# {name} 部署包\n\n"
            f"由 circuit-agents ⑫ 跨平台部署导出。\n\n"
            f"## 容器部署\n```bash\n"
            f"docker build -t {name} .\n"
            f"docker run -p {port}:{port} {name}\n"
            f"```\n\n"
            f"## 直接运行（CLI 模式导出时）\n```bash\n"
            f"pip install -r requirements.txt\n"
            f"python {name}_runner.py\n"
            f"```\n\n"
            f"## 端点\n- POST /run ：执行内嵌电路拓扑，返回结果 JSON\n"
        )
        readme_path = os.path.join(out_dir, "README.md")
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(readme)
        paths["readme"] = readme_path

        return paths


def deploy_selftest():
    """⑫ 跨平台部署离线自检：Dockerfile/runner/CLI 生成 + 语法/内容断言。"""
    spec = {"name": "demo", "components": {
        "src": {"type": "power", "label": "src"},
        "A": {"type": "resistor", "label": "A", "model": "small",
              "produced_outputs": ["x"]},
    }, "wires": [["src", "A"]]}

    # 1) Dockerfile 关键指令
    df = DeploymentExporter.generate_dockerfile(port=8000)
    assert "FROM python:3.13-slim" in df, "应含基础镜像"
    assert "EXPOSE 8000" in df, "应暴露端口"
    assert 'CMD ["uvicorn"' in df, "应以 uvicorn 启动"
    print("✓ ⑫ Dockerfile 生成正确（FROM/EXPOSE/CMD uvicorn）")

    # 2) requirements
    reqs = DeploymentExporter.generate_requirements()
    assert any(r.startswith("fastapi") for r in reqs) and any(r.startswith("uvicorn") for r in reqs)
    print(f"✓ ⑫ requirements 生成正确（{len(reqs)} 项依赖）")

    # 3) runner（server / cli）语法有效且含关键符号
    srv = DeploymentExporter.generate_runner(spec, mode="server")
    cli = DeploymentExporter.generate_runner(spec, mode="cli")
    compile(srv, "<server_runner>", "exec")   # 语法校验
    compile(cli, "<cli_runner>", "exec")
    assert "uvicorn.run" in srv and "CircuitExecutor" in srv
    assert "sys.exit(main())" in cli and "CircuitExecutor" in cli
    print("✓ ⑫ runner 生成正确（server/cli 均语法有效，内嵌 Spec 执行）")

    # 4) 单文件 CLI 落盘
    import tempfile
    tmp = tempfile.mkdtemp()
    cli_path = os.path.join(tmp, "demo_cli.py")
    DeploymentExporter.make_cli(spec, cli_path)
    assert os.path.exists(cli_path) and os.path.getsize(cli_path) > 0
    print("✓ ⑫ make_cli 单文件 CLI 落盘成功")

    # 5) 整套导出包
    bundle = DeploymentExporter.export_bundle(spec, tmp, name="demo", mode="server")
    for k, p in bundle.items():
        assert os.path.exists(p) and os.path.getsize(p) > 0, f"{k} 应生成非空文件"
    assert set(bundle.keys()) == {"dockerfile", "requirements", "runner", "readme"}
    print(f"✓ ⑫ export_bundle 整套导出成功（{len(bundle)} 个文件："
          f"Dockerfile/requirements/runner/README）")

    print("\n⑫ 跨平台部署 离线自检全部通过 ✓")


if __name__ == "__main__":
    deploy_selftest()
