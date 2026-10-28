"""ICC protocol shortcuts matching the pinned upstream MW_UriHandler.cs."""
from __future__ import annotations

import os
import re
import time
from urllib.parse import quote, unquote, urlencode, urlsplit
import winreg

# One canonical URI per action; aliases must not split the recent-use history.
COMMANDS = {
    "fold": ("收纳工具栏", "工具栏"),
    "unfold": ("展开工具栏", "工具栏"),
    "toggle": ("切换收纳", "工具栏"),
    "thoroughhideon": ("启用彻底隐藏", "工具栏"),
    "thoroughhideoff": ("关闭彻底隐藏", "工具栏"),
    "thoroughhidetoggle": ("切换彻底隐藏", "工具栏"),
    "randone": ("单人点名", "课堂工具"),
    "rand": ("点名", "课堂工具"),
    "timer": ("计时器", "课堂工具"),
    "whiteboard": ("切换白板", "白板"),
    "booth": ("视频展台", "课堂工具"),
    "videopresenter": ("内置展台全屏", "课堂工具"),
    "annotate": ("批注", "画笔"),
    "tool/pen": ("画笔", "画笔"),
    "tool/cursor": ("鼠标", "画笔"),
    "tool/eraser": ("橡皮擦", "画笔"),
    "tool/eraserbystrokes": ("笔画橡皮", "画笔"),
    "tool/select": ("套索选择", "画笔"),
    "undo": ("撤销", "墨迹"),
    "redo": ("重做", "墨迹"),
    "clear": ("清空墨迹", "墨迹"),
    "clearall": ("清空墨迹及历史", "墨迹"),
    "previouspage": ("上一页", "白板"),
    "nextpage": ("下一页", "白板"),
    "newpage": ("新建白板页", "白板"),
    "deletepage": ("删除白板页", "白板"),
    "screenshot": ("截图并插入", "白板"),
    "freeze": ("冻结墨迹", "冻结"),
    "unfreeze": ("解冻墨迹", "冻结"),
    "freeze/start": ("开始课程冻结", "冻结"),
    "freeze/end": ("结束课程冻结", "冻结"),
    "freeze/cancel": ("取消课程冻结", "冻结"),
    "tool/state": ("输出当前工具状态", "查询"),
    "config-profile/list": ("输出配置方案列表", "查询"),
    "settings": ("打开 ICC 设置", "设置"),
    "restart": ("重启 ICC", "程序"),
    "restart/admin": ("以管理员身份重启", "程序"),
    "restart/normal": ("以普通权限重启", "程序"),
    "exit": ("退出 ICC", "程序"),
}
ALIASES = {
    "show": "unfold", "board": "whiteboard", "annotation": "annotate", "quit": "exit",
    "clearink": "clear", "clearinkandhistory": "clearall", "page/next": "nextpage",
    "prevpage": "previouspage", "page/previous": "previouspage", "page/add": "newpage", "page/delete": "deletepage",
    "tool/color": "tool/pen", "tool/eraserstroke": "tool/eraserbystrokes", "tool/lasso": "tool/select",
    **{alias + suffix: "freeze" + suffix for alias in ("lock", "ink-freeze", "ink/lock") for suffix in ("", "/start", "/end", "/cancel")},
    **{alias: "unfreeze" for alias in ("unlock", "ink-unfreeze", "ink/unlock")},
}
CONFIRMATIONS = {
    "clear": "清空当前页墨迹？",
    "clearall": "清空当前页墨迹及历史？这可能无法撤销。",
    "deletepage": "删除当前白板页？",
    "unfreeze": "按 ICC URI 协议解冻墨迹？该协议会跳过 ICC 的解冻验证。",
    "freeze/end": "结束课程冻结并解冻？",
    "freeze/cancel": "取消课程冻结？",
    "restart": "重启 ICC？",
    "restart/admin": "以管理员身份重启 ICC？电脑上可能出现 UAC 提示。",
    "restart/normal": "以普通权限重启 ICC？",
    "exit": "退出 ICC？",
    "config-profile/switch": "切换 ICC 配置方案？",
}


def _decoded(value):
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("ICC URI 的百分号编码无效。")
    try:
        text = unquote(value, errors="strict")
        text.encode("utf-8")
    except (UnicodeError, ValueError):
        raise ValueError("ICC URI 编码无效。") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError("ICC URI 不允许控制字符。")
    return text


