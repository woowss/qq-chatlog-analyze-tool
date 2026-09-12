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
"""SRI 复核：本地 vendor 文件 / jsDelivr 实际字节 / _report_assets.html 内联常量，三者对齐

tests/test_sri.py 只能证明"内联常量 == 本地文件"；"本地文件 == CDN 实际提供的字节"
这件事必须联网才证得了，所以单独放这里（升级 vendor 文件后跑一次）：

    python tools/verify_vendor_sri.py            # 联网：本地 <-> CDN <-> 内联常量，逐条核对
    python tools/verify_vendor_sri.py --offline  # 不联网：只核对 本地 <-> 内联常量
    python tools/verify_vendor_sri.py --print    # 不联网：按本地文件打印可粘贴的 VENDOR_CDN 表

退出码 0 = 一致，1 = 有出入。脚本只读不写：哈希该不该改由人决定——自动改写
等于把 CDN 上的变化当成"正常"咽下去，那正是 SRI 要防的事。
"""

import argparse
import base64
import hashlib
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
#: VENDOR_CDN 表与导出代码所在的模板。2026-09-12 起它们被抽到 partial：
#: 私聊报告与群聊报告共用同一份导出机制（两处各存一份必然会漏改一处，而 SRI 对不上时
#: 浏览器是静默拦掉资源）。校验对象因此指向 partial 本身。
REPORT_HTML = REPO_ROOT / "web" / "templates" / "_report_assets.html"
BASE_HTML = REPO_ROOT / "web" / "templates" / "base.html"
VENDOR_DIR = REPO_ROOT / "web" / "static" / "vendor"
TIMEOUT_SECONDS = 30

# VENDOR_CDN 里的单条：'名字': { url: '...', integrity: '...' }
_ENTRY_RE = re.compile(
    r"'(?P<name>[^']+)'\s*:\s*\{\s*url:\s*'(?P<url>[^']+)'\s*,"
    r"\s*integrity:\s*'(?P<integrity>[^']+)'"
)
_VENDOR_REF_RE = re.compile(r"filename='vendor/([^']+)'")


class SriTableError(RuntimeError):
    """VENDOR_CDN 表没找到或形状变了——宁可炸，也不要静默跳过校验"""


def sha384(data: bytes) -> str:
    """SRI 字符串（sha384 是 SRI 规范推荐的强哈希，浏览器普遍支持）"""
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


def sri_of_file(path: Path) -> str:
    return sha384(path.read_bytes())


def load_vendor_table(report_html: Path = REPORT_HTML) -> dict:
    """抽出模板里的 VENDOR_CDN：{文件名: {'url': ..., 'integrity': ...}}（按名字排序）"""
    text = report_html.read_text(encoding="utf-8")
    start = text.find("var VENDOR_CDN")
    if start < 0:
        raise SriTableError(f"{report_html} 里找不到 VENDOR_CDN")
    end = text.find("};", start)
    if end < 0:
        raise SriTableError(f"{report_html} 里 VENDOR_CDN 没有闭合的 }};")
    table = {}
    for match in _ENTRY_RE.finditer(text[start:end]):
        table[match.group("name")] = {
            "url": match.group("url"),
            "integrity": match.group("integrity"),
        }
    if not table:
        raise SriTableError(f"{report_html} 里 VENDOR_CDN 解析出 0 条——模板结构变了，请同步本脚本")
    return table


def app_vendor_files(base_html: Path = BASE_HTML) -> list:
    """base.html 实际加载的 vendor 文件（应用内用本地文件，只在导出时换 CDN）"""
    return sorted(set(_VENDOR_REF_RE.findall(base_html.read_text(encoding="utf-8"))))


def coverage_issues(table: dict, expected: list) -> list:
    """两边必须一一对应：漏一条 SRI，分享版就会去本机 /static 拿资源（必然裂）"""
    issues = []
    for name in expected:
        if name not in table:
            issues.append(f"{name}: base.html 加载了它，但 VENDOR_CDN 里没有 SRI 条目")
    for name in table:
        if name not in expected:
            issues.append(f"{name}: VENDOR_CDN 里有条目，但 base.html 没加载它（多余/改名？）")
        if not (VENDOR_DIR / name).is_file():
            issues.append(f"{name}: 本地文件不存在（{VENDOR_DIR / name}）")
    return issues


