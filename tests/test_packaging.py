# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# -*- coding: utf-8 -*-
"""打包自检：console_scripts 入口、package-data 覆盖范围、依赖声明

`pip install .` 装出来的 wheel 里必须同时有代码与前端资源：
  - app.py / config.py（py-modules）与 analyzer / parser / webapp（packages）；
  - web/templates/*.html 与 web/static/**（package-data）——这类文件不在 .py 里，
    setuptools 默认不收，漏了就是安装后首页 500。

源码侧只能"模拟"setuptools 的收集过程（用 glob 覆盖逐个文件核对），所以 CI 里还有一个
package job 真正构建 wheel、拆包比对 web/ 下的文件清单，并安装后跑 `qqchatlog --version`。
本文件负责的是快速反馈：入口点写错、新增模板/静态文件忘了改 glob、新增第三方 import
忘了写进 dependencies，都会在这里立刻红。
"""
import ast
import atexit
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:  # tomllib 是 3.11+ 的标准库；3.10 由 CI 装上 tomli（见 .github/workflows/test.yml）
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 仅 Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

# 测试隔离：数据目录指向临时目录，绝不碰真实 uploads/ai_cache/session。
# 只清理"自己创建的"目录——外部显式指定的 QQCHAT_DATA_DIR 一律不动。
def _drop_temp_data_dir():
    """跑完把临时数据目录删掉（先关日志：否则清理先跑，logging.shutdown 又把 app.log 写回来）"""
    import logging
    logging.shutdown()
    shutil.rmtree(os.environ["QQCHAT_DATA_DIR"], ignore_errors=True)


if "QQCHAT_DATA_DIR" not in os.environ:
    os.environ["QQCHAT_DATA_DIR"] = tempfile.mkdtemp(prefix="qqchatlog-test-")
    atexit.register(_drop_temp_data_dir)
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as configmod  # noqa: E402
import web as webpkg  # noqa: E402

import app as appmod  # noqa: E402

#: 第三方 import 名 -> 发行包名（两者拼写不一致的少数几个）
_DIST_ALIASES = {"dotenv": "python-dotenv"}

#: 本仓库自己的顶层模块/包，扫描依赖声明时跳过
_FIRST_PARTY = {"app", "config", "analyzer", "parser", "webapp", "web"}


def _pyproject() -> dict:
    if tomllib is None:  # pragma: no cover - Python 3.10 且没装 tomli
        raise unittest.SkipTest("需要 tomllib（Python 3.11+）或 tomli")
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def _normalize_dist(name: str) -> str:
    """把发行名/import 名归一到可比较形式：小写、-/_ 统一、去掉 extras 与版本约束"""
    base = name.split("[")[0].split(";")[0]
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<"):
        base = base.split(sep)[0]
    return base.strip().lower().replace("_", "-")


class TestConsoleScriptEntryPoint(unittest.TestCase):
    """console_scripts 入口：声明、目标、以及 --version 真的能跑"""

    def test_pyproject_declares_script(self):
        scripts = _pyproject()["project"]["scripts"]
        self.assertEqual(scripts.get("qqchatlog"), "app:main",
                         "[project.scripts] 里 qqchatlog 必须指向 app:main")

    def test_entry_target_is_callable(self):
        self.assertTrue(callable(appmod.main), "app.main 必须存在且可调用")
        self.assertEqual(appmod.main.__module__, "app")

    def test_main_version_prints_and_exits_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            appmod.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertRegex(buf.getvalue().strip(), r"^qqchatlog \S+$")

    def test_startup_report_allows_loopback_by_default(self):
        """横幅/自检从 __main__ 里搬出来了：回环地址 + 无口令必须放行"""
        buf = io.StringIO()
        with mock.patch.object(appmod, "FLASK_HOST", "127.0.0.1"), \
                mock.patch.object(appmod, "ACCESS_PASSWORD", ""), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            allowed = appmod._startup_report()
        self.assertTrue(allowed)
        self.assertIn("QQ 聊天记录分析工具", buf.getvalue())

    def test_startup_report_refuses_public_bind_without_password(self):
        """非回环地址没设口令：体检返回 False，由 main() 以退出码 1 结束（不再 sys.exit 打断调用方）"""
        buf = io.StringIO()
        with mock.patch.object(appmod, "FLASK_HOST", "0.0.0.0"), \
                mock.patch.object(appmod, "ACCESS_PASSWORD", ""), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            allowed = appmod._startup_report()
        self.assertFalse(allowed)
        self.assertIn("已拒绝启动", buf.getvalue())