def shortcut(uri):
    if not isinstance(uri, str) or not uri or len(uri) > 2048 or uri != uri.strip() or "\\" in uri:
        raise ValueError("请输入不超过 2048 字符的 icc:// URI。")
    _decoded(uri)  # Check controls before urlsplit can silently strip them.
    if not uri.lower().startswith("icc://"):
        raise ValueError("只允许 icc:// 协议。")
    try:
        parsed = urlsplit(uri)
    except ValueError:
        raise ValueError("ICC URI 格式无效。") from None
    if not parsed.netloc or any(character in parsed.netloc for character in "@:[]") or parsed.fragment:
        raise ValueError("ICC URI 不允许用户信息、端口或片段。")
    path = _decoded(parsed.netloc + parsed.path).strip("/")
    if "//" in path or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("ICC URI 路径无效。")
    command = ALIASES.get(path.lower(), path.lower())
    plugin = command.startswith("plugin/")
    query: dict[str, str] = {}
    for pair in parsed.query.split("&") if parsed.query else []:
        key, equal, value = pair.partition("=")
        key = _decoded(key) if plugin else _decoded(key.strip()).lower()
        if not equal or not key or key in query:
            raise ValueError("ICC URI 参数缺值或重复。")
        # ICC uses Uri.UnescapeDataString, not form encoding: '+' is a literal plus.
        query[key] = _decoded(value if plugin else value.strip())
    allowed = set()
    if command in {"freeze", "unfreeze", "freeze/start", "freeze/end"}:
        allowed = {"page"}
        if "page" in query:
            query["page"] = query["page"].strip()
            if not re.fullmatch(r"[0-9]{1,3}", query["page"]) or int(query["page"]) > 100:
                raise ValueError("ICC page 参数必须为 0–100 的整数；省略时使用 ICC 当前页。")
            query["page"] = str(int(query["page"]))
    if command == "config-profile/switch":
        allowed = {"name"}
        query["name"] = query.get("name", "").strip()
        if not query.get("name") or len(query["name"]) > 200:
            raise ValueError("切换配置方案需要 1–200 字符的 name 参数。")
        name, group = "配置方案：" + query["name"], "设置"
    elif command == "settings" or command.startswith("settings/"):
        allowed = {"key"}
        page = path.split("/", 1)[1] if "/" in path else ""
        if page and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", page):
            raise ValueError("ICC 设置页名称无效。")
        command = "settings" + ("/" + page if page else "")
        name, group = "ICC 设置" + ("：" + page if page else ""), "设置"
    elif command.startswith("plugin/"):
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in command.split("/")[1:]):
            raise ValueError("ICC 插件路径无效。")
        allowed = set(query)
        name, group = "插件：" + path.split("/", 1)[1], "插件"
    elif command in COMMANDS:
        name, group = COMMANDS[command]
    else:
        raise ValueError("该 ICC 命令不在所参考处理器支持的范围内。")
    if set(query) - allowed:
        raise ValueError("此 ICC 命令不支持指定的查询参数。")
    if not plugin and "page" in query:
        name += " · page=" + query["page"]
    if command.startswith("settings") and query.get("key"):
        name += " · " + query["key"]
    canonical = "icc://" + command
    if query:
        canonical += "?" + urlencode(query if plugin else sorted(query.items()), quote_via=quote)
    if len(canonical) > 2048:
        raise ValueError("编码后的 ICC URI 超过 2048 字符。")
    confirmation = CONFIRMATIONS.get(command)
    if command.startswith("plugin/"):
        confirmation = "将此 URI 派发给 ICC 插件？具体操作由该插件决定。"
    return {"uri": canonical, "name": name, "group": group, "confirmation": confirmation}


def registered():
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "icc") as key:
            if winreg.QueryValueEx(key, "URL Protocol")[1] != winreg.REG_SZ:
                return False
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"icc\shell\open\command") as key:
            command, kind = winreg.QueryValueEx(key, "")
            return kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and bool(command.strip())
    except (OSError, AttributeError):
        return False


class IccController:
    def __init__(self, store):
        self.store = store

    def state(self):
        recent = self.store.snapshot().icc_recent
        entries = {"icc://" + command: {**shortcut("icc://" + command), "last_used": 0} for command in COMMANDS}
        for uri, used_at in recent.items():
            try:
                item = shortcut(uri)
            except ValueError:
                continue
            canonical = item["uri"]
            item["last_used"] = max(used_at, entries.get(canonical, {}).get("last_used", 0))
            entries[canonical] = item
        available = registered()
        return {"registered": available,
                "message": "ICC 协议已注册；请在 ICC 中启用 URI 协议。" if available else "未注册 ICC 协议，请在受控电脑安装 ICC 并启用 URI 协议。",
                "items": sorted(entries.values(), key=lambda item: -item["last_used"])}

    def launch(self, uri):
        item = shortcut(uri)
        with self.store.lock:
            if not registered():
                raise RuntimeError("未注册 ICC 协议，请在受控电脑安装 ICC 并启用 URI 协议。")
            try:
                os.startfile(item["uri"], "open")
            except OSError as error:
                raise RuntimeError(f"ICC 协议未能派发，请检查协议注册及 ICC 安装：{error}") from None
            recent = dict(self.store.config.icc_recent)
            # Strict ordering survives concurrent clicks, equal milliseconds and clock rollback.
            recent[item["uri"]] = max(time.time_ns() // 1_000_000, max(recent.values(), default=0) + 1)
            recent = dict(sorted(recent.items(), key=lambda pair: -pair[1])[:128])
            try:
                self.store.update(icc_recent=recent)
            except OSError:
                raise RuntimeError("Windows 已接受 ICC URI，但最近使用记录保存失败。请勿直接重试可能改变数据的操作。") from None
        state = self.state()
        notice = "已提交给 Windows，ICC 不提供执行确认。"
        if item["uri"] == "icc://tool/state":
            notice += "ICC 会将工具状态写入其临时目录的 InkCanvasToolState.txt。"
        elif item["uri"] == "icc://config-profile/list":
            notice += "ICC 会将方案列表写入其临时目录的 InkCanvasConfigProfileList.json。"
        return {**state, "dispatched_uri": item["uri"], "notice": notice}