def url_issues(table: dict) -> list:
    issues = []
    for name, entry in table.items():
        url = entry["url"]
        if not url.startswith("https://cdn.jsdelivr.net/npm/"):
            issues.append(f"{name}: 不是 jsDelivr 的 https 地址：{url}")
        if not url.endswith("/" + name):
            issues.append(f"{name}: URL 末尾的文件名对不上：{url}")
        package = url.split("/npm/", 1)[-1]
        if "@" not in package:
            issues.append(f"{name}: URL 没锁版本（缺 @版本号）：{url}")
    return issues


def local_issues(table: dict) -> list:
    issues = []
    for name, entry in table.items():
        path = VENDOR_DIR / name
        if not path.is_file():
            continue  # 缺失已由 coverage_issues 报过，别重复刷屏
        actual = sri_of_file(path)
        if actual != entry["integrity"]:
            issues.append(f"{name}: 内联常量 {entry['integrity']} 与本地文件 {actual} 不一致")
    return issues


def fetch(url: str):
    """返回 (字节, 小写化的响应头)；SRI 校验的是解码后的实体字节"""
    request = urllib.request.Request(url, headers={"User-Agent": "qqchatlog-sri-check/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read(), {k.lower(): v for k, v in resp.headers.items()}


def cdn_issues(table: dict) -> list:
    issues = []
    for name, entry in table.items():
        path = VENDOR_DIR / name
        try:
            data, headers = fetch(entry["url"])
        except (urllib.error.URLError, OSError) as exc:
            issues.append(f"{name}: 下载失败 {exc}（离线环境请用 --offline）")
            continue
        cdn_sri = sha384(data)
        if cdn_sri != entry["integrity"]:
            issues.append(f"{name}: CDN 字节的哈希 {cdn_sri} 与内联常量不一致（CDN 上的文件变了？）")
        if path.is_file():
            local = path.read_bytes()
            if data != local:
                issues.append(f"{name}: CDN 字节与本地文件不一致（本地 {len(local)}B / CDN {len(data)}B）")
        # 跨源 SRI 要求资源带 CORS 头，否则浏览器会以"跨源校验失败"为由直接拦掉
        allow_origin = headers.get("access-control-allow-origin", "")
        if not allow_origin:
            issues.append(f"{name}: CDN 未返回 Access-Control-Allow-Origin，跨源 SRI 会被浏览器拦掉")
        print(f"  ok  {name:26s} {len(data):>9}B  ACAO={allow_origin or '缺失'}")
    return issues


def render_table(table: dict) -> str:
    """按本地文件重算哈希，打印可直接粘回 _report_assets.html 的 VENDOR_CDN 表"""
    lines = ["var VENDOR_CDN = {"]
    items = list(table.items())
    for index, (name, entry) in enumerate(items):
        path = VENDOR_DIR / name
        integrity = sri_of_file(path) if path.is_file() else entry["integrity"]
        comma = "," if index < len(items) - 1 else ""
        lines.append(f"    '{name}': {{")
        lines.append(f"        url: '{entry['url']}',")
        lines.append(f"        integrity: '{integrity}'")
        lines.append(f"    }}{comma}")
    lines.append("};")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="核对报告导出用的 vendor SRI 哈希")
    parser.add_argument("--offline", action="store_true", help="不联网：只比对本地文件与内联常量")
    parser.add_argument(
        "--print", dest="print_table", action="store_true", help="不联网：按本地文件打印 VENDOR_CDN 表"
    )
    args = parser.parse_args(argv)

    try:
        table = load_vendor_table()
    except SriTableError as exc:
        print(f"[FAIL] {exc}")
        return 1
    expected = app_vendor_files()

    if args.print_table:
        print(render_table(table))
        print(f"# 共 {len(table)} 条；本地文件缺失的条目保留原常量（不凭空造哈希）", file=sys.stderr)
        return 0

    issues = coverage_issues(table, expected) + url_issues(table) + local_issues(table)
    if issues:
        for item in issues:
            print(f"[FAIL] {item}")
        return 1
    print(f"[OK] {len(table)} 个本地 vendor 文件与内联 SRI 常量一致（base.html 引用也已对齐）")

    if args.offline:
        print("（--offline：未联网；CDN 字节由 tools/verify_vendor_sri.py 联网模式负责复核）")
        return 0

    print(f"联网核对 cdn.jsdelivr.net 实际字节（{len(table)} 个文件）…")
    issues = cdn_issues(table)
    if issues:
        for item in issues:
            print(f"[FAIL] {item}")
        return 1
    print("[OK] CDN 字节、本地文件、内联常量三者一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