class TestPackageDataCoverage(unittest.TestCase):
    """package-data 必须逐个文件覆盖 web/ 下的模板与静态资源"""

    def _setuptools_config(self) -> dict:
        return _pyproject()["tool"]["setuptools"]

    def test_declared_modules_and_packages_exist(self):
        tool = self._setuptools_config()
        self.assertEqual(tool["py-modules"], ["app", "config"])
        self.assertEqual(tool["packages"], ["analyzer", "parser", "webapp", "web"])
        for name in tool["py-modules"]:
            with self.subTest(module=name):
                self.assertTrue((ROOT / f"{name}.py").is_file(), f"{name}.py 不存在")
                self.assertIsNotNone(importlib.util.find_spec(name), f"{name} 不可导入")
        for name in tool["packages"]:
            with self.subTest(package=name):
                self.assertTrue((ROOT / name / "__init__.py").is_file(),
                                f"{name}/__init__.py 不存在（没有它 setuptools 不会收包内数据）")
                self.assertIsNotNone(importlib.util.find_spec(name), f"{name} 不可导入")

    def test_no_undeclared_python_package_on_disk(self):
        """仓库里出现新的顶层包（有 __init__.py）却没写进 packages，安装后就会缺模块"""
        declared = set(self._setuptools_config()["packages"])
        # 排除可能存在的本地环境目录（venv/build 产物），它们不是本项目的包
        ignored = {"tests", "docs", "tools", "venv", ".venv", "build", "dist"}
        found = {
            path.parent.name
            for path in ROOT.glob("*/__init__.py")
            if path.parent.name not in ignored
        }
        self.assertEqual(found - declared, set(), "这些包没写进 [tool.setuptools] packages")

    def test_package_data_covers_every_web_asset(self):
        globs = self._setuptools_config()["package-data"]["web"]
        for pattern in globs:
            self.assertFalse(pattern.startswith(("/", "..")), f"glob 必须是包内相对路径: {pattern}")
        covered = set()
        for pattern in globs:
            for path in (ROOT / "web").glob(pattern):
                if path.is_file():
                    covered.add(path.relative_to(ROOT / "web").as_posix())
        actual = {
            path.relative_to(ROOT / "web").as_posix()
            for path in (ROOT / "web").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith(".")
        }
        actual.discard("__init__.py")  # Python 模块由 packages 机制收，不是 package-data 的活儿
        missing = sorted(actual - covered)
        self.assertEqual(missing, [], f"这些前端资源不在 package-data 里，装进 wheel 会丢: {missing}")
        self.assertTrue(covered, "package-data 一个文件都没匹配到，glob 写错了")

    def test_vendor_snapshot_is_covered_recursively(self):
        """vendor/ 是子目录，glob 少了 ** 就会静默漏掉整套本地化前端库"""
        globs = " ".join(self._setuptools_config()["package-data"]["web"])
        self.assertIn("static/**", globs)
        vendor = sorted((ROOT / "web" / "static" / "vendor").glob("*.*"))
        self.assertTrue(vendor, "vendor/ 下应当有本地化的前端库文件")
        covered = {
            path.relative_to(ROOT / "web").as_posix()
            for pattern in self._setuptools_config()["package-data"]["web"]
            for path in (ROOT / "web").glob(pattern)
            if path.is_file()
        }
        for path in vendor:
            with self.subTest(asset=path.name):
                self.assertIn(path.relative_to(ROOT / "web").as_posix(), covered)


class TestFlaskAssetWiring(unittest.TestCase):
    """Flask 拿到的模板/静态目录必须是包内绝对路径（安装后从任何目录都能找到）"""

    def test_web_package_exposes_asset_dirs(self):
        pkg_dir = Path(webpkg.__file__).resolve().parent
        self.assertEqual(webpkg.ROOT, pkg_dir)
        self.assertEqual(webpkg.TEMPLATES_DIR, pkg_dir / "templates")
        self.assertEqual(webpkg.STATIC_DIR, pkg_dir / "static")
        self.assertTrue(webpkg.TEMPLATES_DIR.is_dir())
        self.assertTrue(webpkg.STATIC_DIR.is_dir())

    def test_app_uses_package_absolute_dirs(self):
        repo_root = Path(appmod.__file__).resolve().parent
        for folder in (appmod.app.template_folder, appmod.app.static_folder):
            with self.subTest(folder=folder):
                path = Path(folder)
                self.assertTrue(path.is_absolute(), f"{folder} 不该是相对工作目录的路径")
                self.assertTrue(path.is_dir(), f"{folder} 不存在")
                self.assertEqual(path.parent, repo_root / "web",
                                 "模板/静态目录必须解析到 web 包内（源码与安装后同构）")
        self.assertEqual(appmod.app.static_url_path, "/static",
                         "URL 前缀变了会让模板/导出报告里的 /static/... 全部 404")

    def test_templates_are_found_without_cwd_dependence(self):
        names = sorted(p.name for p in webpkg.TEMPLATES_DIR.glob("*.html"))
        self.assertTrue(names, "web/templates 下没有模板")
        for name in names:
            with self.subTest(template=name):
                self.assertIsNotNone(appmod.app.jinja_env.get_template(name))


