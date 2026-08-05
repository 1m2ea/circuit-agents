#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
circuit-agents 便携 AI 工作站启动器 (portable_launch.py)

放置位置（U 盘）:
    U盘根/AI/launch.py        <- 本文件
    U盘根/AI/ollama/          <- Ollama 程序 + models/
    U盘根/AI/circuit-agents/  <- 本仓库
    U盘根/AI/python-portable/ <- 便携 Python（可选）

插上任意一台 >=16GB 内存的电脑，运行:
    python launch.py            # 起 Ollama + 起 circuit-agents server
    python launch.py --check    # 干跑：只校验路径/环境，不启动进程（适合本地逻辑自检）

启动后访问: http://localhost:8765  (Live Console)
拔掉 U 盘前先 Ctrl+C 关闭本脚本，本机不留模型/服务痕迹。
"""

import argparse
import os
import subprocess
import sys
import time
import platform
import urllib.request
import urllib.error

DEFAULT_MODELS = ["qwen2.5:7b", "deepseek-coder-v2", "qwen2.5:14b"]
OLLAMA_PORT = "11434"
SERVER_PORT = "8765"


def ai_root(script_dir):
    """默认把脚本所在目录当作 U盘/AI/ 根。可用 --ai-root 覆盖。"""
    return script_dir


def ollama_bin(ai):
    if platform.system() == "Windows":
        return os.path.join(ai, "ollama", "ollama.exe")
    return os.path.join(ai, "ollama", "ollama")


def models_dir(ai):
    return os.path.join(ai, "ollama", "models")


def circuit_dir(ai):
    return os.path.join(ai, "circuit-agents")


def python_bin(ai, override):
    if override:
        return override
    # 优先用 U 盘自带便携 Python
    pp = os.path.join(ai, "python-portable")
    if platform.system() == "Windows":
        cand = os.path.join(pp, "python.exe")
    else:
        cand = os.path.join(pp, "bin", "python3")
    if os.path.exists(cand):
        return cand
    return sys.executable


def ollama_health(host):
    url = "http://%s/api/tags" % host
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def run_check(ai, host, models, py):
    """干跑自检：校验目录/程序是否存在，打印将要执行的计划。"""
    print("== portable_launch --check ==")
    print("AI 根目录      : %s" % ai)
    ob = ollama_bin(ai)
    print("Ollama 程序    : %s  %s" % (ob, "[OK]" if os.path.exists(ob) else "[缺失!]"))
    md = models_dir(ai)
    print("模型存储目录   : %s  %s" % (md, "[存在]" if os.path.isdir(md) else "[待创建]"))
    cd = circuit_dir(ai)
    print("circuit-agents : %s  %s" % (cd, "[OK]" if os.path.isdir(cd) else "[缺失!]"))
    print("Python 解释器  : %s" % py)
    print("OLLAMA_HOST    : %s" % host)
    print("OLLAMA_MODELS  : %s" % md)
    print("计划拉取模型   : %s" % ", ".join(models))
    print("circuit server : http://localhost:%s" % SERVER_PORT)
    print("== 校验完成（未启动任何进程）==")
    missing = not os.path.exists(ob) or not os.path.isdir(cd)
    return 0 if not missing else 2


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="circuit-agents 便携工作站启动器")
    ap.add_argument("--ai-root", default=here, help="U盘/AI/ 根目录（默认=本脚本所在目录）")
    ap.add_argument("--host", default="127.0.0.1:" + OLLAMA_PORT, help="Ollama 监听地址")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS),
                    help="启动时确保已拉取的模型（逗号分隔）")
    ap.add_argument("--python", default=None, help="运行 circuit-agents server 的 Python 路径")
    ap.add_argument("--server-port", default=SERVER_PORT, help="circuit-agents server 端口")
    ap.add_argument("--no-pull", action="store_true", help="跳过模型拉取")
    ap.add_argument("--check", action="store_true", help="干跑自检，不启动进程")
    args = ap.parse_args()

    ai = os.path.abspath(args.ai_root)
    model_list = [m.strip() for m in args.models.split(",") if m.strip()]
    py = python_bin(ai, args.python)

    if args.check:
        return run_check(ai, args.host, model_list, py)

    ob = ollama_bin(ai)
    if not os.path.exists(ob):
        print("[!] 未找到 Ollama 程序: %s" % ob)
        print("    请把 ollama 程序放到 AI/ollama/ 下（Windows 为 ollama.exe）")
        return 2
    if not os.path.isdir(circuit_dir(ai)):
        print("[!] 未找到 circuit-agents: %s" % circuit_dir(ai))
        return 2

    env = os.environ.copy()
    env["OLLAMA_MODELS"] = models_dir(ai)
    env["OLLAMA_HOST"] = args.host
    os.makedirs(models_dir(ai), exist_ok=True)

    # 1) 启动 Ollama serve
    print("[1/4] 启动 Ollama (host=%s) ..." % args.host)
    ol = subprocess.Popen([ob, "serve"], env=env)
    ready = False
    for _ in range(30):
        if ollama_health(args.host):
            ready = True
            break
        if ol.poll() is not None:
            print("[!] Ollama 进程异常退出，码=%s" % ol.returncode)
            return 3
        time.sleep(1)
    if not ready:
        print("[!] Ollama 在 30s 内未就绪，请检查端口/模型目录权限")
        ol.terminate()
        return 3
    print("      Ollama 就绪 ✓")

    # 2) 拉取缺失模型
    if not args.no_pull:
        print("[2/4] 校验/拉取模型: %s" % ", ".join(model_list))
        for m in model_list:
            try:
                subprocess.run([ob, "pull", m], env=env, check=True)
                print("      + %s" % m)
            except subprocess.CalledProcessError as e:
                print("      [!] 拉取 %s 失败: %s（可离线跳过，或手动 ollama pull）" % (m, e))
    else:
        print("[2/4] 跳过模型拉取 (--no-pull)")

    # 3) 启动 circuit-agents server
    print("[3/4] 启动 circuit-agents server (port=%s) ..." % args.server_port)
    srv = subprocess.Popen(
        [py, "server.py", "--host", "0.0.0.0", "--port", args.server_port],
        cwd=circuit_dir(ai), env=env)
    time.sleep(2)
    if srv.poll() is not None:
        print("[!] circuit-agents server 启动失败，码=%s" % srv.returncode)
        ol.terminate()
        return 4
    print("      server 就绪 ✓  ->  http://localhost:%s" % args.server_port)

    # 4) 常驻，Ctrl+C 干净退出
    print("[4/4] 便携工作站已就绪。Ctrl+C 关闭（本机不留痕迹）。")
    try:
        while True:
            time.sleep(1)
            if ol.poll() is not None:
                print("[!] Ollama 已退出，关闭 server")
                break
            if srv.poll() is not None:
                print("[!] server 已退出，关闭 Ollama")
                break
    except KeyboardInterrupt:
        print("\n[.] 收到 Ctrl+C，正在关闭 ...")
    finally:
        for p in (srv, ol):
            try:
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    print("[.] 已关闭，本机无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
