"""CI 静态检查脚本 —— 无需外部依赖（Redis/ES/LLM）即可运行。

检查项：
  1. 全部 Python 文件语法可编译
  2. 核心模块可导入（不触发网络调用）
  3. 配置文件可解析且结构合法
  4. 前端 JS 文件存在且非空
  5. 仓库中不含真实密钥（防回归）

用法：python scripts/ci_checks.py
退出码：0 = 全部通过，1 = 存在失败项
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"   {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


# --------------------------------------------------------------------------
print("=" * 70)
print("1) Python 语法检查")
print("=" * 70)
py_files = [
    p for p in ROOT.rglob("*.py")
    if not any(part in p.parts for part in (".venv", "__pycache__", ".git"))
]
bad_syntax: list[str] = []
for p in py_files:
    try:
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    except SyntaxError as e:
        bad_syntax.append(f"{p.relative_to(ROOT)}:{e.lineno} {e.msg}")
check(f"{len(py_files)} 个 Python 文件语法正确", not bad_syntax,
      "" if not bad_syntax else f"失败: {bad_syntax[:3]}")

# --------------------------------------------------------------------------
print()
print("=" * 70)
print("2) 核心模块导入（不触发网络）")
print("=" * 70)
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# 这些模块在导入期不应发起网络请求；缺密钥只应降级而非崩溃
import importlib

for mod in ("server.config", "server.database", "server.utils.security",
            "server.utils.sse", "server.agent.state", "server.agent.base_tool",
            "server.llm.base"):
    try:
        importlib.import_module(mod)
        check(f"import {mod}", True)
    except Exception as e:
        check(f"import {mod}", False, f"{type(e).__name__}: {e}")

for mod in ("server.main", "gateway.main"):
    try:
        m = importlib.import_module(mod)
        title = getattr(m.app, "title", "?")
        check(f"import {mod} (app = {title!r})", True)
    except Exception as e:
        check(f"import {mod}", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------
print()
print("=" * 70)
print("3) 配置文件结构")
print("=" * 70)
try:
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    providers = cfg.get("llm", {}).get("providers", [])
    check("settings.yaml 可解析", True)
    check(f"providers 非空（{len(providers)} 个）", len(providers) > 0)
    idx = cfg["llm"].get("active_provider", 0)
    check(f"active_provider={idx} 在范围内", 0 <= idx < len(providers))
    for p in providers:
        missing = [k for k in ("name", "base_url", "api_key_env", "default_model")
                   if not p.get(k)]
        check(f"provider {p.get('name')!r} 字段完整", not missing,
              "" if not missing else f"缺 {missing}")
except Exception as e:
    check("settings.yaml 可解析", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------
print()
print("=" * 70)
print("4) 前端静态资源")
print("=" * 70)
js_dir = ROOT / "static" / "js"
js_files = sorted(js_dir.glob("*.js"))
check(f"前端 JS 模块存在（{len(js_files)} 个）", len(js_files) >= 8)
empty = [p.name for p in js_files if p.stat().st_size == 0]
check("无空 JS 文件", not empty, "" if not empty else str(empty))
for asset in ("static/index.html", "static/css/style.css",
              "static/lib/marked.min.js", "static/lib/highlight.min.js"):
    check(f"{asset} 存在", (ROOT / asset).exists())

# --------------------------------------------------------------------------
print()
print("=" * 70)
print("5) 密钥防泄漏（防回归）")
print("=" * 70)
# 真实密钥的特征：OpenAI/DashScope 风格的长 token
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")
scanned = 0
leaks: list[str] = []
for p in ROOT.rglob("*"):
    if not p.is_file():
        continue
    if any(part in p.parts for part in (".venv", "__pycache__", ".git", "chroma_db")):
        continue
    if p.name == ".env":          # .env 本身含密钥属预期，且已被 .gitignore 排除
        continue
    if p.suffix.lower() in (".png", ".jpg", ".ico", ".pdf", ".bin", ".db", ".sqlite3"):
        continue
    if p.stat().st_size > 3_000_000:
        continue
    scanned += 1
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    if SECRET_RE.search(text):
        leaks.append(str(p.relative_to(ROOT)))
check(f"扫描 {scanned} 个文件未发现真实密钥", not leaks,
      "" if not leaks else f"泄漏: {leaks}")

# .env 必须仍在 .gitignore 中
gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
check(".gitignore 仍排除 .env", bool(re.search(r"^\.env\s*$", gi, re.M)))

# --------------------------------------------------------------------------
print()
print("=" * 70)
if FAILURES:
    print(f"结果: {CHECKS - len(FAILURES)}/{CHECKS} 通过，{len(FAILURES)} 项失败")
    for f in FAILURES:
        print(f"  FAILED: {f}")
    sys.exit(1)
print(f"结果: {CHECKS}/{CHECKS} 全部通过")
sys.exit(0)