class TestDependencyDeclarations(unittest.TestCase):
    """第三方 import 必须都在 [project] dependencies 里，否则装完就 ImportError"""

    def _declared(self) -> set:
        return {_normalize_dist(dep) for dep in _pyproject()["project"]["dependencies"]}

    def _imported(self) -> set:
        modules = set()
        sources = [ROOT / "app.py", ROOT / "config.py", ROOT / "web" / "__init__.py"]
        for package in ("analyzer", "parser", "webapp"):
            sources.extend(sorted((ROOT / package).glob("*.py")))
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
        third_party = modules - set(sys.stdlib_module_names) - _FIRST_PARTY
        return {_DIST_ALIASES.get(name, name).lower().replace("_", "-") for name in third_party}

    def test_every_third_party_import_is_declared(self):
        undeclared = sorted(self._imported() - self._declared())
        self.assertEqual(undeclared, [], f"这些包被 import 了却没写进 dependencies: {undeclared}")

    def test_declared_runtime_deps_are_the_expected_five(self):
        """依赖面有意保持在 5 个直连包；多一个都要在这里显式改，避免悄悄膨胀"""
        self.assertEqual(
            self._declared(),
            {"flask", "flask-session", "openai", "jieba", "python-dotenv"},
        )

    def test_requirements_pins_every_declared_dependency(self):
        """开发/CI 的 requirements.txt 必须钉住 pyproject 声明的每个运行期依赖，避免两处漂移"""
        pinned = set()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                pinned.add(_normalize_dist(line))
        missing = sorted(self._declared() - pinned)
        self.assertEqual(missing, [], f"requirements.txt 里没有钉住这些运行期依赖: {missing}")


class TestDataDirDefaults(unittest.TestCase):
    """数据目录默认值：源码检出在仓库内，安装态退到用户数据目录"""

    def test_source_checkout_keeps_data_in_repo(self):
        self.assertTrue(configmod._is_source_checkout(), "本仓库应当被识别为源码检出")
        self.assertEqual(configmod.DEFAULT_DATA_DIR, configmod.BASE_DIR)

    def test_user_data_dir_is_absolute_and_app_scoped(self):
        path = configmod._user_data_dir()
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, "qqchatlog")


class TestDataDirEnvFile(unittest.TestCase):
    """数据目录下的 .env 必须被读到

    pip 安装后没有"项目根目录"可以放配置，用户数据目录是唯一确定的位置（`.secret_key` 也在那儿），
    README 的安装说明按这个行为写。`.env` 已在进程内导入过，所以只能用子进程验证。
    """

    #: 候选断言键：要挑一个本仓库 .env 里没定义的，否则本地开发时项目 .env 会先赢（这正是设计优先级）
    CANDIDATES = (
        ("QQCHAT_FACE_FETCH_LIMIT", "77", "config.FACE_FETCH_LIMIT"),
        ("LOG_RETENTION_DAYS", "5", "config.LOG_RETENTION_DAYS"),
        ("FLASK_PORT", "5099", "config.FLASK_PORT"),
    )

    def _probe(self, data_dir: Path, expression: str, extra_env=None) -> str:
        """在子进程里导入 config 并打印某个常量（cwd 与数据目录都指向临时目录）"""
        env = {**os.environ, "PYTHONPATH": str(ROOT), "QQCHAT_DATA_DIR": str(data_dir)}
        env.update(extra_env or {})
        result = subprocess.run([sys.executable, "-c", f"import config; print({expression})"],
                                cwd=str(data_dir), env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _local_env_keys(self) -> str:
        """本地 .env 的原文（只用来判断某个键是否已被定义，不打印内容）"""
        path = ROOT / ".env"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def test_env_file_in_data_dir_is_loaded(self):
        local_env = self._local_env_keys()
        for key, value, expression in self.CANDIDATES:
            if f"{key}=" in local_env or key in os.environ:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / ".env").write_text(f"{key}={value}\n", encoding="utf-8")
                self.assertEqual(self._probe(Path(tmp), expression), value,
                                 f"数据目录下的 .env 没被读到（{key}）")
            return
        self.skipTest("本仓库 .env / 环境变量把候选键都占了，跳过（CI 无 .env，必然执行）")

    def test_real_env_var_still_wins(self):
        """真实环境变量优先级最高：与数据目录 .env 冲突时不能被覆盖"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("FLASK_PORT=5099\n", encoding="utf-8")
            probed = self._probe(Path(tmp), "config.FLASK_PORT", {"FLASK_PORT": "5111"})
        self.assertEqual(probed, "5111")


if __name__ == "__main__":
    unittest.main()
