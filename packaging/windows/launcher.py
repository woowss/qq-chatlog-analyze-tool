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
"""Windows 桌面启动器。

安装版只需要用户双击一次：这里负责启动 Flask WSGI 服务、等待健康检查通过、
打开默认浏览器，并提供停止服务、打开配置和打开数据目录的入口。服务运行在同一
进程的后台线程里，因此关闭窗口可以调用 Werkzeug 的正常 shutdown，而不是粗暴
终止一个子进程；正在进行的分析会先看到进程级 shutdown 标志并停止派发新调用。

该文件故意只依赖 Python 标准库，导入时不碰 tkinter、Flask 或项目配置。这样
打包分析、源码级测试和 ``--version`` 都不会因为桌面环境缺少 GUI 而失败。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Optional


APP_TITLE = "QQ 聊天记录分析工具"
APP_MUTEX_PREFIX = "Local\\QQChatLog-"
DEFAULT_STARTUP_TIMEOUT = 30.0
HEALTH_TIMEOUT = 0.75
HEALTH_POLL_INTERVAL = 0.2
RUNTIME_FILE_NAME = "runtime.json"
ENV_FILE_NAME = ".env"

_MUTEX_HANDLE = None

_ENV_TEMPLATE = """# QQ 聊天记录分析工具配置
# 修改后重启应用生效；不需要 AI 分析时可以保持 DEEPSEEK_API_KEY 为空。

# DEEPSEEK_API_KEY=
# DEEPSEEK_MODEL=deepseek-flash
# DEEPSEEK_BASE_URL=https://api.deepseek.com/v1

# 端口被占用时可改成其他本机端口。
# FLASK_PORT=5000

# 如需把数据迁移到其他磁盘，填写绝对路径。
# QQCHAT_DATA_DIR=
"""
_LOCAL_HOSTS = frozenset(("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"))


def _prepare_source_import_path() -> None:
    """源码直接运行启动器时，把仓库根目录放入导入路径。"""
    if getattr(sys, "frozen", False):
        return
    root = Path(__file__).resolve().parents[2]
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


def _load_app_module():
    """延迟导入应用，避免纯工具函数和测试依赖桌面/运行时环境。"""
    _prepare_source_import_path()
    import app

    return app


def _load_config_module():
    _prepare_source_import_path()
    import config

    return config


def browser_host(host: str) -> str:
    """把监听用的通配地址转换成浏览器可访问的本机地址。"""
    normalized = (host or "127.0.0.1").strip()
    if normalized in ("0.0.0.0", "::"):
        return "127.0.0.1"
    return normalized


def build_service_url(host: str, port: int) -> str:
    """生成兼容 IPv4/IPv6 的本地访问地址。"""
    display_host = browser_host(host)
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{int(port)}/"


def health_url(host: str, port: int) -> str:
    """生成不带尾部页面路径的健康检查地址。"""
    return build_service_url(host, port).rstrip("/") + "/health"


def probe_health(url: str, timeout: float = HEALTH_TIMEOUT) -> bool:
    """检查本机服务是否真的返回健康探针，而不是只检查端口是否打开。"""
    try:
        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200 and response.read(16).decode("utf-8", "replace").strip() == "ok"
    except (OSError, urllib.error.URLError, ValueError):
        return False


def wait_for_health(
    url: str,
    timeout: float = DEFAULT_STARTUP_TIMEOUT,
    poll_interval: float = HEALTH_POLL_INTERVAL,
    probe: Callable[[str, float], bool] = probe_health,
) -> bool:
    """在有限时间内等待服务就绪。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if probe(url, min(HEALTH_TIMEOUT, max(0.05, deadline - time.monotonic()))):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.01, poll_interval))


def _socket_family(host: str) -> int:
    return socket.AF_INET6 if ":" in (host or "") else socket.AF_INET


