# -*- coding: utf-8 -*-
"""
Git 分支清理工具

单文件 Tkinter 应用，用于在一个本地 Git 仓库里对比「本地分支 / 远程分支」，
按最后提交时间、远程有无、是否已并入主分支筛选，然后带保护地批量删除。

依赖：仅 Python 标准库 + git CLI。
"""

import calendar
import csv
import fnmatch
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
except ImportError:
    # pythonw 下没有控制台，只能借助系统弹窗告知。
    # 本机 PATH 上的 pythonw 可能是不带 tkinter 的精简版，属于常见坑。
    _MSG = (
        "当前 Python 解释器不包含 tkinter，无法启动图形界面。\n\n"
        "请双击本目录下的「启动.vbs」启动，或用完整路径的解释器：\n"
        r"%LOCALAPPDATA%\Programs\Python\Python314-32\pythonw.exe git_branch_manager.pyw"
    )
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, _MSG, "Git 分支清理工具", 0x10)
    except Exception:
        pass
    sys.exit(1)


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

APP_NAME = "Git 分支清理工具"
APP_VERSION = "1.7.0"          # 新增功能时递增次版本号，修 bug/样式调整递增修订号
APP_AUTHOR = "283365011@qq.com"
APP_REPO = "https://github.com/carbin1930/git-branch-manager"
APP_LICENSE = "Apache License 2.0"

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：__file__ 指向临时解压目录，配置/日志必须落在 exe 同级目录
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "branch-delete.log")
ERROR_LOG = os.path.join(APP_DIR, "error.log")

GIT_TIMEOUT = 30
PUSH_TIMEOUT = 90
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 删除留痕与还原
LOG_SEP = "\t"                       # 分支名可能含 |，用 tab 分隔才不会串列
BACKUP_PREFIX = "refs/gbm-backup/"   # 删除时保留的备份 ref，防止对象被 gc 回收

UI_FONT = ("Microsoft YaHei UI", 9)
UI_FONT_SMALL = ("Microsoft YaHei UI", 8)
MONO_FONT = ("Consolas", 9)

TIME_CHOICES = [
    "全部",
    "超 3 个月未提交",
    "超 6 个月未提交",
    "超 1 年未提交",
    "超 2 年未提交",
    "自定义",
]
TIME_TO_MONTHS = {
    "超 3 个月未提交": 3,
    "超 6 个月未提交": 6,
    "超 1 年未提交": 12,
    "超 2 年未提交": 24,
}

MERGE_CHOICES = ["不限", "只看已并入", "只看未并入"]

DEFAULT_CONFIG = {
    "ui_version": 3,          # 布局变更时递增，用来丢弃过期列宽
    "last_repo": "",
    "recent_repos": [],
    "main_branches": [],
    "time_filter": "全部",
    "from_date": "",
    "to_date": "",
    "protect_patterns": [],
    "allow_remote_delete": False,
    "sync_scroll": False,
    "merge_filter": "不限",
    "only_remote_missing": False,
    "only_local_exists": False,
    "filter_local": True,
    "filter_remote": True,
    "export_dir": "",
    "column_widths": {},
}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    data = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULT_CONFIG:
                if k not in data:
                    continue
                default = DEFAULT_CONFIG[k]
                if isinstance(default, list) and isinstance(data[k], list):
                    cfg[k] = data[k]
                elif isinstance(data[k], type(default)):
                    cfg[k] = data[k]
    except Exception:
        data = {}
    # 布局改版后旧列宽不再适用，直接丢弃，避免把窗口撑爆
    if data.get("ui_version") != DEFAULT_CONFIG["ui_version"]:
        cfg["column_widths"] = {}
        cfg["ui_version"] = DEFAULT_CONFIG["ui_version"]
    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Git 调用层
# --------------------------------------------------------------------------

class GitError(Exception):
    pass


