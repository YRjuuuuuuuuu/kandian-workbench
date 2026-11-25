"""Build the Windows x64 DeepSeek distribution from official runtime artifacts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from content_engine import __version__
from content_engine.crawler_runtime import bundled_media_status

PYTHON_VERSION = "3.14.10"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
DIRECTORIES = ("components", "content_engine", "modes", "prompts", "skills", "web")
FILES = ("启动工作台.cmd", "停止工作台.cmd", "LICENSE")
SCRIPTS = ("workbench_control.py", "python_runtime.ps1")
CACHE = ROOT / "tools" / "standalone-build"


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_wheels(target, requirements):
    wheels = CACHE / "wheels"
    flags = ["--disable-pip-version-check", "--platform", "win_amd64", "--python-version", "3.14", "--abi", "cp314", "--only-binary=:all:"]
    subprocess.run([sys.executable, "-m", "pip", "download", *flags, "--dest", str(wheels), "--index-url", "https://pypi.org/simple", "--require-hashes", "-r", str(requirements)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", *flags, "--no-index", "--find-links", str(wheels), "--require-hashes", "--no-compile", "--target", str(target), "-r", str(requirements)], check=True)


def build_runtime(runtime):
    cache = CACHE
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / PYTHON_URL.rsplit("/", 1)[-1]
    if not archive.exists():
        partial = archive.with_suffix(".download")
        with urllib.request.urlopen(PYTHON_URL, timeout=60) as response, partial.open("wb") as file:
            shutil.copyfileobj(response, file)
        if checksum(partial) != PYTHON_SHA256:
            raise ValueError("Python 安装包 SHA256 校验失败")
        partial.replace(archive)
    if checksum(archive) != PYTHON_SHA256:
        raise ValueError("Python 安装包 SHA256 校验失败")
    runtime.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(runtime)
    (runtime / "python314._pth").write_text("python314.zip\n.\n..\nLib/site-packages\n", encoding="ascii")
    requirements = ROOT / "scripts" / "standalone-requirements.txt"
    install_wheels(runtime / "Lib" / "site-packages", requirements)
    shutil.copy2(requirements, runtime / "dependencies.txt")


def build_media_runtime(app):
    media = app / "runtime" / "hotlists"
    requirements = ROOT / "scripts" / "hotlist-requirements.txt"
    reusable = bundled_media_status(ROOT)
    installed = ROOT / "runtime" / "hotlists"
    if (reusable["browser_ready"] and (installed / "dependencies.txt").is_file()
            and (installed / "dependencies.txt").read_bytes() == requirements.read_bytes()):
        shutil.copytree(installed, media, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
    else:
        dependencies = media / "dependencies"
        install_wheels(dependencies, requirements)
        shutil.copy2(requirements, media / "dependencies.txt")
        # Use the browser downloader from the exact locked Playwright driver.
        cache = CACHE / "playwright-browsers"
        env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(cache))
        bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv[1]); "
                     "sys.argv=['playwright','install','--only-shell','chromium']; "
                     "runpy.run_module('playwright',run_name='__main__')")
        subprocess.run([str(app / "runtime" / "python.exe"), "-c", bootstrap, str(dependencies)],
                       cwd=app, env=env, check=True)
        browsers = media / "browsers"
        browsers.mkdir()
        for path in cache.iterdir():
            if path.is_dir() and path.name.startswith(("chromium_headless_shell-", "ffmpeg-", "winldd-")):
                shutil.copytree(path, browsers / path.name, ignore=shutil.ignore_patterns("*.log"))
    status = bundled_media_status(app)
    if not status["browser_ready"]:
        raise ValueError("内置热榜运行时缺少匹配的无头 Chromium")
    smoke = app.parent / "hotlists-build-smoke.json"
    subprocess.run([status["python"], "-X", "utf8", status["worker"], "--smoke", "--output", str(smoke)],
                   cwd=app, env=dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=status["browser_cache"]),
                   timeout=55, check=True)
    result = json.loads(smoke.read_text(encoding="utf-8"))
    if result["status"] != "completed" or len(result["records"]) != 3:
        raise ValueError(f"内置热榜组件自检失败：{result}")


def build(app, output, install_runtime=False, reuse_runtime=False):
    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if os.name != "nt" or not shell:
        raise RuntimeError("请在 Windows x64 上构建独立包")
    subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "scripts" / "build_launcher.ps1")], check=True, cwd=ROOT)
    app.mkdir()
    for directory in DIRECTORIES:
        shutil.copytree(ROOT / directory, app / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    for name in FILES:
        shutil.copy2(ROOT / name, app / name)
    shutil.copy2(ROOT / "看点工场.exe", app / "看点工场.exe")
    (app / "scripts").mkdir()
    for name in SCRIPTS:
        shutil.copy2(ROOT / "scripts" / name, app / "scripts" / name)
    shutil.copy2(ROOT / "docs" / "windows.md", app / "使用说明.md")
    (app / "standalone.deepseek").write_text("DeepSeek API\n", encoding="utf-8")
    runtime = app / "runtime"
    if reuse_runtime:
        installed = ROOT / "runtime"
        if (installed / "dependencies.txt").read_bytes() != (ROOT / "scripts" / "standalone-requirements.txt").read_bytes():
            raise ValueError("现有运行时的依赖锁定文件不匹配")
        shutil.copytree(installed, runtime, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "hotlists", "mediacrawler"))
    else:
        build_runtime(runtime)
    snippet = f"import sys; assert sys.version.split()[0] == {PYTHON_VERSION!r}; from PIL import Image; import matplotlib; import numpy; import certifi; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai')"
    subprocess.run([str(runtime / "python.exe"), "-c", snippet], check=True, cwd=app)
    build_media_runtime(app)
    output.mkdir(parents=True, exist_ok=True)
    filename = f"kandian-workbench-{__version__}-windows-x64.zip"
    archive = output / filename
    partial = archive.with_suffix(".tmp")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(app.rglob("*")):
            relative = path.relative_to(app)
            # pip's unused console stubs embed the builder's interpreter path.
            if (path.is_file() and "__pycache__" not in relative.parts and path.suffix not in (".pyc", ".log")
                    and relative.parts[:4] not in (("runtime", "Lib", "site-packages", "bin"),
                                                  ("runtime", "hotlists", "dependencies", "bin"))):
                bundle.write(path, Path("看点工场") / relative)
    with zipfile.ZipFile(partial) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("发行包完整性校验失败")
    partial.replace(archive)
    sha256 = checksum(archive)
    archive.with_suffix(".sha256").write_text(f"{sha256}  {filename}\n", encoding="utf-8")
    if install_runtime:
        target = ROOT / "runtime"
        if not target.exists():
            shutil.copytree(runtime, target)
        elif not (target / "hotlists").exists():
            shutil.copytree(runtime / "hotlists", target / "hotlists")
    print(json.dumps({"file": str(archive), "sha256": sha256, "bytes": archive.stat().st_size}, ensure_ascii=False))


def main():
    import tempfile
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--install-runtime", action="store_true")
    parser.add_argument("--reuse-runtime", action="store_true", help="复用本次已构建并安装的便携运行时")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="standalone-") as staging:
        build(Path(staging) / "app", args.output_dir.resolve(), args.install_runtime, args.reuse_runtime)


if __name__ == "__main__":
    main()