def port_is_available(host: str, port: int) -> bool:
    """做一次尽力而为的端口探测；真正启动时仍以 make_server 绑定为准。"""
    sock = socket.socket(_socket_family(host), socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_available_port(host: str, preferred: int, attempts: int = 100) -> int:
    """从配置端口开始寻找可用端口。"""
    start = max(1, min(65535, int(preferred)))
    for offset in range(max(1, attempts)):
        candidate = start + offset
        if candidate > 65535:
            break
        if port_is_available(host, candidate):
            return candidate
    raise OSError(f"找不到可用端口（起始端口 {start}，检查范围 {attempts} 个）")


def runtime_file(data_dir: Path) -> Path:
    return Path(data_dir) / RUNTIME_FILE_NAME


def write_runtime_info(path: Path, host: str, port: int, pid: Optional[int] = None) -> None:
    """原子写入本地运行信息，供第二次双击时找到现有实例。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": int(pid if pid is not None else os.getpid()),
        "host": str(host),
        "port": int(port),
    }
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".runtime-", suffix=".tmp", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def read_runtime_info(path: Path) -> Optional[dict]:
    """读取并验证本地运行信息；损坏或过期格式直接忽略。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    host = payload.get("host")
    port = payload.get("port")
    pid = payload.get("pid")
    if not isinstance(host, str) or host.strip().lower() not in _LOCAL_HOSTS:
        return None
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    return {"host": host, "port": port, "pid": pid}


def remove_runtime_info(path: Path, pid: Optional[int] = None) -> None:
    """只删除当前实例写入的运行信息，避免误删后启动的其他实例信息。"""
    payload = read_runtime_info(path)
    if payload is None or (pid is not None and payload["pid"] != pid):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # 运行信息只是辅助文件，删除失败不影响程序退出。
        return


def ensure_env_file(data_dir: Path) -> Path:
    """首次打开配置时创建安全模板，绝不覆盖已有配置。"""
    path = Path(data_dir) / ENV_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(_ENV_TEMPLATE)
        except FileExistsError:
            pass
    return path


def mutex_name(data_dir: Path) -> str:
    """按数据目录生成稳定的 Windows 本地互斥体名称。"""
    normalized = str(Path(data_dir).resolve()).casefold().encode("utf-8", "replace")
    digest = hashlib.sha256(normalized).hexdigest()[:24]
    return APP_MUTEX_PREFIX + digest


def acquire_single_instance(data_dir: Path) -> bool:
    """同一数据目录只允许一个桌面启动器实例。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, mutex_name(data_dir))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


def release_single_instance() -> None:
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE is None or os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle(_MUTEX_HANDLE)
    _MUTEX_HANDLE = None


def _open_path(path: Path) -> None:
    """用 Windows 文件关联打开文件或目录。"""
    if path.exists() or path.suffix or path.name.startswith("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        raise OSError("桌面启动器只支持 Windows 文件关联")


def _show_existing_instance(data_dir: Path) -> bool:
    """第二次双击时打开已有服务；返回是否已经处理本次启动。"""
    info = read_runtime_info(runtime_file(data_dir))
    if info is None:
        return False
    url = build_service_url(info["host"], info["port"])
    if not probe_health(health_url(info["host"], info["port"])):
        return False
    webbrowser.open(url)
    return True


class DesktopController:
    """Tk 窗口与 WSGI 服务的生命周期控制器。"""

    def __init__(self, root, tk, ttk, messagebox, server, server_thread, app_module, data_dir, host, port):
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.server = server
        self.server_thread = server_thread
        self.app_module = app_module
        self.data_dir = Path(data_dir)
        self.host = host
        self.port = port
        self.url = build_service_url(host, port)
        self.runtime_path = runtime_file(self.data_dir)
        self.stopping = False

        root.title(APP_TITLE)
        root.geometry("560x300")
        root.minsize(520, 260)
        root.protocol("WM_DELETE_WINDOW", self.request_close)

        outer = ttk.Frame(root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_TITLE, font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text="本地服务由此窗口管理，聊天记录和缓存只保存在本机。", wraplength=510).pack(
            anchor="w", pady=(8, 14)
        )

        self.status = tk.StringVar(value="正在等待服务就绪……")
        ttk.Label(outer, textvariable=self.status, foreground="#245a8d").pack(anchor="w")
        ttk.Label(outer, text=f"访问地址：{self.url}").pack(anchor="w", pady=(4, 0))
        ttk.Label(outer, text=f"数据目录：{self.data_dir}", wraplength=510).pack(anchor="w", pady=(4, 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(22, 0))
        self.open_button = ttk.Button(buttons, text="打开分析页面", command=self.open_browser)
        self.open_button.pack(side="left")
        ttk.Button(buttons, text="打开配置文件", command=self.open_config).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="打开数据目录", command=self.open_data).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="打开日志目录", command=self.open_logs).pack(side="left", padx=(8, 0))
        self.close_button = ttk.Button(buttons, text="停止并退出", command=self.request_close)
        self.close_button.pack(side="right")

        self.root.after(80, self._begin_health_probe)

    def _begin_health_probe(self):
        threading.Thread(target=self._wait_and_open_browser, daemon=True, name="health-probe").start()

    def _wait_and_open_browser(self):
        ready = wait_for_health(health_url(self.host, self.port))
        self.root.after(0, lambda: self._on_ready(ready))

    def _on_ready(self, ready: bool):
        if self.stopping:
            return
        if ready:
            self.status.set("服务已启动，浏览器即将打开。")
            self.open_browser()
        else:
            self.status.set("服务已启动，但健康检查超时，请点击“打开分析页面”重试。")

    def open_browser(self):
        if not self.stopping:
            webbrowser.open(self.url)

    def open_config(self):
        try:
            _open_path(ensure_env_file(self.data_dir))
        except OSError as exc:
            self.messagebox.showerror("打开配置失败", str(exc), parent=self.root)

    def open_data(self):
        try:
            _open_path(self.data_dir)
        except OSError as exc:
            self.messagebox.showerror("打开目录失败", str(exc), parent=self.root)

    def open_logs(self):
        try:
            _open_path(self.data_dir / "logs")
        except OSError as exc:
            self.messagebox.showerror("打开日志失败", str(exc), parent=self.root)

    def request_close(self):
        if self.stopping:
            return
        self.stopping = True
        self.status.set("正在停止服务并保存用量记录……")
        self.open_button.configure(state="disabled")
        self.close_button.configure(state="disabled")
        threading.Thread(target=self._shutdown, daemon=True, name="shutdown").start()

    def _shutdown(self):
        try:
            self.app_module.request_shutdown()
            self.app_module.flush_usage()
            grace = max(0.0, float(getattr(self.app_module, "SHUTDOWN_GRACE_SECONDS", 5.0)))
            if grace:
                time.sleep(grace)
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            self.app_module.logger.warning("桌面启动器停止服务时出现异常: %s", exc)
        finally:
            remove_runtime_info(self.runtime_path, os.getpid())
            release_single_instance()
            self.root.after(0, self.root.destroy)


def _create_server(app_module, host: str, preferred_port: int):
    """绑定 WSGI 服务；探测端口后仍捕获绑定竞态并继续尝试。"""
    from werkzeug.serving import make_server

    for offset in range(100):
        port = preferred_port + offset
        if port > 65535:
            break
        if not port_is_available(host, port):
            continue
        try:
            server = make_server(host, port, app_module.app, threaded=True)
            return server, port
        except (OSError, SystemExit) as exc:
            # Werkzeug 把绑定失败转换为 sys.exit(1)，必须处理探测后被抢占的竞态。
            # 其他退出码不代表端口绑定错误，不应被当作重试吞掉。
            if isinstance(exc, SystemExit) and exc.code != 1:
                raise
            continue
    raise OSError(f"无法绑定端口 {preferred_port} 附近的本机服务")


def _run_desktop() -> int:
    """启动 Flask 服务和桌面管理窗口。"""
    # 桌面版只服务本机；需要局域网/公网部署时继续使用 python app.py，并按 README
    # 配置 ACCESS_PASSWORD、ALLOWED_ORIGINS 与 HTTPS。这样安装包不会因为某个旧的
    # FLASK_HOST 环境变量而意外把聊天记录暴露到局域网。
    os.environ["FLASK_HOST"] = "127.0.0.1"
    _prepare_source_import_path()
    config = _load_config_module()
    data_dir = Path(config.DATA_DIR)
    ensure_env_file(data_dir)

    if not acquire_single_instance(data_dir):
        if _show_existing_instance(data_dir):
            return 0
        try:
            import tkinter.messagebox as messagebox

            messagebox.showwarning("程序已在运行", "已有一个 QQ 聊天记录分析工具正在启动，请稍后再试。")
        except Exception:
            pass
        return 0

    server = None
    try:
        app_module = _load_app_module()
        host = app_module.FLASK_HOST
        server, port = _create_server(app_module, host, int(app_module.FLASK_PORT))
        app_module.FLASK_PORT = port
        if not app_module._startup_report():
            raise RuntimeError("当前配置拒绝启动，请检查 FLASK_HOST、ACCESS_PASSWORD 和 .env。")

        write_runtime_info(runtime_file(data_dir), host, port)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="qqchatlog-server")
        server_thread.start()

        import tkinter as tk
        from tkinter import messagebox, ttk

        root = tk.Tk()
        DesktopController(root, tk, ttk, messagebox, server, server_thread, app_module, data_dir, host, port)
        root.mainloop()
        return 0
    except Exception as exc:  # noqa: BLE001
        if server is not None:
            try:
                server.server_close()
            except OSError:
                pass
        remove_runtime_info(runtime_file(data_dir), os.getpid())
        release_single_instance()
        try:
            import tkinter.messagebox as messagebox

            messagebox.showerror("启动失败", f"{exc}\n\n请查看数据目录下的 logs\\app.log。")
        except Exception:
            stream = getattr(sys, "stderr", None)
            if stream is not None:
                print(f"启动失败: {exc}", file=stream)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    """桌面入口；保留 --version 方便安装包和 CI 自检。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        app_module = _load_app_module()
        try:
            return app_module.main(["--version"])
        except SystemExit as exc:
            return int(exc.code or 0)
    if args:
        raise SystemExit(f"不支持的参数: {' '.join(args)}")
    return _run_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