class Repo:
    """一个 Git 仓库的只读/写入操作封装，所有命令以参数数组传参，不经 shell。"""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.root = None
        self.is_bare = False

    # -- 基础 --------------------------------------------------------------
    def run(self, args, timeout=GIT_TIMEOUT, input_text=None):
        cmd = ["git", "-c", "core.quotepath=false", "-c", "color.ui=false"] + list(args)
        # 注意：不能用 subprocess 的 input= 传文本 —— Windows 上会把 \n 变成 \r\n，
        # git update-ref --stdin 会报 "expected SP but got: ?"。走临时文件最稳。
        tmp_path = None
        stdin_src = subprocess.DEVNULL
        if input_text is not None:
            fd, tmp_path = tempfile.mkstemp(prefix="gbm_stdin_", suffix=".txt")
            with os.fdopen(fd, "wb") as f:
                f.write(input_text.encode("utf-8"))
            stdin_src = open(tmp_path, "rb")
        try:
            p = subprocess.run(
                cmd,
                cwd=self.path,
                stdin=stdin_src,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        except FileNotFoundError:
            raise GitError("找不到 git 命令：请确认已安装 Git 并加入系统 PATH。")
        except subprocess.TimeoutExpired:
            raise GitError("git 命令超时（%ds）：git %s" % (timeout, " ".join(args)))
        except OSError as e:
            raise GitError("无法执行 git：%s" % e)
        finally:
            if tmp_path:
                try:
                    stdin_src.close()
                except Exception:
                    pass
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        return p.returncode, p.stdout, p.stderr

    def open(self):
        """校验并定位仓库根目录，支持传入子目录。"""
        if not os.path.isdir(self.path):
            raise GitError("路径不存在：%s" % self.path)
        code, out, err = self.run(["rev-parse", "--show-toplevel"])
        if code != 0:
            # 可能是 bare 仓库（没有工作区）
            code2, out2, err2 = self.run(["rev-parse", "--is-bare-repository", "--absolute-git-dir"])
            if code2 == 0 and out2.strip().splitlines()[0].strip() == "true":
                self.is_bare = True
                self.root = self.path
                return self.root
            raise GitError("不是 Git 仓库：%s" % self.path)
        self.root = out.strip().splitlines()[0].strip()
        self.path = self.root
        code2, out2, _ = self.run(["rev-parse", "--is-bare-repository"])
        self.is_bare = (code2 == 0 and out2.strip() == "true")
        return self.root

    # -- 采集 --------------------------------------------------------------
    def current_branch(self):
        code, out, _ = self.run(["symbolic-ref", "--short", "-q", "HEAD"])
        if code != 0:
            return ""
        return out.strip()

    def remotes(self):
        code, out, _ = self.run(["remote"])
        if code != 0:
            return []
        return [x.strip() for x in out.splitlines() if x.strip()]

    def branches(self):
        """一次 for-each-ref 采集本地与远程分支的尖端信息。"""
        fmt = "%(refname)%00%(objectname)%00%(committerdate:unix)%00%(committerdate:iso8601)"
        code, out, err = self.run(["for-each-ref", "--format=" + fmt, "refs/heads", "refs/remotes"])
        if code != 0:
            raise GitError("读取分支失败：%s" % err.strip())

        locals_, remotes_ = [], []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x00")
            if len(parts) < 4:
                continue
            refname, sha, ts_raw, iso = parts[0], parts[1], parts[2], parts[3]
            ts = _parse_unix(ts_raw, iso)

            if refname.startswith("refs/heads/"):
                name = refname[len("refs/heads/"):]
                locals_.append(BranchInfo(
                    full=refname, short=name, name=name, sha=sha, commit_ts=ts, iso=iso))
            elif refname.startswith("refs/remotes/"):
                rest = refname[len("refs/remotes/"):]
                # 排除 origin/HEAD 这类符号引用，以及 refs/remotes/origin 这种顶层残留
                if "/" not in rest or rest.endswith("/HEAD"):
                    continue
                remote, name = rest.split("/", 1)
                if not name or name == "HEAD":
                    continue
                remotes_.append(BranchInfo(
                    full=refname, short=rest, name=name, sha=sha, commit_ts=ts,
                    iso=iso, remote=remote))
        return locals_, remotes_

    def merged_refs(self, main_name):
        """返回已并入指定主分支的 refname 集合。"""
        code, out, err = self.run([
            "for-each-ref", "--format=%(refname)", "--merged=" + main_name,
            "refs/heads", "refs/remotes",
        ])
        if code != 0:
            raise GitError("判定并入关系失败（%s）：%s" % (main_name, err.strip()))
        return {line.strip() for line in out.splitlines() if line.strip()}

    def branch_exists(self, name):
        code, _, _ = self.run(["show-ref", "--verify", "--quiet", "refs/heads/" + name])
        return code == 0

    def has_commit(self, sha):
        """对象是否还在本地（没有被 gc 回收）。"""
        code, _, _ = self.run(["cat-file", "-e", sha + "^{commit}"], timeout=15)
        return code == 0

    def worktree_branches(self):
        """返回 {分支名: worktree 路径}，含主工作区。用于提前识别被占用的分支。"""
        code, out, _ = self.run(["worktree", "list", "--porcelain"])
        if code != 0:
            return {}
        res = {}
        path = ""
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("branch refs/heads/"):
                res[line[len("branch refs/heads/"):].strip()] = path
        return res

    def local_branch_names(self):
        code, out, _ = self.run(["for-each-ref", "--format=%(refname:short)", "refs/heads"])
        if code != 0:
            return set()
        return {x.strip() for x in out.splitlines() if x.strip()}

    def fetch_prune(self):
        code, out, err = self.run(["fetch", "--prune", "--all"], timeout=120)
        return code, (out + err).strip()


def _parse_unix(raw, iso):
    try:
        return int(raw.strip())
    except Exception:
        pass
    try:
        # 形如 2026-11-06 09:32:00 +0800
        return int(datetime.strptime(iso.strip()[:19], "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return 0


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class BranchInfo:
    full: str
    short: str
    name: str
    sha: str
    commit_ts: int
    iso: str
    remote: str = ""
    is_main: bool = False
    is_current: bool = False
    has_remote: bool = False     # 本地分支：远程是否存在同名
    has_local: bool = False      # 远程分支：本地是否存在同名
    merged_into: list = field(default_factory=list)
    merged_known: bool = True    # 是否设置了主分支（False 时并入列显示 —）


def fmt_date(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def days_ago(ts):
    if not ts:
        return 99999
    return int((datetime.now() - datetime.fromtimestamp(ts)).total_seconds() // 86400)


def fmt_ago(ts):
    d = days_ago(ts)
    if d <= 0:
        return "今天"
    if d < 30:
        return "%d 天" % d
    if d < 365:
        return "%d 个月" % (d // 30)
    years = d / 365.0
    return "%.1f 年" % years


def minus_months(dt, months):
    y = dt.year + (dt.month - 1 - months) // 12
    m = (dt.month - 1 - months) % 12 + 1
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    for f in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, f)
        except ValueError:
            continue
    return None


def format_branch_reasons(pairs, limit=10, indent="   "):
    """[(分支名, 原因)] -> 每行一条，避免长串顿号挤成一段。"""
    lines = []
    for name, reason in pairs[:limit]:
        lines.append("%s%s  ——  %s" % (indent, name, reason))
    if len(pairs) > limit:
        lines.append("%s… 另有 %d 个" % (indent, len(pairs) - limit))
    return "\n".join(lines)


def merged_label(b):
    if b.is_main:
        return "主分支"
    if not b.merged_known:
        return "—"
    if not b.merged_into:
        return "否"
    return "是(%s)" % "/".join(b.merged_into)


def write_log(repo_path, branch_name, sha, merged_text, action="DELETE"):
    """追加一条操作留痕。tab 分隔，分支名含 | 也不会错列。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = LOG_SEP.join([stamp, repo_path, branch_name, sha, merged_text, action]) + "\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)


def write_delete_log(repo_path, branch_name, sha, merged_text):
    write_log(repo_path, branch_name, sha, merged_text, "DELETE")


def parse_log_line(line):
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    parts = line.split(LOG_SEP)
    if len(parts) < 5:
        # 兼容早期用 " | " 分隔的 5 段格式
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            return None
    return {
        "time": parts[0],
        "repo": parts[1],
        "branch": parts[2],
        "sha": parts[3],
        "merged": parts[4],
        "action": (parts[5] if len(parts) > 5 else "DELETE").strip() or "DELETE",
    }


def load_log_records():
    recs = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                r = parse_log_line(line)
                if r:
                    recs.append(r)
    except Exception:
        pass
    return recs


def same_repo(a, b):
    """判断两条记录是否属于同一个仓库。兼容斜杠、大小写、结尾分隔符差异。"""
    if not a or not b:
        return False
    na = os.path.normcase(os.path.normpath(a))
    nb = os.path.normcase(os.path.normpath(b))
    if na == nb:
        return True
    # 再比一次真实路径：兼容符号链接、SUBST 虚拟盘符、8.3 短文件名
    try:
        return os.path.normcase(os.path.realpath(na)) == os.path.normcase(os.path.realpath(nb))
    except Exception:
        return False


def backup_ref_path(branch_name, stamp=None):
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    return "%s%s/%s" % (BACKUP_PREFIX, stamp, branch_name)


def make_backup_ref(repo, branch_name, sha):
    """删除前把分支尖端挂到一个备份 ref 上，避免对象被 git gc 回收。"""
    ref = backup_ref_path(branch_name)
    try:
        code, _out, _err = repo.run(["update-ref", ref, sha])
    except Exception:
        return ""
    return ref if code == 0 else ""


def drop_backup_ref(repo, ref):
    if not ref:
        return
    try:
        repo.run(["update-ref", "-d", ref])
    except Exception:
        pass


def list_backup_refs(repo):
    """返回 {分支名: [(ref, sha), ...]}，按 ref 名升序（即时间先后）。"""
    out_map = {}
    try:
        code, out, _err = repo.run(["for-each-ref", "--format=%(refname)%00%(objectname)", BACKUP_PREFIX])
    except Exception:
        return out_map
    if code != 0:
        return out_map
    for line in out.splitlines():
        if "\x00" not in line:
            continue
        ref, _sep, sha = line.partition("\x00")
        rest = ref[len(BACKUP_PREFIX):] if ref.startswith(BACKUP_PREFIX) else ""
        if "/" not in rest:
            continue
        _stamp, _s, branch = rest.partition("/")
        if branch:
            out_map.setdefault(branch, []).append((ref, sha))
    return out_map


def commits_exist(repo, shas):
    """批量检查 commit 是否还在本地对象库（比逐条 cat-file 快一个量级）。"""
    shas = [s for s in dict.fromkeys(shas) if s]
    if not shas:
        return set()
    try:
        p = subprocess.run(
            ["git", "-c", "core.quotepath=false", "cat-file", "--batch-check"],
            cwd=repo.path,
            input="\n".join(shas) + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return set()
    alive = set()
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] != "missing":
            alive.add(parts[0])
    return alive


# --------------------------------------------------------------------------
# 删除确认框
# --------------------------------------------------------------------------

class ConfirmDialog(tk.Toplevel):
    def __init__(self, parent, title, rows, skipped, need_input=None, is_remote=False):
        super().__init__(parent)
        self.title(title)
        self.result = False
        self.transient(parent)
        self.resizable(True, True)
        self.geometry("740x500")
        self.minsize(560, 360)
        self._need_input = need_input
        self._is_remote = is_remote
        self._ack_var = tk.BooleanVar(value=False)
        self._input_var = tk.StringVar()

        self.configure(bg="#f5f5f5")
        pad = {"padx": 12, "pady": 6}

        unmerged = [b for b in rows if not b.merged_into and not b.is_main]
        self._unmerged_count = len(unmerged)
        # 远程分支里既未并入、本地又没有副本的：删掉就真的没了
        self._lost = [b for b in unmerged if is_remote and not b.has_local]

        head = tk.Frame(self, bg="#f5f5f5")
        head.pack(fill="x", **pad)
        tk.Label(head, text=title, font=("Microsoft YaHei UI", 11, "bold"),
                 bg="#f5f5f5", anchor="w").pack(fill="x")

        if is_remote:
            if unmerged:
                desc = ("共 %d 个远程分支将被删除，其中 %d 个未并入主分支。"
                        % (len(rows), len(unmerged)))
                if self._lost:
                    desc += ("\n其中 %d 个本地也没有同名分支 —— 删除后这些提交可能彻底丢失，"
                             "没有地方可以找回。" % len(self._lost))
                else:
                    desc += "\n这些分支本地都有同名副本，删除远程后仍可从本地重新推送。"
            else:
                desc = "共 %d 个远程分支将被删除，全部已并入主分支。" % len(rows)
            warn_color = "#b00020" if self._lost else ("#a05a00" if unmerged else "#1c7c3c")
        else:
            desc = ("共 %d 个本地分支将被删除，其中 %d 个未并入主分支。"
                    % (len(rows), len(unmerged)))
            if unmerged:
                desc += "\n删除后仍可通过「还原…」从备份点找回。"
            warn_color = "#a05a00" if unmerged else "#1c7c3c"

        tk.Label(head, text=desc,
                 fg=warn_color, bg="#f5f5f5",
                 font=UI_FONT, anchor="w", justify="left",
                 wraplength=680).pack(fill="x", pady=(4, 0))

        body = tk.Frame(self, bg="#f5f5f5")
        body.pack(fill="both", expand=True, padx=12, pady=4)
        cols = ("name", "date", "ago", "merged")
        tv = ttk.Treeview(body, columns=cols, show="headings", height=12)
        for c, w, t in (("name", 300, "分支名"), ("date", 150, "最后提交"),
                        ("ago", 90, "距今"), ("merged", 140, "已并入主分支")):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w", stretch=(c == "name"))
        sb = ttk.Scrollbar(body, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        tv.tag_configure("unmerged", foreground="#b00020")
        for b in sorted(rows, key=lambda x: x.commit_ts):
            tags = () if b.merged_into or b.is_main else ("unmerged",)
            label = merged_label(b)
            if is_remote and not b.merged_into and not b.is_main:
                label = "否 · 无本地副本" if not b.has_local else "否 · 本地有副本"
            tv.insert("", "end", values=(b.short, fmt_date(b.commit_ts), fmt_ago(b.commit_ts),
                                         label), tags=tags)

        if skipped:
            txt = ("已自动跳过（%d 个）：\n%s"
                   % (len(skipped), format_branch_reasons(skipped, limit=5)))
            tk.Label(self, text=txt, fg="#7a5c00", bg="#f5f5f5",
                     font=UI_FONT_SMALL, anchor="w", justify="left",
                     wraplength=690).pack(fill="x", padx=12)

        bottom = tk.Frame(self, bg="#f5f5f5")
        bottom.pack(fill="x", **pad)

        if unmerged:
            if is_remote:
                ack_text = "我已确认：删除未并入主分支的远程分支，相关提交可能彻底丢失、无法找回"
            else:
                ack_text = "我已确认：删除未并入主分支的分支（仍可从备份点还原）"
            tk.Checkbutton(bottom, text=ack_text,
                           variable=self._ack_var, bg="#f5f5f5", font=UI_FONT,
                           fg="#b00020", activebackground="#f5f5f5",
                           command=self._refresh_state).pack(anchor="w")

        if need_input:
            row = tk.Frame(bottom, bg="#f5f5f5")
            row.pack(anchor="w", pady=(6, 0))
            tk.Label(row, text="请输入 %s 以确认远程删除：" % need_input, bg="#f5f5f5",
                     font=UI_FONT).pack(side="left")
            e = tk.Entry(row, textvariable=self._input_var, width=16, font=MONO_FONT)
            e.pack(side="left", padx=6)
            self._input_var.trace_add("write", lambda *a: self._refresh_state())
            e.focus_set()

        btns = tk.Frame(bottom, bg="#f5f5f5")
        btns.pack(anchor="e", pady=(10, 0))
        self.ok_btn = ttk.Button(btns, text="确认删除", command=self._on_ok, width=14)
        self.ok_btn.pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="取消", command=self._on_cancel, width=10).pack(side="right")

        self._refresh_state()
        self.bind("<Escape>", lambda e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.grab_set()
        self.after(50, self._center, parent)

    def _center(self, parent):
        try:
            self.update_idletasks()
            # 按内容自适应高度：跳过的分支越多，提示区越高，避免把表格挤扁
            w = max(700, min(self.winfo_reqwidth(), 920))
            h = max(420, min(self.winfo_reqheight(), max(480, self.winfo_screenheight() - 160)))
            self.geometry("%dx%d" % (w, h))
            self.update_idletasks()
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
            self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        except Exception:
            pass

    def _refresh_state(self):
        ok = True
        if self._need_input and self._input_var.get().strip() != self._need_input:
            ok = False
        if self._has_unmerged() and not self._ack_var.get():
            ok = False
        self.ok_btn.state(["!disabled"] if ok else ["disabled"])

    def _has_unmerged(self):
        return self._unmerged_count > 0

    def _on_ok(self):
        self.result = True
        self.destroy()

    def _on_cancel(self):
        self.result = False
        self.destroy()


# --------------------------------------------------------------------------
# 还原对话框
# --------------------------------------------------------------------------

class RestoreDialog(tk.Toplevel):
    """从 branch-delete.log 挑出本仓库删过的分支，一键还原。

    能否还原只取决于 commit 对象是否还在本地；删除时自动建的
    refs/gbm-backup/* 备份点会保住这些对象，避免被 git gc 回收。
    """

    def __init__(self, parent, repo):
        super().__init__(parent)
        self.title("还原已删除的分支")
        self.parent_app = parent
        self.repo = repo
        self.checked = set()
        self.restored = 0
        self.transient(parent)
        self.geometry("790x520")
        self.minsize(640, 400)
        self.configure(bg="#f5f5f5")
        self._load()
        self._build()
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.after(50, self._center)

    def _center(self):
        try:
            self.update_idletasks()
            x = self.parent_app.winfo_rootx() + (self.parent_app.winfo_width() - self.winfo_width()) // 2
            y = self.parent_app.winfo_rooty() + (self.parent_app.winfo_height() - self.winfo_height()) // 3
            self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        except Exception:
            pass

    def _load(self):
        all_recs = load_log_records()
        recs = [r for r in all_recs
                if r.get("action", "DELETE") == "DELETE"
                and same_repo(r.get("repo", ""), self.repo.path)]
        self.total_recs = len(all_recs)
        self.matched_recs = len(recs)

        latest = {}
        for r in recs:
            latest[r["branch"]] = r        # 同名分支只保留最后一次删除
        rows = sorted(latest.values(), key=lambda r: r["time"], reverse=True)[:500]

        backups = list_backup_refs(self.repo)

        # 日志里没有、但仓库内还留着备份点的分支：换机器、日志被清掉后仍能还原
        logged = {r["branch"] for r in rows}
        extra = []
        for branch in sorted(backups):
            if branch in logged:
                continue
            ref, sha = backups[branch][-1]
            extra.append({
                "time": "（仅备份点）", "repo": self.repo.path, "branch": branch,
                "sha": sha, "merged": "-", "action": "DELETE", "_source": "backup",
            })
        rows.extend(extra)
        self.backup_only = len(extra)

        alive = commits_exist(self.repo, [r["sha"] for r in rows])
        existing = self.repo.local_branch_names()
        for r in rows:
            r["_backup"] = backups.get(r["branch"], [])
            if r["branch"] in existing:
                r["_state"] = "已存在"
            elif r["sha"] in alive:
                r["_state"] = "可还原"
            else:
                r["_state"] = "对象已回收"
        self.rows = rows

    def _build(self):
        pad = {"padx": 12, "pady": 6}

        head = tk.Frame(self, bg="#f5f5f5")
        head.pack(fill="x", **pad)
        tk.Label(head, text="还原已删除的分支", font=("Microsoft YaHei UI", 11, "bold"),
                 bg="#f5f5f5", anchor="w").pack(fill="x")
        desc = ("当前仓库：%s\n日志共 %d 条，其中本仓库 %d 条。"
                % (self.repo.path, self.total_recs, self.matched_recs))
        if self.backup_only:
            desc += ("另有 %d 个分支日志里没有记录，是从仓库内的 refs/gbm-backup 备份点"
                     "恢复出来的（换机器或日志被清掉时仍可用）。" % self.backup_only)
        desc += "\n「可还原」= 该 commit 仍在本地；备份点会防止对象被 git gc 回收。"
        tk.Label(head, text=desc,
                 fg="#555555", bg="#f5f5f5", font=UI_FONT_SMALL, anchor="w",
                 justify="left", wraplength=745).pack(fill="x", pady=(3, 0))

        body = tk.Frame(self, bg="#f5f5f5")
        body.pack(fill="both", expand=True, padx=12, pady=4)
        cols = ("chk", "name", "time", "sha", "state")
        self.tv = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        for c, w, t in (("chk", 40, "勾选"), ("name", 250, "分支名"),
                        ("time", 140, "删除时间"), ("sha", 90, "SHA"),
                        ("state", 100, "状态")):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w" if c == "name" else "center",
                           stretch=(c == "name"))
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tv.tag_configure("done", foreground="#1c7c3c")
        self.tv.tag_configure("bad", foreground="#999999")
        self.tv.bind("<Button-1>", self._on_click)
        self.tv.bind("<Button-3>", self._on_right_click)

        bottom = tk.Frame(self, bg="#f5f5f5")
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="全选可还原", width=12,
                   command=self._select_restorable).pack(side="left")
        ttk.Button(bottom, text="全不选", width=8,
                   command=self._select_none).pack(side="left", padx=4)
        self.ok_btn = ttk.Button(bottom, text="还原选中(0)", width=14, command=self._do_restore)
        self.ok_btn.pack(side="right")
        ttk.Button(bottom, text="关闭", width=8, command=self.destroy).pack(side="right", padx=6)

        self._render()

    def _render(self):
        self.tv.delete(*self.tv.get_children())
        for i, r in enumerate(self.rows):
            iid = str(i)
            mark = "☑" if iid in self.checked else "☐"
            tags = ()
            if r["_state"] == "已还原":
                tags = ("done",)
            elif r["_state"] != "可还原":
                tags = ("bad",)
            self.tv.insert("", "end", iid=iid, values=(
                mark, r["branch"], r["time"], r["sha"][:8], r["_state"]), tags=tags)
        self._update_btn()

    def _update_btn(self):
        n = 0
        for i in self.checked:
            try:
                if self.rows[int(i)]["_state"] == "可还原":
                    n += 1
            except (ValueError, IndexError):
                pass
        self.ok_btn.config(text="还原选中(%d)" % n)
        self.ok_btn.state(["!disabled"] if n else ["disabled"])

    def _toggle(self, iid):
        if iid in self.checked:
            self.checked.discard(iid)
        else:
            self.checked.add(iid)
        self._render()

    def _on_click(self, event):
        if self.tv.identify_region(event.x, event.y) != "cell":
            return
        if self.tv.identify_column(event.x) != "#1":
            return
        iid = self.tv.identify_row(event.y)
        if iid:
            self._toggle(iid)
            return "break"

    def _on_right_click(self, event):
        iid = self.tv.identify_row(event.y)
        if not iid:
            return
        self.tv.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="勾选/取消勾选", command=lambda: self._toggle(iid))
        m.add_command(label="复制分支名",
                      command=lambda: self._copy(self.rows[int(iid)]["branch"]))
        m.add_command(label="复制 SHA",
                      command=lambda: self._copy(self.rows[int(iid)]["sha"]))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _select_restorable(self):
        self.checked = {str(i) for i, r in enumerate(self.rows) if r["_state"] == "可还原"}
        self._render()

    def _select_none(self):
        self.checked.clear()
        self._render()

    def _do_restore(self):
        targets = []
        for i in list(self.checked):
            try:
                r = self.rows[int(i)]
            except (ValueError, IndexError):
                continue
            if r["_state"] == "可还原":
                targets.append(r)
        if not targets:
            return

        ok, fail = [], []
        for r in targets:
            try:
                code, out, err = self.repo.run(["branch", "--", r["branch"], r["sha"]])
            except Exception as e:
                fail.append((r["branch"], str(e)))
                continue
            if code == 0:
                ok.append(r["branch"])
                r["_state"] = "已还原"
                for ref, _sha in r.get("_backup", []):
                    drop_backup_ref(self.repo, ref)
                try:
                    write_log(self.repo.path, r["branch"], r["sha"], "还原", "RESTORE")
                except Exception:
                    pass
            else:
                fail.append((r["branch"], (err or out).strip()))

        self.checked.clear()
        self._render()

        msg = "还原成功 %d 个。" % len(ok)
        if ok:
            msg += "\n\n" + "\n".join("  " + n for n in ok[:12])
            if len(ok) > 12:
                msg += "\n  … 另有 %d 个" % (len(ok) - 12)
        if fail:
            msg += "\n\n失败 %d 个：\n" % len(fail)
            msg += "\n".join("  %s  ——  %s" % (n, e.replace("\n", " ")[:160])
                             for n, e in fail[:10])
        (messagebox.showwarning if fail else messagebox.showinfo)("还原结果", msg)

        if ok:
            self.restored += len(ok)
            self.parent_app.refresh(False)


# --------------------------------------------------------------------------
# 悬停提示
# --------------------------------------------------------------------------

class Tooltip:
    """轻量 tooltip：悬停 delay 毫秒后显示，支持传入函数动态生成文本。"""

    def __init__(self, widget, text, delay=480, wrap=380):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.wrap = wrap
        self._tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self):
        self._after = None
        try:
            text = self.text() if callable(self.text) else self.text
        except Exception:
            return
        if not text:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        try:
            self._tip.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(self._tip, text=text, justify="left", bg="#fffce0", fg="#333333",
                 relief="solid", bd=1, padx=9, pady=6,
                 font=UI_FONT_SMALL, wraplength=self.wrap).pack()
        self._tip.update_idletasks()
        tw, th = self._tip.winfo_reqwidth(), self._tip.winfo_reqheight()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        sw, sh = self.widget.winfo_screenwidth(), self.widget.winfo_screenheight()
        if x + tw > sw - 10:
            x = max(10, sw - tw - 10)
        if y + th > sh - 10:                       # 下方放不下就翻到上方
            y = self.widget.winfo_rooty() - th - 6
        self._tip.wm_geometry("+%d+%d" % (x, y))

    def _hide(self, event=None):
        self._cancel()
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


# --------------------------------------------------------------------------
# 关于对话框
# --------------------------------------------------------------------------

class AboutDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("关于 " + APP_NAME)
        self.transient(parent)
        self.resizable(False, False)
        self.configure(bg="#ffffff")
        self.geometry("590x470")
        self._parent = parent
        pad_x = 26

        tk.Label(self, text=APP_NAME, font=("Microsoft YaHei UI", 16, "bold"),
                 bg="#ffffff", fg="#1a1a1a").pack(anchor="w", padx=pad_x, pady=(22, 0))
        tk.Label(self, text="v" + APP_VERSION, font=UI_FONT, bg="#ffffff",
                 fg="#888888").pack(anchor="w", padx=pad_x, pady=(0, 12))
        tk.Label(self, text="批量清理 Git 仓库中的陈旧分支。\n"
                            "删除前自动记录 SHA 并创建备份点，删错了随时可以还原。",
                 font=UI_FONT, bg="#ffffff", fg="#333333", justify="left",
                 anchor="w").pack(fill="x", padx=pad_x)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=pad_x, pady=14)

        info = tk.Frame(self, bg="#ffffff")
        info.pack(fill="x", padx=pad_x)
        info.columnconfigure(1, weight=1)
        rows = (
            ("作者", APP_AUTHOR, None),
            ("许可", APP_LICENSE, None),
            ("仓库", APP_REPO, APP_REPO),
            ("最新版", APP_REPO + "/releases/latest", APP_REPO + "/releases/latest"),
        )
        for i, (label, value, url) in enumerate(rows):
            tk.Label(info, text=label, font=UI_FONT, bg="#ffffff", fg="#888888",
                     anchor="w", width=6).grid(row=i, column=0, sticky="w", pady=3)
            if url:
                lb = tk.Label(info, text=value, font=UI_FONT, bg="#ffffff",
                              fg="#1a5fb4", anchor="w", cursor="hand2")
                lb.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
                lb.bind("<Enter>", lambda e, w=lb: w.configure(
                    fg="#0d3f7a", font=(UI_FONT[0], UI_FONT[1], "underline")))
                lb.bind("<Leave>", lambda e, w=lb: w.configure(fg="#1a5fb4", font=UI_FONT))
            else:
                lb = tk.Label(info, text=value, font=UI_FONT, bg="#ffffff",
                              fg="#333333", anchor="w")
            lb.grid(row=i, column=1, sticky="w", pady=3)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=pad_x, pady=14)

        tk.Label(self, text="技术栈：Python 3 + Tkinter（零第三方依赖）+ git CLI",
                 font=UI_FONT_SMALL, bg="#ffffff", fg="#777777",
                 anchor="w").pack(fill="x", padx=pad_x)

        paths = tk.Frame(self, bg="#f7f7f7")
        paths.pack(fill="x", padx=pad_x, pady=(10, 0))
        for txt, pady in (("配置：%s" % CONFIG_PATH, (6, 0)),
                          ("日志：%s" % LOG_PATH, (2, 6))):
            tk.Label(paths, text=txt, font=UI_FONT_SMALL, bg="#f7f7f7", fg="#666666",
                     anchor="w", justify="left", wraplength=500).pack(
                fill="x", padx=8, pady=pady)

        btns = tk.Frame(self, bg="#ffffff")
        btns.pack(fill="x", padx=pad_x, pady=14)
        ttk.Button(btns, text="打开仓库", width=12,
                   command=lambda: webbrowser.open(APP_REPO)).pack(side="left")
        ttk.Button(btns, text="打开程序目录", width=14,
                   command=self._open_dir).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", width=10, command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.after(50, self._center)

    def _open_dir(self):
        try:
            os.startfile(APP_DIR)
        except Exception:
            webbrowser.open("file://" + APP_DIR.replace("\\", "/"))

    def _center(self):
        try:
            self.update_idletasks()
            p = self._parent
            x = p.winfo_rootx() + (p.winfo_width() - self.winfo_width()) // 2
            y = p.winfo_rooty() + (p.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        except Exception:
            pass


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s v%s" % (APP_NAME, APP_VERSION))
        self.geometry("1200x720")
        self.minsize(1000, 560)

        self.cfg = load_config()
        # 兼容旧版/异常配置值，避免下拉框显示非法项
        if self.cfg.get("time_filter") not in TIME_CHOICES:
            self.cfg["time_filter"] = "全部"
        if self.cfg.get("merge_filter") not in MERGE_CHOICES:
            self.cfg["merge_filter"] = "不限"
        self.repo = None
        self.local_branches = []
        self.remote_branches = []
        self.visible_local = []
        self.visible_remote = []
        self.checked = set()
        self.checked_remote = set()
        self.current_branch = ""
        self.remotes = []
        self.merged_cache = {}      # main -> set(refname)
        self.sort_state = {"local": ("date", True), "remote": ("date", True)}
        self.q = queue.Queue()
        self._busy = False
        self._gen = 0
        self._fetch_first = True

        self._build_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._poll_queue)
        self.after(120, self._boot)

    def _on_close(self):
        self._save_column_widths()
        save_config(self.cfg)
        self.destroy()

    # -- 样式 --------------------------------------------------------------
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Treeview", font=UI_FONT, rowheight=23)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TButton", font=UI_FONT)
        style.configure("TCheckbutton", font=UI_FONT)
        style.configure("TLabel", font=UI_FONT)
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))

    # -- 界面 --------------------------------------------------------------
    def _build_ui(self):
        root = ttk.Frame(self, padding=(8, 6, 8, 4))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        # 第一行：仓库路径
        row0 = ttk.Frame(root)
        row0.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        row0.columnconfigure(1, weight=1)
        ttk.Label(row0, text="仓库路径").grid(row=0, column=0, padx=(0, 6))
        self.path_var = tk.StringVar(value=self.cfg.get("last_repo", ""))
        self.path_combo = ttk.Combobox(
            row0, textvariable=self.path_var, font=UI_FONT,
            values=list(self.cfg.get("recent_repos", [])))
        self.path_combo.grid(row=0, column=1, sticky="ew")
        self.path_combo.bind("<Return>", lambda e: self.refresh(True))
        self.path_combo.bind("<<ComboboxSelected>>", self._on_recent_selected)
        Tooltip(self.path_combo, "填 Git 仓库路径，支持填子目录（会自动定位到仓库根）。\n"
                                 "下拉框里是最近成功打开过的 10 个仓库，选中即加载。\n\n"
                                 "启动时不会自动连接，需要点「刷新」才真正读取。")
        ttk.Button(row0, text="浏览…", width=8, command=self.on_browse).grid(row=0, column=2, padx=4)
        self.btn_refresh = ttk.Button(row0, text="刷新", width=8, command=lambda: self.refresh(True))
        self.btn_refresh.grid(row=0, column=3, padx=(0, 4))
        self.btn_fetch = ttk.Button(row0, text="git fetch --prune", width=18, command=self.on_fetch)
        self.btn_fetch.grid(row=0, column=4)
        Tooltip(self.btn_fetch, "执行 git fetch --prune：同步远程分支列表，\n"
                                "并清掉服务器上已删除、本地还留着的跟踪引用。\n\n"
                                "删远程时若报「remote ref does not exist」，点这个就能清掉那条残留。")

        # 第二行：主分支 / 保护白名单 / 当前分支
        row1 = ttk.Frame(root)
        row1.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(row1, text="主分支").pack(side="left", padx=(0, 6))
        self.main_frame = ttk.Frame(row1)
        self.main_frame.pack(side="left")
        ttk.Button(row1, text="＋添加", width=8, command=self.on_add_main).pack(side="left", padx=(6, 12))
        ttk.Label(row1, text="保护").pack(side="left", padx=(0, 4))
        self.protect_var = tk.StringVar(value=", ".join(self.cfg.get("protect_patterns", [])))
        pe = ttk.Entry(row1, textvariable=self.protect_var, width=26, font=UI_FONT)
        pe.pack(side="left")
        pe.bind("<FocusOut>", lambda e: self._save_protect())
        pe.bind("<Return>", lambda e: self._save_protect())
        ttk.Label(row1, text="（如 prod-永久-* ，逗号分隔）", foreground="#777777",
                  font=UI_FONT_SMALL).pack(side="left", padx=(4, 0))
        self.cur_label = ttk.Label(row1, text="当前分支: —", foreground="#1a5fb4")
        self.cur_label.pack(side="right")

        # 第三行：时间条件与搜索
        row2 = ttk.Frame(root)
        row2.grid(row=2, column=0, sticky="ew", pady=(2, 2))

        ttk.Label(row2, text="时间").pack(side="left", padx=(0, 4))
        self.time_var = tk.StringVar(value=self.cfg.get("time_filter", "全部"))
        self.time_combo = ttk.Combobox(row2, textvariable=self.time_var, values=TIME_CHOICES,
                                       state="readonly", width=16, font=UI_FONT)
        self.time_combo.pack(side="left")
        self.time_combo.bind("<<ComboboxSelected>>", lambda e: self.on_time_change())
        Tooltip(self.time_combo, "按「多久没提交过」筛选，不是「最近提交有多频繁」。\n"
                                 "选「超 1 年未提交」= 只显示最后提交早于一年前的分支。\n\n"
                                 "「已并入且过期」按钮的阈值也跟随这里。")

        ttk.Label(row2, text="起").pack(side="left", padx=(8, 2))
        self.from_var = tk.StringVar(value=self.cfg.get("from_date", ""))
        self.from_entry = ttk.Entry(row2, textvariable=self.from_var, width=11, font=UI_FONT)
        self.from_entry.pack(side="left")
        ttk.Label(row2, text="止").pack(side="left", padx=(4, 2))
        self.to_var = tk.StringVar(value=self.cfg.get("to_date", ""))
        self.to_entry = ttk.Entry(row2, textvariable=self.to_var, width=11, font=UI_FONT)
        self.to_entry.pack(side="left", padx=(0, 8))

        ttk.Separator(row2, orient="vertical").pack(side="left", fill="y", padx=6, pady=2)

        ttk.Label(row2, text="🔍").pack(side="left", padx=(2, 2))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(row2, textvariable=self.search_var, width=24, font=UI_FONT)
        self.search_entry.pack(side="left")
        self.search_var.trace_add("write", lambda *a: self.on_filter_change())
        self._placeholder(self.search_entry, self.search_var, "分支名，支持 * 通配")
        Tooltip(self.search_entry, "按分支名筛选，输入即时生效（本地和远程两张表都过滤）：\n\n"
                                   "· prod        包含 prod 的分支（子串，忽略大小写）\n"
                                   "· prod-*      通配符，* 匹配任意字符、? 匹配单个字符\n"
                                   "· prod wip    空格分隔表示「同时包含」多个关键字\n\n"
                                   "筛完点「全选」只会勾选当前可见的行，再点删除就能批量清理。")
        ttk.Button(row2, text="重置", width=6, command=self.on_reset_filter).pack(side="left", padx=6)

        # 第三行续：筛选开关（单独一行，窗口窄时才不会被挤掉）
        row2b = ttk.Frame(root)
        row2b.grid(row=3, column=0, sticky="ew", pady=(0, 4))

        self.filter_local_var = tk.BooleanVar(value=self.cfg.get("filter_local", True))
        self.filter_remote_var = tk.BooleanVar(value=self.cfg.get("filter_remote", True))
        c_loc = ttk.Checkbutton(row2b, text="筛本地", variable=self.filter_local_var,
                                command=self.on_filter_change)
        c_loc.pack(side="left")
        Tooltip(c_loc, "对左表（本地分支）应用时间 / 并入 / 搜索等筛选条件。\n"
                       "取消勾选后左表显示全部分支，不做任何过滤。")
        c_rem = ttk.Checkbutton(row2b, text="筛远程", variable=self.filter_remote_var,
                                command=self.on_filter_change)
        c_rem.pack(side="left", padx=(2, 10))
        Tooltip(c_rem, "对右表（远程分支）应用同样的筛选条件。\n"
                       "取消勾选后右表显示全部分支，不做任何过滤。")

        ttk.Separator(row2b, orient="vertical").pack(side="left", fill="y", padx=4, pady=2)

        ttk.Label(row2b, text="并入").pack(side="left", padx=(8, 4))
        self.merge_var = tk.StringVar(value=self.cfg.get("merge_filter", "不限"))
        mc = ttk.Combobox(row2b, textvariable=self.merge_var, values=MERGE_CHOICES,
                          state="readonly", width=11, font=UI_FONT)
        mc.pack(side="left")
        mc.bind("<<ComboboxSelected>>", lambda e: self.on_filter_change())
        Tooltip(mc, "按「是否已并入主分支」筛选——同时作用于本地、远程两张表。\n\n"
                    "只看已并入：只留下尖端已经在主分支历史里的分支，删除不会丢代码\n"
                    "只看未并入：只留下主分支没有的提交的分支，删除有风险")

        self.only_remote_missing_var = tk.BooleanVar(value=self.cfg.get("only_remote_missing", False))
        cb_miss = ttk.Checkbutton(row2b, text="远程已无同名", variable=self.only_remote_missing_var,
                                  command=self.on_filter_change)
        cb_miss.pack(side="left", padx=(12, 2))
        Tooltip(cb_miss, "只显示「远程已经没有同名分支」的本地分支。\n"
                         "这类通常是已经合并完、可以安全清掉的。")

        self.only_local_exists_var = tk.BooleanVar(value=self.cfg.get("only_local_exists", False))
        cb_locc = ttk.Checkbutton(row2b, text="远程仅看本地有", variable=self.only_local_exists_var,
                                  command=self.on_filter_change)
        cb_locc.pack(side="left", padx=(2, 8))
        Tooltip(cb_locc, "右表只显示「本地存在同名分支」的远程分支。\n"
                         "删掉这类远程分支后仍可从本地重新推送。")

        # 第四行：左右两表
        paned = ttk.PanedWindow(root, orient="horizontal")
        paned.grid(row=4, column=0, sticky="nsew")

        left_wrap = ttk.LabelFrame(paned, text=" 本地分支 ", padding=4)
        right_wrap = ttk.LabelFrame(paned, text=" 远程分支 ", padding=4)
        paned.add(left_wrap, weight=1)
        paned.add(right_wrap, weight=1)

        self.left_head = left_wrap
        self.right_head = right_wrap

        # 左表
        self.left = self._make_tree(
            left_wrap,
            cols=[("chk", 36, "勾选", False), ("name", 190, "分支名", True),
                  ("date", 118, "最后提交", True), ("ago", 58, "距今", True),
                  ("remote", 64, "远程同名", True), ("merged", 104, "已并入主分支", True)],
            sort_key="local")
        self.left.bind("<Button-1>", self._on_left_click)
        self.left.bind("<space>", self._on_left_space)
        self.left.bind("<<TreeviewSelect>>", self._on_select_left)

        # 右表
        self.right = self._make_tree(
            right_wrap,
            cols=[("chk", 36, "勾选", False), ("name", 190, "分支名", True),
                  ("date", 118, "最后提交", True), ("ago", 58, "距今", True),
                  ("local", 64, "本地同名", True), ("merged", 104, "已并入主分支", True)],
            sort_key="remote")
        self.right.configure(selectmode="extended")
        self.right.bind("<Button-1>", self._on_right_click_chk)
        self.right.bind("<space>", self._on_right_space)
        self.right.bind("<<TreeviewSelect>>", self._on_select_right)

        self.left.tag_configure("current", foreground="#1a5fb4")
        self.left.tag_configure("main", foreground="#1c7c3c")
        self.right.tag_configure("main", foreground="#1c7c3c")

        # 第五行：操作按钮
        row4 = ttk.Frame(root)
        row4.grid(row=5, column=0, sticky="ew", pady=(6, 2))

        b_all = ttk.Button(row4, text="全选", width=6, command=self.on_select_all)
        b_all.pack(side="left")
        Tooltip(b_all, "勾选当前列表里所有可见的行（本地 + 远程，受筛选条件影响）")
        b_inv = ttk.Button(row4, text="反选", width=6, command=self.on_invert)
        b_inv.pack(side="left", padx=3)
        Tooltip(b_inv, "已勾选的取消、未勾选的勾上（本地 + 远程）")
        b_none = ttk.Button(row4, text="全不选", width=7, command=self.on_select_none)
        b_none.pack(side="left")
        Tooltip(b_none, "清空所有勾选，不会删除任何东西")

        ttk.Separator(row4, orient="vertical").pack(side="left", fill="y", padx=6, pady=2)

        b_quick = ttk.Button(row4, text="已并入且过期", width=13,
                             command=self.on_quick_merged_old)
        b_quick.pack(side="left")
        Tooltip(b_quick, lambda: (
            "勾选「已并入主分支」并且最后提交早于 %s 的分支。\n\n"
            "「过期」不是固定值，它跟随上方「时间」筛选（当前：%s）。\n"
            "本地和远程会一起勾上，再分别点两个删除按钮执行。"
            % (self._quick_cutoff_desc(), self.time_var.get())))
        b_miss = ttk.Button(row4, text="远程已无且已并入", width=15,
                            command=self.on_quick_remote_missing)
        b_miss.pack(side="left", padx=3)
        Tooltip(b_miss, "勾选「远程已经没有同名分支」且「已并入主分支」的本地分支。\n"
                        "这类通常只剩本地一份残留，可以放心清掉。")

        ttk.Separator(row4, orient="vertical").pack(side="left", fill="y", padx=6, pady=2)

        self.btn_del_local = ttk.Button(row4, text="删除本地选中(0)", width=15,
                                        command=self.on_delete_local)
        self.btn_del_local.pack(side="left")
        Tooltip(self.btn_del_local, "删除左表勾选的本地分支。\n\n"
                                    "自动跳过：当前检出分支、主分支、命中白名单、被 worktree 占用的。\n"
                                    "删前自动记录 SHA 并创建备份点，删错了能从「还原…」找回。")

        self.allow_remote_var = tk.BooleanVar(value=self.cfg.get("allow_remote_delete", False))
        cb_allow = ttk.Checkbutton(row4, text="允许删远程", variable=self.allow_remote_var,
                                   command=self.on_toggle_remote)
        cb_allow.pack(side="left", padx=(8, 2))
        Tooltip(cb_allow, "远程删除默认关闭，勾选后才能点右边的「删除远程选中」。\n"
                          "提交时必须手工输入 DELETE 确认。")
        self.btn_del_remote = ttk.Button(row4, text="删除远程选中", width=12,
                                         command=self.on_delete_remote)
        self.btn_del_remote.pack(side="left")
        Tooltip(self.btn_del_remote, "删除右表勾选的远程分支（需先勾选「允许删远程」）。\n\n"
                                     "已并入主分支的，输入 DELETE 即可；\n"
                                     "未并入的还要额外勾选确认，本地也没有副本时会红色警告可能彻底丢失。")

        b_restore = ttk.Button(row4, text="还原…", width=8, command=self.on_restore)
        b_restore.pack(side="left", padx=(8, 0))
        Tooltip(b_restore, "从删除日志和仓库内的备份点恢复已删除的分支。\n"
                           "只显示当前仓库的记录；日志丢了也能从 refs/gbm-backup 找回来。")
        b_export = ttk.Button(row4, text="导出 CSV", width=10, command=self.on_export)
        b_export.pack(side="right")
        Tooltip(b_export, "把当前筛选出的分支导出成 CSV。\nUTF-8 BOM 编码，Excel 双击打开中文不乱码。")

        # 第六行：状态栏（上下两行，窗口窄时也不会互相挤掉）
        status = ttk.Frame(root)
        status.grid(row=6, column=0, sticky="ew", pady=(2, 0))
        line1 = ttk.Frame(status)
        line1.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(line1, textvariable=self.status_var, foreground="#333333",
                  anchor="w").pack(side="left", fill="x", expand=True)
        # 进度条默认不显示，长任务时 pack 出来
        self.progress = ttk.Progressbar(line1, length=150, mode="indeterminate")
        self.detail_var = tk.StringVar(value="")
        line2 = ttk.Frame(status)
        line2.pack(fill="x")
        ttk.Label(line2, textvariable=self.detail_var, foreground="#777777",
                  font=UI_FONT_SMALL, anchor="w").pack(side="left", fill="x", expand=True)
        self.ver_label = ttk.Label(
            line2, text="v%s  ·  %s  ·  关于" % (APP_VERSION, APP_AUTHOR),
            foreground="#999999", font=UI_FONT_SMALL, cursor="hand2")
        self.ver_label.pack(side="right")
        self.ver_label.bind("<Button-1>", lambda e: self.on_about())
        self.ver_label.bind("<Enter>", lambda e: self.ver_label.configure(foreground="#1a5fb4"))
        self.ver_label.bind("<Leave>", lambda e: self.ver_label.configure(foreground="#999999"))

        self._refresh_main_chips()
        state = "normal" if self.time_var.get() == "自定义" else "disabled"
        self.from_entry.config(state=state)
        self.to_entry.config(state=state)

    def _make_tree(self, parent, cols, sort_key):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        ids = [c[0] for c in cols]
        tv = ttk.Treeview(wrap, columns=ids, show="headings", selectmode="browse")
        widths = self.cfg.get("column_widths", {}) or {}
        for cid, w, title, sortable in cols:
            tv.heading(cid, text=title,
                       command=(lambda c=cid: self._on_sort(sort_key, c)) if sortable else "")
            key = "%s.%s" % (sort_key, cid)
            tv.column(cid, width=int(widths.get(key, w)), minwidth=40,
                      anchor="center" if cid not in ("name",) else "w",
                      stretch=(cid in ("name", "merged")))
        vsb = ttk.Scrollbar(wrap, orient="vertical")
        hsb = ttk.Scrollbar(wrap, orient="horizontal")
        tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.configure(command=tv.yview)
        hsb.configure(command=tv.xview)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        tv.bind("<Button-3>", lambda e, t=tv: self._show_context_menu(e, t))
        return tv

    # -- 启动 --------------------------------------------------------------
    def _boot(self):
        if not self._check_git():
            return
        # 只预填上次的路径，不自动连接；用户点「刷新」才真正读取仓库
        path = self.path_var.get().strip()
        if path:
            self.status_var.set("上次使用的仓库已填入，点「刷新」加载分支。")
        else:
            self.status_var.set("请在上方填写或选择一个 Git 仓库路径，然后点「刷新」。")
        self.detail_var.set("")

    def _check_git(self):
        try:
            p = subprocess.run(["git", "--version"], capture_output=True, text=True,
                               timeout=10, creationflags=CREATE_NO_WINDOW)
            if p.returncode != 0:
                raise OSError
            return True
        except Exception:
            messagebox.showerror(
                "未找到 git",
                "无法执行 git 命令。\n\n请确认已安装 Git，并将其 bin 目录加入系统 PATH，"
                "或重启本程序。")
            self.status_var.set("git 不可用")
            return False

    # -- 队列 --------------------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                fn, payload = self.q.get_nowait()
                try:
                    fn(payload)
                except Exception:
                    self._log_exception()
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _bg(self, work, done):
        def runner():
            try:
                res = work()
            except Exception as e:
                res = e
            self.q.put((done, res))
        threading.Thread(target=runner, daemon=True).start()

    # -- 进度反馈 ----------------------------------------------------------
    def _show_progress(self, text=None, maximum=None):
        """maximum 有值 -> 百分比进度；否则 -> 来回滚动的忙碌动画。"""
        if text:
            self.status_var.set(text)
        try:
            if maximum:
                self.progress.configure(mode="determinate", maximum=max(int(maximum), 1), value=0)
                self.progress.stop()
            else:
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            if not self.progress.winfo_ismapped():
                self.progress.pack(side="right", padx=(10, 0))
        except Exception:
            pass

    def _end_progress(self):
        try:
            self.progress.stop()
            self.progress.pack_forget()
        except Exception:
            pass

    def _report(self, value=None, text=None):
        """后台线程调用，投递进度到主线程。"""
        self.q.put((self._on_progress, (value, text)))

    def _on_progress(self, payload):
        value, text = payload
        if value is not None:
            try:
                self.progress.configure(value=int(value))
            except Exception:
                pass
        if text:
            self.status_var.set(text)

    def _log_exception(self):
        msg = traceback.format_exc()
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write("[%s]\n%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass
        messagebox.showerror("程序异常", msg[-1500:])

    def report_callback_exception(self, exc, val, tb):
        msg = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write("[%s]\n%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass
        messagebox.showerror("程序异常", msg[-1500:])

    # -- 仓库 --------------------------------------------------------------
    def _push_recent(self, path):
        """把成功打开的仓库记入下拉历史：去重、最新在前、最多 10 条。"""
        lst = [p for p in self.cfg.get("recent_repos", []) if not same_repo(p, path)]
        lst.insert(0, path)
        self.cfg["recent_repos"] = lst[:10]
        try:
            self.path_combo.configure(values=self.cfg["recent_repos"])
        except Exception:
            pass
        save_config(self.cfg)

    def _on_recent_selected(self, event=None):
        path = self.path_var.get().strip()
        if not path:
            return
        if self.repo and same_repo(path, self.repo.path):
            return          # 已经是当前仓库，不重复加载
        self.refresh(True)

    def on_browse(self):
        init = self.path_var.get().strip() or os.path.expanduser("~")
        path = filedialog.askdirectory(title="选择 Git 仓库目录", initialdir=init)
        if path:
            self.path_var.set(os.path.normpath(path))
            self.refresh(True)

    def refresh(self, notify_empty=False):
        path = self.path_var.get().strip()
        if not path:
            if notify_empty:
                messagebox.showinfo("提示", "请先填写或选择仓库路径。")
            return
        if self._busy:
            return
        self._busy = True
        self._gen += 1
        gen = self._gen
        self._show_progress("正在读取分支…")
        self.detail_var.set("")

        def work():
            repo = Repo(path)
            root = repo.open()
            cur = repo.current_branch()
            remotes = repo.remotes()
            locals_, remotes_b = repo.branches()
            return repo, root, cur, remotes, locals_, remotes_b

        def done(res):
            if gen != self._gen:
                return
            self._busy = False
            self._end_progress()
            if isinstance(res, Exception):
                msg = str(res) if isinstance(res, GitError) else "读取失败：%s" % res
                if not isinstance(res, GitError):
                    self._log_exception()
                self.status_var.set(msg)
                self.local_branches, self.remote_branches = [], []
                self.visible_local, self.visible_remote = [], []
                self._render()
                self._update_buttons()
                messagebox.showerror("无法读取仓库", msg)
                return
            repo, root, cur, remotes, locals_, remotes_b = res
            self.repo = repo
            self.current_branch = cur
            self.remotes = remotes
            self.local_branches = locals_
            self.remote_branches = remotes_b
            self.checked.clear()
            self.checked_remote.clear()
            self.path_var.set(root)
            self.cfg["last_repo"] = root
            self._push_recent(root)
            save_config(self.cfg)
            self.cur_label.config(
                text="当前分支: %s" % (cur if cur else "(detached)"))
            self._ensure_default_mains()
            self._compute_and_render()
            if repo.is_bare:
                self.status_var.set("裸仓库：仅支持只读浏览，已禁用本地删除。")

        self._bg(work, done)

    def _ensure_default_mains(self):
        names = {b.name for b in self.local_branches}
        mains = [m for m in self.cfg.get("main_branches", []) if m in names]
        if not mains:
            for cand in ("prod", "master", "main"):
                if cand in names and cand not in mains:
                    mains.append(cand)
                    if len(mains) >= 2:
                        break
            if not mains:
                for cand in ("prod", "master", "main"):
                    if cand in names:
                        mains.append(cand)
                        break
        self.cfg["main_branches"] = mains
        save_config(self.cfg)
        self._refresh_main_chips()

    def _refresh_main_chips(self):
        for w in self.main_frame.winfo_children():
            w.destroy()
        mains = self.cfg.get("main_branches", [])
        if not mains:
            ttk.Label(self.main_frame, text="（未设置，判定功能停用）",
                      foreground="#b00020", font=UI_FONT_SMALL).pack(side="left")
            return
        for m in mains:
            chip = tk.Frame(self.main_frame, bg="#e6eefc", bd=0, padx=0, pady=0)
            chip.pack(side="left", padx=(0, 4))
            tk.Label(chip, text=" " + m + " ", bg="#e6eefc", font=UI_FONT,
                     fg="#1a5fb4").pack(side="left")
            btn = tk.Label(chip, text="×", bg="#e6eefc", fg="#666666", font=UI_FONT,
                           cursor="hand2", padx=4)
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda e, name=m: self.on_remove_main(name))
            btn.bind("<Enter>", lambda e, w=btn: w.config(fg="#b00020"))
            btn.bind("<Leave>", lambda e, w=btn: w.config(fg="#666666"))

    def on_add_main(self):
        if not self.repo:
            messagebox.showinfo("提示", "请先打开一个仓库。")
            return
        name = simpledialog.askstring("添加主分支", "输入分支名（本地分支）：", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self.cfg.get("main_branches", []):
            messagebox.showinfo("提示", "该分支已在列表中。")
            return
        if not self.repo.branch_exists(name):
            messagebox.showwarning("不存在", "本地不存在分支「%s」，无法加入主分支列表。\n\n"
                                             "（仅本地分支可作为主分支基准）" % name)
            return
        self.cfg.setdefault("main_branches", []).append(name)
        save_config(self.cfg)
        self._refresh_main_chips()
        self._compute_and_render()

    def on_remove_main(self, name):
        mains = self.cfg.get("main_branches", [])
        if name in mains:
            mains.remove(name)
            self.cfg["main_branches"] = mains
            save_config(self.cfg)
            self._refresh_main_chips()
            self._compute_and_render()

    # -- 合并判定 + 渲染 ----------------------------------------------------
    def _compute_and_render(self):
        mains = list(self.cfg.get("main_branches", []))
        repo = self.repo
        if not repo:
            return
        gen = self._gen
        if not mains:
            for b in self.local_branches + self.remote_branches:
                b.merged_into = []
            self._join_flags()
            self._render()
            return

        self._show_progress("正在判定并入关系…")

        def work():
            result = {}
            for m in mains:
                try:
                    result[m] = repo.merged_refs(m)
                except GitError:
                    result[m] = set()
            return result

        def done(res):
            if gen != self._gen:
                return          # 期间又发起了新的刷新，丢弃过期结果
            self._end_progress()
            if isinstance(res, Exception):
                self.status_var.set("并入判定失败：%s" % res)
                for b in self.local_branches + self.remote_branches:
                    b.merged_into = []
                self._join_flags()
                self._render()
                return
            self.merged_cache = res
            for b in self.local_branches + self.remote_branches:
                hits = [m for m in mains if b.full in res.get(m, set())]
                b.merged_into = hits
            self._join_flags()
            self._render()

        self._bg(work, done)

    def _join_flags(self):
        mains = set(self.cfg.get("main_branches", []))
        known = bool(mains)
        remote_names = {}
        for r in self.remote_branches:
            remote_names.setdefault(r.name, []).append(r)
        local_names = {l.name for l in self.local_branches}

        for b in self.local_branches:
            b.is_main = b.name in mains
            b.is_current = bool(self.current_branch) and b.name == self.current_branch
            b.has_remote = b.name in remote_names
            b.merged_known = known
        for b in self.remote_branches:
            b.is_main = b.name in mains
            b.has_local = b.name in local_names
            b.merged_known = known

    def _render(self):
        self._apply_filters()
        self._render_left()
        self._render_right()
        self._update_headers()
        self._update_buttons()
        self._update_status()

    def _update_headers(self):
        self.left_head.config(text=" 本地分支 (%d) " % len(self.visible_local))
        if not self.remotes:
            self.right_head.config(text=" 远程分支 (0) — 未配置远程 ")
        else:
            self.right_head.config(text=" 远程分支 (%d) " % len(self.visible_remote))

    # -- 筛选 --------------------------------------------------------------
    def _time_predicate(self):
        """返回 (predicate, 描述)。predicate(ts) -> bool"""
        key = self.time_var.get()
        now = datetime.now()
        if key in TIME_TO_MONTHS:
            months = TIME_TO_MONTHS[key]
            cutoff = minus_months(now, months).timestamp()
            return (lambda ts: 0 < ts < cutoff), key
        if key == "自定义":
            d1 = parse_date(self.from_var.get())
            d2 = parse_date(self.to_var.get())
            if not d1 and not d2:
                return (lambda ts: True), "自定义(未填日期)"
            lo = d1.timestamp() if d1 else 0
            hi = (d2 + timedelta(days=1)).timestamp() if d2 else float("inf")
            return (lambda ts: lo <= ts < hi), "自定义"
        return (lambda ts: True), "全部"

    def _merge_predicate(self):
        mode = self.merge_var.get()
        mains = self.cfg.get("main_branches", [])
        if mode == "只看已并入":
            return lambda b: bool(b.merged_into)
        if mode == "只看未并入":
            return lambda b: (bool(mains) and not b.merged_into and not b.is_main) if mains else (not b.is_main)
        return lambda b: True

    def _name_matcher(self):
        """分支名匹配：默认子串（忽略大小写）；含 * / ? 时按通配符；空格分隔多个关键字取交集。"""
        raw = self.search_var.get().strip().lower()
        if not raw:
            return lambda name: True
        terms = raw.split()

        def match_one(low, t):
            if "*" in t or "?" in t:
                return fnmatch.fnmatchcase(low, t)
            return t in low

        def matcher(name):
            n = name.lower()
            return all(match_one(n, t) for t in terms)

        return matcher

    def _apply_filters(self):
        pred_time, self._time_desc = self._time_predicate()
        pred_merge = self._merge_predicate()
        name_ok = self._name_matcher()

        def keep(b, is_local):
            # name 不带 remote 前缀，short 带；任一命中即可，这样 prod-* 也能匹配 origin/prod-x
            if not (name_ok(b.name) or name_ok(b.short)):
                return False
            if not pred_time(b.commit_ts):
                return False
            if not pred_merge(b):
                return False
            if is_local:
                if self.only_remote_missing_var.get() and b.has_remote:
                    return False
            else:
                if self.only_local_exists_var.get() and not b.has_local:
                    return False
            return True

        lb = [b for b in self.local_branches if keep(b, True)] if self.filter_local_var.get() \
            else list(self.local_branches)
        rb = [b for b in self.remote_branches if keep(b, False)] if self.filter_remote_var.get() \
            else list(self.remote_branches)

        self.visible_local = self._sort_rows(lb, "local")
        self.visible_remote = self._sort_rows(rb, "remote")

    def _sort_rows(self, rows, which):
        col, reverse = self.sort_state[which]
        keymap = {
            "name": lambda b: b.short.lower(),
            "date": lambda b: b.commit_ts,
            "ago": lambda b: b.commit_ts,
            "remote": lambda b: 1 if b.has_remote else 0,
            "local": lambda b: 1 if b.has_local else 0,
            "merged": lambda b: len(b.merged_into) if b.merged_into else 0,
        }
        key = keymap.get(col, keymap["date"])
        try:
            return sorted(rows, key=key, reverse=reverse)
        except Exception:
            return rows

    def _on_sort(self, which, col):
        cur_col, cur_rev = self.sort_state[which]
        self.sort_state[which] = (col, not cur_rev if cur_col == col else False)
        self._render()

    def _render_left(self):
        tv = self.left
        tv.delete(*tv.get_children())
        for b in self.visible_local:
            mark = "☑" if b.full in self.checked else "☐"
            tags = []
            if b.is_current:
                tags.append("current")
            if b.is_main:
                tags.append("main")
            tv.insert("", "end", iid=b.full, values=(
                mark, b.short, fmt_date(b.commit_ts), fmt_ago(b.commit_ts),
                "有" if b.has_remote else "无", merged_label(b)), tags=tuple(tags))

    def _render_right(self):
        tv = self.right
        tv.delete(*tv.get_children())
        for b in self.visible_remote:
            tags = ("main",) if b.is_main else ()
            mark = "☑" if b.full in self.checked_remote else "☐"
            tv.insert("", "end", iid=b.full, values=(
                mark, b.short, fmt_date(b.commit_ts), fmt_ago(b.commit_ts),
                "有" if b.has_local else "无", merged_label(b)), tags=tags)

    # -- 交互 --------------------------------------------------------------
    def _on_left_click(self, event):
        if self.left.identify_region(event.x, event.y) != "cell":
            return
        col = self.left.identify_column(event.x)
        row = self.left.identify_row(event.y)
        if not row:
            return
        if col == "#1":
            self._toggle(row)
            return "break"

    def _on_left_space(self, event):
        sel = self.left.selection()
        for iid in sel:
            self._toggle(iid, force=None)
        return "break"

    def _on_right_click_chk(self, event):
        if self.right.identify_region(event.x, event.y) != "cell":
            return
        if self.right.identify_column(event.x) != "#1":
            return
        iid = self.right.identify_row(event.y)
        if iid:
            self._toggle_remote(iid)
            return "break"

    def _on_right_space(self, event):
        for iid in self.right.selection():
            self._toggle_remote(iid)
        return "break"

    def _toggle_remote(self, iid, force=None):
        if iid in self.checked_remote:
            if force is True:
                return
            self.checked_remote.discard(iid)
            mark = "☐"
        else:
            if force is False:
                return
            self.checked_remote.add(iid)
            mark = "☑"
        try:
            vals = list(self.right.item(iid, "values"))
            vals[0] = mark
            self.right.item(iid, values=vals)
        except Exception:
            pass
        self._update_buttons()
        self._update_status()

    def _toggle(self, iid, force=None):
        if iid in self.checked:
            if force is True:
                return
            self.checked.discard(iid)
            mark = "☐"
        else:
            if force is False:
                return
            self.checked.add(iid)
            mark = "☑"
        try:
            vals = list(self.left.item(iid, "values"))
            vals[0] = mark
            self.left.item(iid, values=vals)
        except Exception:
            pass
        self._update_buttons()
        self._update_status()

    def _on_select_left(self, event):
        sel = self.left.selection()
        if sel:
            b = self._find(self.local_branches, sel[0])
            if b:
                self.detail_var.set("%s  ·  %s  ·  %s" % (b.short, b.sha, fmt_date(b.commit_ts)))

    def _on_select_right(self, event):
        sel = self.right.selection()
        if sel:
            b = self._find(self.remote_branches, sel[-1])
            if b:
                self.detail_var.set("%s  ·  %s  ·  %s" % (b.short, b.sha, fmt_date(b.commit_ts)))
        self._update_buttons()

    def _find(self, rows, full):
        for b in rows:
            if b.full == full:
                return b
        return None

    def _show_context_menu(self, event, tv):
        iid = tv.identify_row(event.y)
        if not iid:
            return
        tv.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        if tv is self.left:
            m.add_command(label="勾选/取消勾选", command=lambda: self._toggle(iid))
            m.add_command(label="复制分支名", command=lambda: self._copy(iid))
            m.add_separator()
            m.add_command(label="删除该分支…", command=self.on_delete_local)
        else:
            m.add_command(label="勾选/取消勾选", command=lambda: self._toggle_remote(iid))
            m.add_command(label="复制分支名", command=lambda: self._copy(iid))
            m.add_separator()
            m.add_command(label="删除该远程分支…", command=lambda: self._delete_one_remote(iid))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _copy(self, iid):
        if iid.startswith("refs/heads/"):
            name = iid[len("refs/heads/"):]
        elif iid.startswith("refs/remotes/"):
            name = iid[len("refs/remotes/"):]
        else:
            name = iid
        self.clipboard_clear()
        self.clipboard_append(name)
        self.status_var.set("已复制：%s" % name)

    def on_select_all(self):
        for b in self.visible_local:
            self.checked.add(b.full)
        for b in self.visible_remote:
            self.checked_remote.add(b.full)
        self._render_left()
        self._render_right()
        self._update_buttons()
        self._update_status()

    def on_select_none(self):
        self.checked.clear()
        self.checked_remote.clear()
        self._render_left()
        self._render_right()
        self._update_buttons()
        self._update_status()

    def on_invert(self):
        for b in self.visible_local:
            if b.full in self.checked:
                self.checked.discard(b.full)
            else:
                self.checked.add(b.full)
        for b in self.visible_remote:
            if b.full in self.checked_remote:
                self.checked_remote.discard(b.full)
            else:
                self.checked_remote.add(b.full)
        self._render_left()
        self._render_right()
        self._update_buttons()
        self._update_status()

    def _quick_cutoff(self):
        """快捷勾选使用的过期阈值：优先用当前时间筛选，否则按 1 年。"""
        key = self.time_var.get()
        if key in TIME_TO_MONTHS:
            return minus_months(datetime.now(), TIME_TO_MONTHS[key]).timestamp()
        return minus_months(datetime.now(), 12).timestamp()

    def _placeholder(self, entry, var, text):
        """给 Entry 加占位提示：Tk 没有原生支持，叠一个 Label 来实现。"""
        lb = tk.Label(entry, text=text, foreground="#a6a6a6", background="#ffffff",
                      font=UI_FONT, cursor="xterm", anchor="w")

        def sync(*_a):
            if var.get().strip():
                lb.place_forget()
            else:
                lb.place(x=5, y=1)

        var.trace_add("write", sync)
        entry.bind("<FocusIn>", lambda e: lb.place_forget(), add="+")
        entry.bind("<FocusOut>", lambda e: sync(), add="+")
        lb.bind("<Button-1>", lambda e: entry.focus_set())
        sync()

    def _quick_cutoff_desc(self):
        """给 tooltip 用：说明「过期」当前按什么算。"""
        key = self.time_var.get()
        months = TIME_TO_MONTHS.get(key, 12)
        d = minus_months(datetime.now(), months)
        if key in TIME_TO_MONTHS:
            note = "时间筛选「%s」" % key
        else:
            note = "时间筛选「%s」，按 1 年算" % key
        return "%s（%s）" % (d.strftime("%Y-%m-%d"), note)

    def on_quick_merged_old(self):
        cutoff = self._quick_cutoff()
        n_local = n_remote = 0
        for b in self.visible_local:
            if b.merged_into and not b.is_current and not b.is_main and 0 < b.commit_ts < cutoff:
                self.checked.add(b.full)
                n_local += 1
        for b in self.visible_remote:
            if b.merged_into and not b.is_main and 0 < b.commit_ts < cutoff:
                self.checked_remote.add(b.full)
                n_remote += 1
        self._render_left()
        self._render_right()
        self._update_buttons()
        self._update_status()
        self.status_var.set("已勾选 %d 个本地、%d 个远程「已并入主分支且过期」的分支"
                            % (n_local, n_remote))

    def on_quick_remote_missing(self):
        n = 0
        for b in self.visible_local:
            if b.merged_into and not b.has_remote and not b.is_current and not b.is_main:
                self.checked.add(b.full)
                n += 1
        self._render_left()
        self._update_buttons()
        self._update_status()
        self.status_var.set("已勾选 %d 个「远程已无同名且已并入」的本地分支" % n)

    def on_time_change(self):
        custom = self.time_var.get() == "自定义"
        state = "normal" if custom else "disabled"
        self.from_entry.config(state=state)
        self.to_entry.config(state=state)
        self.on_filter_change()

    def on_filter_change(self):
        self.cfg["time_filter"] = self.time_var.get()
        self.cfg["from_date"] = self.from_var.get()
        self.cfg["to_date"] = self.to_var.get()
        self.cfg["merge_filter"] = self.merge_var.get()
        self.cfg["only_remote_missing"] = self.only_remote_missing_var.get()
        self.cfg["only_local_exists"] = self.only_local_exists_var.get()
        self.cfg["filter_local"] = self.filter_local_var.get()
        self.cfg["filter_remote"] = self.filter_remote_var.get()
        save_config(self.cfg)
        if self.local_branches or self.remote_branches:
            self._render()

    def on_reset_filter(self):
        self.time_var.set("全部")
        self.from_var.set("")
        self.to_var.set("")
        self.merge_var.set("不限")
        self.only_remote_missing_var.set(False)
        self.only_local_exists_var.set(False)
        self.search_var.set("")
        self.from_entry.config(state="disabled")
        self.to_entry.config(state="disabled")
        self.on_filter_change()

    def _save_protect(self):
        raw = self.protect_var.get()
        pats = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
        self.cfg["protect_patterns"] = pats
        save_config(self.cfg)

    # -- 状态与按钮 --------------------------------------------------------
    def _visible_checked(self):
        return [b for b in self.visible_local if b.full in self.checked]

    def _visible_checked_remote(self):
        return [b for b in self.visible_remote if b.full in self.checked_remote]

    def _update_buttons(self):
        n_local = len(self._visible_checked())
        n_remote = len(self._visible_checked_remote())
        self.btn_del_local.config(text="删除本地选中(%d)" % n_local)
        self.btn_del_remote.config(text="删除远程选中(%d)" % n_remote)
        if not self.repo or self.repo.is_bare:
            self.btn_del_local.state(["disabled"])
            self.btn_del_remote.state(["disabled"])
            return
        self.btn_del_local.state(["!disabled"] if n_local else ["disabled"])
        can_remote = (self.allow_remote_var.get() and n_remote > 0 and bool(self.remotes))
        self.btn_del_remote.state(["!disabled"] if can_remote else ["disabled"])

    def _update_status(self):
        total_local = len(self.local_branches)
        merged_n = sum(1 for b in self.local_branches if b.merged_into)
        no_remote_n = sum(1 for b in self.local_branches if not b.has_remote)
        n_local = len(self._visible_checked())
        n_remote = len(self._visible_checked_remote())
        self.status_var.set(
            "当前筛选：本地 %d 行 / 远程 %d 行，已选 本地 %d / 远程 %d　|　"
            "本地共 %d 个，已并入 %d 个，远程已无 %d 个"
            % (len(self.visible_local), len(self.visible_remote), n_local, n_remote,
               total_local, merged_n, no_remote_n))
        if not self.detail_var.get():
            self._update_fetch_hint()

    def _update_fetch_hint(self):
        if not self.repo:
            self.detail_var.set("")
            return
        head = os.path.join(self.repo.path, ".git", "FETCH_HEAD")
        if self.repo.is_bare:
            head = os.path.join(self.repo.path, "FETCH_HEAD")
        try:
            ts = os.path.getmtime(head)
            when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = "从未"
        self.detail_var.set("远程状态基于本地跟踪引用，最后 fetch：%s（如需准确请先 fetch）" % when)

    # -- fetch -------------------------------------------------------------
    def on_fetch(self):
        if not self.repo:
            messagebox.showinfo("提示", "请先打开一个仓库。")
            return
        if self._busy:
            return
        self._busy = True
        self.btn_del_local.state(["disabled"])
        self.btn_del_remote.state(["disabled"])
        self.btn_fetch.state(["disabled"])
        self._show_progress("正在执行 git fetch --prune …")
        repo = self.repo

        def work():
            return repo.fetch_prune()

        def done(res):
            self._busy = False
            self.btn_fetch.state(["!disabled"])
            self._end_progress()
            if isinstance(res, Exception):
                self.status_var.set("fetch 失败：%s" % res)
                self._update_buttons()
                return
            code, out = res
            if code == 0:
                self.status_var.set("fetch 完成，正在刷新…")
                self._busy = False
                self.refresh(False)
            else:
                self.status_var.set("fetch 失败：%s" % (out.splitlines()[-1] if out else "未知错误"))
                self._update_buttons()
                messagebox.showwarning("fetch 失败", out[-1500:] or "未知错误")

        self._bg(work, done)

    # -- 保护判断 ----------------------------------------------------------
    def protect_reason(self, b, for_remote=False, live_current=None, worktrees=None):
        mains = self.cfg.get("main_branches", [])
        cur = self.current_branch if live_current is None else live_current
        wt = worktrees or {}
        if not for_remote:
            if cur and b.name == cur:
                return "当前检出分支"
            if b.name in wt:
                return "被 worktree 检出（%s）" % wt[b.name]
            if b.is_main or b.name in mains:
                return "主分支"
        else:
            if b.name in mains:
                return "主分支"
        for pat in self.cfg.get("protect_patterns", []):
            if fnmatch.fnmatch(b.name, pat):
                return "命中保护白名单(%s)" % pat
        return ""

    def _merged_text(self, b):
        if b.merged_into:
            return "已并入(%s)" % "/".join(b.merged_into)
        return "未并入"

    # -- 删除本地 ----------------------------------------------------------
    def on_delete_local(self):
        if not self.repo:
            return
        if self.repo.is_bare:
            messagebox.showwarning("不支持", "这是裸仓库，无法删除本地分支。")
            return
        rows = self._visible_checked()
        if not rows:
            messagebox.showinfo("提示", "请先勾选要删除的本地分支。")
            return

        # 删除前实时复核：程序打开后用户可能在别处切换过分支
        live_current = self.current_branch
        try:
            live_current = self.repo.current_branch()
            if live_current != self.current_branch:
                self.current_branch = live_current
            self.cur_label.config(
                text="当前分支: %s" % (live_current if live_current else "(detached)"))
        except Exception:
            pass
        try:
            worktrees = self.repo.worktree_branches()
        except Exception:
            worktrees = {}

        allowed, skipped = [], []
        for b in rows:
            reason = self.protect_reason(b, live_current=live_current, worktrees=worktrees)
            if reason:
                skipped.append((b, reason))
            else:
                allowed.append(b)

        if not allowed:
            messagebox.showwarning(
                "无可删除分支",
                "选中的 %d 个分支全部被保护，未执行删除：\n\n%s"
                % (len(rows), format_branch_reasons(skipped, limit=12, indent="  ")))
            return

        dlg = ConfirmDialog(self, "确认删除本地分支", allowed, skipped)
        self.wait_window(dlg)
        if not dlg.result:
            return

        # 日志可写性预检查（一次文件操作），避免后台删到一半才发现写不了
        try:
            with open(LOG_PATH, "a", encoding="utf-8"):
                pass
        except Exception as e:
            if not messagebox.askyesno(
                    "日志不可写",
                    "无法写入 %s：\n%s\n\n继续删除将无法通过日志恢复分支，是否继续？"
                    % (LOG_PATH, e)):
                return

        # 全部放后台。注意这里做了批量化：逐个分支走子进程的话，
        # Windows 上每次 git 启动约 200ms，30 个分支就要 20 秒以上。
        repo = self.repo
        targets = [(b.short, b.name, b.sha, self._merged_text(b)) for b in allowed]
        total = len(targets)
        self._busy = True
        self.btn_del_local.state(["disabled"])
        self.btn_del_remote.state(["disabled"])
        self._show_progress("正在删除 %d 个分支…" % total)

        def work():
            ok, fail, valid = [], [], []
            # 1) 写日志（纯文件 IO）
            for short, name, sha, merged_text in targets:
                try:
                    write_delete_log(repo.path, short, sha, merged_text)
                    valid.append((short, name, sha))
                except Exception as e:
                    fail.append((short, "日志写入失败，已跳过：%s" % e))
            if not valid:
                return ok, fail

            # 2) 批量建备份 ref：一次 update-ref --stdin 代替 N 次子进程
            self._report(None, "正在创建备份点（%d 个）…" % len(valid))
            bak_rc = 1
            try:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                lines = "".join("update %s %s\n" % (backup_ref_path(name, stamp), sha)
                                for _short, name, sha in valid)
                bak_rc, _o, _e = repo.run(["update-ref", "--stdin"], input_text=lines)
            except Exception:
                bak_rc = 1
            if bak_rc != 0:
                # 批量失败（极少见）退回逐个，保证备份点不丢
                for _short, name, sha in valid:
                    try:
                        make_backup_ref(repo, name, sha)
                    except Exception:
                        pass

            # 3) 批量删除
            self._report(None, "正在删除 %d 个分支…" % len(valid))
            names = [v[0] for v in valid]
            try:
                repo.run(["branch", "-D", "--"] + names)
            except Exception:
                pass

            # 4) 用剩余列表核对结果，对没删掉的逐个诊断原因
            remain = repo.local_branch_names()
            lost = []
            for n in names:
                if n in remain:
                    lost.append(n)
                else:
                    ok.append(n)
            for n in lost:
                try:
                    c2, o2, e2 = repo.run(["branch", "-D", "--", n])
                except Exception as e:
                    fail.append((n, str(e)))
                    continue
                if c2 == 0:
                    ok.append(n)
                else:
                    fail.append((n, (e2 or o2).strip()))
            return ok, fail

        def done(res):
            self._end_progress()
            self._busy = False
            if isinstance(res, Exception):
                self.status_var.set("删除失败：%s" % res)
                self._update_buttons()
                return
            ok, fail = res
            self.checked.clear()
            self.status_var.set("删除完成：成功 %d 个，失败 %d 个" % (len(ok), len(fail)))
            msg = ["成功删除 %d 个，跳过 %d 个，失败 %d 个。"
                   % (len(ok), len(skipped), len(fail))]
            if skipped:
                msg.append("\n跳过 %d 个：" % len(skipped))
                msg.append(format_branch_reasons(skipped, limit=10, indent="  "))
            if fail:
                msg.append("\n失败 %d 个：" % len(fail))
                for n, e in fail[:10]:
                    e = e.replace("\n", " ")
                    if "worktree" in e.lower() or "checked out" in e.lower():
                        e += "  ← 被其他 worktree 占用"
                    msg.append("  %s  ——  %s" % (n, e))
            msg.append("\n恢复方式：点主界面「还原…」按钮，或执行 git branch <分支名> <SHA>"
                       "\n（记录见 %s）" % os.path.basename(LOG_PATH))
            if fail:
                messagebox.showwarning("删除结果", "\n".join(msg))
            else:
                messagebox.showinfo("删除结果", "\n".join(msg))
            self.refresh(False)

        self._bg(work, done)

    # -- 删除远程 ----------------------------------------------------------
    def on_about(self):
        AboutDialog(self)

    def on_restore(self):
        if not self.repo:
            messagebox.showinfo("提示", "请先打开一个仓库。")
            return
        RestoreDialog(self, self.repo)

    def _delete_one_remote(self, iid):
        self.checked_remote.clear()
        self.checked_remote.add(iid)
        self._render_right()
        self.on_delete_remote()

    def on_toggle_remote(self):
        self.cfg["allow_remote_delete"] = self.allow_remote_var.get()
        save_config(self.cfg)
        self._update_buttons()
        if self.allow_remote_var.get():
            self.status_var.set("已开启远程删除：仅允许删除「已并入主分支」的远程分支，"
                                "确认时需输入 DELETE。")

    def on_delete_remote(self):
        if not self.repo:
            return
        if not self.allow_remote_var.get():
            messagebox.showinfo("提示", "请先勾选「允许删除远程」，远程删除默认关闭。")
            return
        rows = self._visible_checked_remote()
        if not rows:
            messagebox.showinfo("提示", "请先在右侧远程分支表勾选要删除的分支。")
            return
        if not self.cfg.get("main_branches", []):
            messagebox.showwarning(
                "需要先设置主分支",
                "远程删除依赖「是否已并入主分支」的判定。\n"
                "请先在顶部添加主分支（如 prod），再删除远程分支。")
            return

        allowed, skipped = [], []
        for b in rows:
            reason = self.protect_reason(b, for_remote=True)
            if reason:
                skipped.append((b, reason))
                continue
            # 未并入的也放行：由确认框标红 + 强制二次确认来兜底
            allowed.append(b)

        if not allowed:
            messagebox.showwarning(
                "无可删除的远程分支",
                "选中的分支全部被保护（主分支或命中保护白名单），未执行删除。\n\n%s"
                % format_branch_reasons(skipped, limit=12, indent="  "))
            return

        if len(self.remotes) > 1:
            from tkinter import simpledialog as sd
            target = sd.askstring("选择 remote",
                                  "存在多个远程：%s\n请输入目标 remote：" % ", ".join(self.remotes),
                                  parent=self)
            if not target or target.strip() not in self.remotes:
                return
            target = target.strip()
            allowed = [b for b in allowed if b.remote == target]
            if not allowed:
                messagebox.showinfo("提示", "选中的分支不属于 remote「%s」。" % target)
                return

        dlg = ConfirmDialog(self, "确认删除远程分支", allowed, skipped,
                            need_input="DELETE", is_remote=True)
        self.wait_window(dlg)
        if not dlg.result:
            return

        repo = self.repo
        items = [(b.remote, b.name, b.short, b.sha, self._merged_text(b)) for b in allowed]
        total = len(items)
        self._busy = True
        self.btn_del_remote.state(["disabled"])
        self.btn_del_local.state(["disabled"])
        self._show_progress("正在删除远程 0/%d …" % total, maximum=total)

        def work():
            ok, fail = [], []
            for idx, (remote, name, short, sha, merged_text) in enumerate(items, 1):
                self._report(idx - 1, "正在删除远程 %d/%d：%s" % (idx, total, short))
                try:
                    write_delete_log(repo.path, short, sha, "远程删除·" + merged_text)
                except Exception:
                    pass
                args = ["push", remote, "--delete"]
                if name.startswith("-"):
                    args.append(":refs/heads/" + name)
                else:
                    args.append(name)
                code, out, err = repo.run(args, timeout=PUSH_TIMEOUT)
                if code == 0:
                    ok.append(short)
                else:
                    fail.append((short, (err or out).strip()))
            return ok, fail

        def done(res):
            self._end_progress()
            self._busy = False
            if isinstance(res, Exception):
                self.status_var.set("远程删除失败：%s" % res)
                self._update_buttons()
                return
            ok, fail = res
            msg = ["远程删除：成功 %d 个，跳过 %d 个，失败 %d 个。"
                   % (len(ok), len(skipped), len(fail))]
            if fail:
                msg.append("\n失败 %d 个：" % len(fail))
                for n, e in fail[:10]:
                    msg.append("  %s  ——  %s" % (n, e.replace("\n", " ")[:200]))
            (messagebox.showwarning if fail else messagebox.showinfo)("远程删除结果", "\n".join(msg))
            self.status_var.set("远程删除完成：成功 %d 个，失败 %d 个" % (len(ok), len(fail)))
            self.refresh(False)

        self._bg(work, done)

    # -- 导出 --------------------------------------------------------------
    def on_export(self):
        if not self.visible_local and not self.visible_remote:
            messagebox.showinfo("提示", "当前视图为空，没有可导出的数据。")
            return
        default_dir = self.cfg.get("export_dir") or os.path.expanduser("~")
        default_name = "branches_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M")
        path = filedialog.asksaveasfilename(
            title="导出当前视图", defaultextension=".csv",
            initialdir=default_dir, initialfile=default_name,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        self.cfg["export_dir"] = os.path.dirname(path)
        save_config(self.cfg)

        header = ["类型", "分支名", "最后提交时间", "距今天数", "远程有同名",
                  "已并入主分支(命中列表)", "是否主分支", "是否当前分支"]
        rows = []
        for b in self.visible_local:
            rows.append(["本地", b.short, fmt_date(b.commit_ts), days_ago(b.commit_ts),
                         "有" if b.has_remote else "无",
                         "/".join(b.merged_into) if b.merged_into else "否",
                         "是" if b.is_main else "否",
                         "是" if b.is_current else "否"])
        for b in self.visible_remote:
            rows.append(["远程", b.short, fmt_date(b.commit_ts), days_ago(b.commit_ts),
                         "有" if b.has_local else "无",
                         "/".join(b.merged_into) if b.merged_into else "否",
                         "是" if b.is_main else "否", "否"])
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            return
        self.status_var.set("已导出 %d 行到 %s" % (len(rows), path))

    # -- 列宽 --------------------------------------------------------------
    def _save_column_widths(self):
        widths = {}
        for which, tv in (("local", self.left), ("remote", self.right)):
            for cid in tv["columns"]:
                try:
                    widths["%s.%s" % (which, cid)] = int(tv.column(cid, "width"))
                except Exception:
                    pass
        self.cfg["column_widths"] = widths

    def destroy(self):
        try:
            self._save_column_widths()
            self.cfg["sync_scroll"] = self.cfg.get("sync_scroll", False)
            save_config(self.cfg)
        except Exception:
            pass
        super().destroy()


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main():
    # 打包自检：GitBranchManager.exe --diag 会在程序目录写 diag.txt 后退出
    # 用于确认 frozen 模式下的配置/日志落点与 Tk 可用性
    if "--diag" in sys.argv:
        info = {
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
            "app_dir": APP_DIR,
            "config_path": CONFIG_PATH,
            "log_path": LOG_PATH,
            "python": sys.version,
            "tk": getattr(tk, "TkVersion", None),
            "cwd": os.getcwd(),
        }
        try:
            with open(os.path.join(APP_DIR, "diag.txt"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return

    app = App()
    try:
        app.mainloop()
    except Exception:
        msg = traceback.format_exc()
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass
        try:
            messagebox.showerror("程序异常", msg[-1500:])
        except Exception:
            pass


if __name__ == "__main__":
    main()
