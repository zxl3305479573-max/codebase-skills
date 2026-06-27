# -*- coding: utf-8 -*-
"""
Codebase Skill — 一键安装脚本

自动完成：
1. pip 依赖安装
2. tree-sitter 语言包编译
3. ONNX 模型下载 (BGE-small-en-v1.5, 384维)
4. 环境验证

用法: python install.py
"""

import os
import subprocess
import sys
from pathlib import Path

# Windows GBK 兼容：强制 stdout 使用 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
REQUIREMENTS = SKILL_DIR / "requirements.txt"
MODEL_CACHE = Path.home() / ".cache" / "codebase-skill" / "models"

# ASCII-safe status markers
OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def run(cmd: list[str], desc: str) -> bool:
    """运行命令并打印进度"""
    print(f"  {desc}...", end=" ", flush=True)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            print(OK)
            return True
        else:
            print(f"{FAIL} (exit {result.returncode})")
            if result.stderr.strip():
                lines = result.stderr.strip().split("\n")
                for line in lines[-2:]:
                    print(f"     {line}")
            return False
    except subprocess.TimeoutExpired:
        print(f"{FAIL} (timeout)")
        return False
    except FileNotFoundError:
        print(f"{FAIL} (command not found)")
        return False


def step1_check_python() -> bool:
    """检查 Python 版本"""
    print("[1/5] Python version check")
    ver = sys.version_info
    if ver >= (3, 10):
        print(f"  Python {ver.major}.{ver.minor}.{ver.micro} {OK}")
        return True
    print(f"  Python {ver.major}.{ver.minor} {FAIL} (need >= 3.10)")
    return False


def step2_install_pip_deps() -> bool:
    """安装 pip 依赖"""
    print("[2/5] pip dependencies")

    pip_cmd = [
        sys.executable, "-m", "pip", "install",
        "-r", str(REQUIREMENTS),
        "--quiet", "--disable-pip-version-check",
    ]

    # 国内用户尝试清华镜像加速
    mirrors = [
        [],
        ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
        ["-i", "https://pypi.org/simple"],
    ]

    for i, mirror in enumerate(mirrors):
        label = f"pip install (attempt {i+1}/{len(mirrors)})"
        if run(pip_cmd + mirror, label):
            return True

    print(f"  {WARN} pip install partially failed, will use fallback mode")
    return False


def step3_setup_tree_sitter() -> bool:
    """下载 tree-sitter 语言包"""
    print("[3/5] tree-sitter language packs")

    languages = {
        "python": "tree-sitter-python",
        "javascript": "tree-sitter-javascript",
        "typescript": "tree-sitter-typescript",
    }

    all_ok = True
    for lang, pkg in languages.items():
        ok = run(
            [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
            f"tree-sitter-{lang}"
        )
        if not ok:
            all_ok = False

    return all_ok


def step4_download_onnx_model() -> bool:
    """下载 ONNX 嵌入模型"""
    print("[4/5] ONNX model (BGE-small-en-v1.5, 384-dim)")

    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    model_dir = MODEL_CACHE / "bge-small-en-v1.5"
    onnx_file = model_dir / "model.onnx"

    if onnx_file.exists():
        size_mb = onnx_file.stat().st_size / (1024 * 1024)
        print(f"  Model already cached: {onnx_file} ({size_mb:.1f} MB) {OK}")
        return True

    # 方法1: optimum-cli (推荐)
    print("  Downloading (~130MB, may take a few minutes)...")
    print("  Mirror: HuggingFace (hf-mirror.com fallback if needed)")
    ok = run(
        [sys.executable, "-m", "optimum.exporters.onnx",
         "--model", "BAAI/bge-small-en-v1.5",
         "--for-ort",
         str(model_dir)],
        "optimum-cli export"
    )
    if ok:
        return True

    # 方法2: huggingface_hub 直接下载
    print("  Trying huggingface_hub direct download...")
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            "BAAI/bge-small-en-v1.5",
            local_dir=str(model_dir),
            local_dir_use_symlinks=False,
        )
        if onnx_file.exists():
            print(f"  Model downloaded {OK}")
            return True
        else:
            print("  Downloaded but need ONNX conversion...")
            ok2 = run(
                [sys.executable, "-m", "optimum.exporters.onnx",
                 "--model", str(model_dir),
                 "--for-ort",
                 str(model_dir)],
                "optimum-cli convert"
            )
            return ok2
    except ImportError:
        print(f"  {WARN} huggingface_hub not installed")
    except Exception as e:
        print(f"  {WARN} download failed: {e}")

    print(f"  Model will be auto-downloaded on first use (needs network)")
    print(f"  {WARN} Using hash fallback mode for now")
    return False


def step5_verify() -> dict:
    """验证安装"""
    print("[5/5] Environment verification")
    print()

    results = {}
    checks = [
        ("lancedb", "LanceDB (vector DB)"),
        ("onnxruntime", "ONNX Runtime (local embeddings)"),
        ("tree_sitter", "tree-sitter (JS/TS AST)"),
        ("rank_bm25", "rank-bm25 (keyword search)"),
        ("anthropic", "Anthropic SDK (query rewrite)"),
    ]

    for module, label in checks:
        try:
            __import__(module)
            print(f"  {OK} {label}")
            results[module] = True
        except ImportError:
            print(f"  {WARN} {label} — fallback mode")
            results[module] = False

    # ONNX Model file
    model_path = MODEL_CACHE / "bge-small-en-v1.5" / "model.onnx"
    if model_path.exists():
        size_mb = model_path.stat().st_size / (1024 * 1024)
        print(f"  {OK} BGE-small model ({size_mb:.1f} MB)")
        results["model_ready"] = True
    else:
        print(f"  {WARN} BGE-small model — hash fallback")
        results["model_ready"] = False

    return results


def print_summary(results: dict) -> None:
    """打印安装摘要"""
    ok_count = sum(1 for v in results.values() if v)
    total = len(results)

    print()
    print("=" * 50)
    print("  Installation Summary")
    print("=" * 50)
    print(f"  Components ready: {ok_count}/{total}")
    print()

    if ok_count == total:
        print("  [DONE] Full semantic search + query rewrite available!")
    elif ok_count >= 3:
        print("  [OK] Core functionality ready. Install missing")
        print("       components for better accuracy.")
    else:
        print("  [OK] Fallback mode ready (JSON index + hash vectors).")
        print("       Run: pip install -r requirements.txt")
        print("       for full semantic search.")

    print()
    print(f"  Model cache: {MODEL_CACHE}")
    print("=" * 50)


def main():
    print()
    print("=" * 48)
    print("  Codebase Skill — One-Click Install")
    print("=" * 48)
    print()

    if not step1_check_python():
        sys.exit(1)

    step2_install_pip_deps()
    step3_setup_tree_sitter()
    step4_download_onnx_model()
    results = step5_verify()
    print_summary(results)
    sys.exit(0)


if __name__ == "__main__":
    main()
