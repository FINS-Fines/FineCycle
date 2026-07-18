"""interactive_runner.py — 纯交互 CLI，所有操作通过 DataCenter 门面执行。

用法：python -m interactive_runner
"""

from __future__ import annotations

import builtins
import os
import re
import shlex
import logging
from contextlib import contextmanager
from pathlib import Path
import sys
import subprocess
import time
import threading
import queue
import selectors
from dataclasses import dataclass
from getpass import getpass
from typing import Any, Optional

from core.datacenter import DataCenter

# ── Optional UI ────────────────────────────────────────────────────────────

try:
    from rich.console import Console as _RichConsole
    from rich.table import Table as _RichTable
    from rich import box as _rich_box
    from rich.markup import escape as _rich_escape
    from rich.panel import Panel as _RichPanel
    RICH_AVAILABLE = True
except Exception:
    _RichConsole = _RichTable = _rich_box = _rich_escape = _RichPanel = None  # type: ignore
    RICH_AVAILABLE = False

def _escape_console_text(text: str) -> str:
    if RICH_AVAILABLE and _rich_escape is not None:
        try: return _rich_escape(text or "")
        except Exception: return text or ""
    return text or ""

try:
    from prompt_toolkit import PromptSession as _PromptSession
    from prompt_toolkit.history import InMemoryHistory as _InMemoryHistory
    from prompt_toolkit.patch_stdout import patch_stdout as _patch_stdout
    from prompt_toolkit.completion import NestedCompleter as _NestedCompleter
    PROMPT_TOOLKIT_AVAILABLE = True
except Exception:
    _PromptSession = _InMemoryHistory = _patch_stdout = _NestedCompleter = None  # type: ignore
    PROMPT_TOOLKIT_AVAILABLE = False

def _strip_rich_markup(text: str) -> str:
    return re.sub(r"\[[^\]]+\]", "", text or "")

class _PlainConsole:
    def print(self, *a, **kw):
        sep, end = kw.get("sep"," "), kw.get("end","\n")
        builtins.print(sep.join(_strip_rich_markup(str(x)) for x in a), end=end)
    def rule(self, title=""):
        s = (title or "").strip(); bar = "=" * max(40, min(80, len(s)+10))
        builtins.print(f"\n{bar}");
        if s: builtins.print(_strip_rich_markup(s))
        builtins.print(bar)
    @contextmanager
    def status(self, msg): self.print(msg); yield

console = _RichConsole() if RICH_AVAILABLE else _PlainConsole()
def print(*a, **kw):  # type: ignore
    if "markup" not in kw: kw["markup"] = False
    return console.print(*a, **kw)

@dataclass
class SshAuth:
    username: str; password: str; port: int = 22

# ── InteractivePlatform ────────────────────────────────────────────────────

class InteractivePlatform:
    """交互式部署平台。所有操作通过 self.dc (DataCenter) 执行。"""

    def __init__(self) -> None:
        self.dc = DataCenter("InteractivePlatform")
        self.ssh_auth: Optional[SshAuth] = None
        self._cz_monitor_thread: Optional[threading.Thread] = None
        self._cz_monitor_started = False
        self._cz_seen: set[str] = set()
        self._task_done_events: "queue.Queue[tuple[str, bool]]" = queue.Queue()

        missing = []
        if not RICH_AVAILABLE: missing.append("rich")
        if not PROMPT_TOOLKIT_AVAILABLE: missing.append("prompt_toolkit")
        if PROMPT_TOOLKIT_AVAILABLE and _NestedCompleter is None: missing.append("NestedCompleter")
        if missing: self._print_info(f"可选依赖未安装({','.join(missing)})，回退纯文本；安装: pip install rich prompt_toolkit")

    # ── UI helpers ──────────────────────────────────────────────────────
    def _ph(self, t):
        s = str(t or "").strip()
        if s: console.rule(f"[bold]{_escape_console_text(s)}[/bold]")
        else: console.rule(" ")
    def _ps(self, m): console.print(f"[bold green]OK {_escape_console_text(str(m or ''))}[/bold green]")
    def _pe(self, m): console.print(f"[bold red]ERR {_escape_console_text(str(m or ''))}[/bold red]")
    def _pi(self, m): console.print(f"[cyan]... {_escape_console_text(str(m or ''))}[/cyan]")
    def _pstep(self, m): console.print(f"[yellow]> {_escape_console_text(str(m or ''))}...[/yellow]")
    def _psuggest(self, e):
        t = str(e or "").lower()
        tips = {"authentication failed":"检查SSH密码/密钥","permission denied":"检查SSH用户/权限/sudo",
                "no route to host":"检查IP/路由/网关","connection timed out":"检查网络连通性/防火墙/SSH端口",
                "connection refused":"确认SSH服务已启动","host key verification failed":"清理known_hosts或关闭StrictHostKeyChecking"}
        for k,v in tips.items():
            if k in t: console.print(f"  -> {v}"); return

    def _read_line(self, prompt):
        sel = selectors.DefaultSelector()
        try:
            sel.register(sys.stdin, selectors.EVENT_READ)
            sys.stdout.write(prompt); sys.stdout.flush()
            while True:
                rd = False
                while True:
                    try: self._task_done_events.get_nowait(); rd = True
                    except queue.Empty: break
                if rd: sys.stdout.write("\n"+prompt); sys.stdout.flush()
                if sel.select(timeout=0.2):
                    l = sys.stdin.readline()
                    if l == "": raise EOFError
                    return l.rstrip("\n")
        finally:
            try: sel.close()
            except Exception: pass

    @staticmethod
    def _ask(p, default=None):
        return input(f"{p}: ") if default is None else (input(f"{p} [默认: {default}]: ") or str(default))
    @staticmethod
    def _aski(p, default=0, min_v=None):
        while True:
            r = input(f"{p} [默认: {default}]: ").strip()
            if not r: v = default
            else:
                try: v = int(r)
                except ValueError: console.print("[bold red]请输入整数[/bold red]"); continue
            if min_v is not None and v < min_v: console.print(f"[bold red]请输入>={min_v}[/bold red]"); continue
            return v
    @staticmethod
    def _askb(p, default=False):
        d = "y" if default else "n"
        while True:
            r = input(f"{p} [y/n，默认: {d}]: ").strip().lower()
            if not r: return default
            if r in {"y","yes","true","1"}: return True
            if r in {"n","no","false","0"}: return False
            console.print("[bold red]请输入 y 或 n[/bold red]")
    @staticmethod
    def _askcsv(p, default=""):
        r = input(f"{p} [默认: {default}]: ").strip() or str(default)
        return {x.strip() for x in (r or "").split(",") if x.strip()}

    @staticmethod
    def _prompt(text, default=None):
        return input(f"{text}: ") if default is None else (input(f"{text} [默认: {default}]: ") or str(default))
    @staticmethod
    def _prompti(text, default):
        while True:
            r = input(f"{text} [默认: {default}]: ").strip()
            if not r: return int(default)
            try: return int(r)
            except ValueError: console.print("[bold red]请输入整数[/bold red]")
    @staticmethod
    def _promptb(text, default):
        d = "y" if default else "n"
        while True:
            r = input(f"{text} [y/n，默认: {d}]: ").strip().lower()
            if not r: return default
            if r in {"y","yes","true","1"}: return True
            if r in {"n","no","false","0"}: return False
            console.print("[bold red]请输入 y 或 n[/bold red]")

    @staticmethod
    def _fmt_dur(s):
        try: s = int(max(0,float(s)))
        except: return "-"
        h,m,ss = s//3600, (s%3600)//60, s%60
        if h: return f"{h}h{m:02d}m{ss:02d}s"
        if m: return f"{m}m{ss:02d}s"
        return f"{ss}s"

    def _setup_logger(self, prefix):
        d = Path(__file__).resolve().parent / "logs"; d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S"); p = d / f"manual-{prefix}-{ts}.log"
        L = logging.getLogger(f"manual.{prefix}.{ts}"); L.setLevel(logging.INFO); L.propagate = False
        L.handlers = []
        h = logging.FileHandler(str(p), encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        L.addHandler(h)
        return L, str(p)

    # ── Clonezilla helpers ──────────────────────────────────────────────
    def _cz_prompt_ip(self, iso, default=""):
        while True:
            v = self._prompt(f"镜像 {iso} 绑定的 IP 地址", default=default).strip()
            if self.dc.is_ipv4(v): return v
            self._pe("请输入合法的 IPv4 地址")

    def _cz_choose(self, purpose, only_free=True, preselected=None):
        choices = self.dc.get_free_clonezilla_images() if only_free else [
            (k,v) for k,v in (self.dc.clonezilla_images or {}).items() if v is not None]
        if not choices: self._pe(f"当前没有{'空闲' if only_free else '可用'}镜像可选"); self._pi("可执行 cz-register 扫描并注册镜像"); return None
        cm = {str(k): v for k,v in choices}
        if preselected:
            s = cm.get(str(preselected).strip())
            if s is None: self._pe(f"指定镜像不可用：{preselected}"); return None
            return str(preselected).strip(), s
        self._ph(f"[Clonezilla] {purpose}")
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
            t.add_column("#",no_wrap=True); t.add_column("ISO Name"); t.add_column("Node",no_wrap=True)
            t.add_column("IP",no_wrap=True); t.add_column("Status",no_wrap=True)
            for i,(iso,item) in enumerate(choices,1):
                st = "[bold red]BUSY[/bold red]" if str(getattr(item,"status","free")).lower()=="busy" else "[bold green]FREE[/bold green]"
                t.add_row(str(i), _escape_console_text(str(iso)),
                          _escape_console_text(str(getattr(item,"node","-"))),
                          _escape_console_text(str(getattr(item,"ip","-"))), st)
            console.print(t)
        else:
            for i,(iso,item) in enumerate(choices,1):
                s = getattr(item,"status","free"); ip = getattr(item,"ip","-"); node = getattr(item,"node","-")
                self._pi(f"{i}. {iso} node={node} ip={ip} status={str(s).upper()}")
        while True:
            r = self._ask("请选择 Clonezilla 镜像（输入序号或 ISO 名称）", default="1").strip() or "1"
            if r.isdigit():
                idx = int(r)-1
                if 0 <= idx < len(choices): return choices[idx]
                self._pe("序号超出范围"); continue
            s = cm.get(r)
            if s is not None: return r, s
            self._pe(f"未找到镜像：{r}")

    def _cz_register_isos(self, node, iso_names):
        if not iso_names: return 0
        self._pi(f"发现 Clonezilla 镜像：{', '.join(iso_names)}")
        cnt = 0
        for iso in iso_names:
            if not self._askb(f"是否注册 Clonezilla 镜像 {iso}?", default=False): continue
            ip = self._cz_prompt_ip(iso, default=(self.dc.parse_clonezilla_ip(iso) or ""))
            try: self.dc.register_clonezilla_iso(iso, node, ip); cnt += 1; self._ps(f"[Clonezilla] 已注册：{iso} -> {ip}")
            except Exception as e: self._pe(f"[Clonezilla] 注册失败：{e}")
        return cnt

    def _cz_start_monitor(self):
        if self._cz_monitor_started or self.dc.master_server is None: return
        self._cz_monitor_started = True
        def w():
            while True:
                try:
                    if self.dc.master_server is not None:
                        for iso in self.dc.scan_and_register_clonezilla_isos():
                            if iso not in self._cz_seen:
                                self._cz_seen.add(iso)
                                cz = (self.dc.clonezilla_images or {}).get(iso)
                                self._ps(f"[后台通知] 自动发现并注册了 Clonezilla 镜像: {iso} -> {getattr(cz,'ip','?') if cz else '?'}")
                except Exception: pass
                time.sleep(30)
        self._cz_monitor_thread = threading.Thread(target=w, name="cz-monitor", daemon=True)
        self._cz_monitor_thread.start()

    # ── Tab completer ───────────────────────────────────────────────────
    def _get_completer(self):
        if not (PROMPT_TOOLKIT_AVAILABLE and _NestedCompleter is not None): return None
        try:
            hids = sorted((self.dc.hosts or {}).keys())
            imgs = self.dc.get_images_display(); iids = sorted([x["id"] for x in imgs])
            run = self.dc.get_running_tasks_display(); pend = self.dc.get_pending_tasks_display()
            pids = sorted([t["task_id"] for t in pend]); rids = sorted([t["task_id"] for t in run])
            bids = sorted([h["id"] for h in self.dc.get_hosts_display() if h["is_busy"]])
            fcz = sorted([c["iso_name"] for c in self.dc.get_clonezilla_list() if c["status"]=="free"])
            def L(xs): return {str(x):None for x in xs}
            return _NestedCompleter.from_nested_dict({
                "help":None,"exit":None,"list":None,"tasks":None,"vms":None,"refresh":None,"clear":None,
                "auto":None,"cz-list":None,"cz-register":None,
                "register-baremetal":L(fcz) if fcz else None,"bmreg":L(fcz) if fcz else None,
                "add-baremetal":L(fcz) if fcz else None,
                "cancel":L(pids),"logs":L(sorted(set(pids+rids))),"release":L(bids),
                "check":L(hids),
                "info":{"host":L(hids),"image":L(iids)},
                "tag":L(sorted(set(hids)|set(iids))),
                "deploy":{v:L(hids) for v in iids},
                "template":L(iids),"clone-template":L(iids),
            })
        except Exception: return None

    # ── Stage 1: Bootstrap ──────────────────────────────────────────────
    def bootstrap_and_discovery(self): self._step2()

    def _step2(self):
        self._ph("初始化向导：连接集群并注册资源")
        self._pstep("连接 Master Server")
        mip = self._prompt("Master Server IP/Hostname")
        pve_u = self._prompt("PVE API 用户名", default="root@pam")
        while True:
            pve_p = getpass("PVE API 密码（必填）：")
            if (pve_p or "").strip(): break
            self._pe("PVE API 密码不能为空")
        pp = 8006; pvs = False
        if self._promptb("是否配置 PVE API 高级参数", default=False):
            pp = self._prompti("PVE API 端口", default=8006)
            pvs = self._promptb("PVE API verify_ssl", default=False)
        su = self._prompt("SSH 用户名", default="root"); sp = self._prompti("SSH 端口", default=22)
        while True:
            spw = getpass("SSH 密码（必填）：")
            if (spw or "").strip(): break
            self._pe("SSH 密码不能为空")
        self.ssh_auth = SshAuth(username=su, password=spw, port=sp)
        self._pstep("连接 Master（SSH + PVE API）")
        try:
            with console.status("正在连接 Master..."):
                master = self.dc.bootstrap_master(mip, ssh_username=su, ssh_password=spw, ssh_port=sp,
                                                   pve_api_username=pve_u, pve_api_password=pve_p,
                                                   pve_api_port=pp, pve_verify_ssl=pvs)
        except Exception as e: raise RuntimeError(f"连接 Master 失败：{e}") from e
        self._ps(f"Master 已连接: {master.hostname} ({mip})")

        # Clonezilla ISOs
        try: cz_isos = self.dc.scan_clonezilla_isos(master.pve_node_name)
        except Exception as e: self._pe(f"[Clonezilla] 扫描失败：{e}"); cz_isos = []
        if cz_isos: self._cz_seen.update(cz_isos); self._cz_register_isos(master.pve_node_name, cz_isos)

        # 集群节点
        self._pstep("扫描集群节点")
        with console.status("正在扫描集群节点..."):
            try: nodes = self.dc.get_nodes()
            except Exception as e: raise RuntimeError(f"扫描集群节点失败：{e}") from e
        if not nodes: raise RuntimeError("未发现任何节点")
        self._ph("节点发现结果")
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
            t.add_column("#",justify="right",style="dim"); t.add_column("NODE",style="bold"); t.add_column("STATUS")
            for i,n in enumerate(nodes,1):
                s = str(n.get("status") or "").strip() or "-"; sl = s.lower()
                st = "bold green" if sl in {"online","ok"} else "bold red" if sl in {"offline","down"} else "yellow"
                t.add_row(str(i), str(n.get("node") or "").strip() or "-", f"[{st}]{s}[/{st}]")
            console.print(t)
        else:
            for i,n in enumerate(nodes,1): print(f"{i:<4} {str(n.get('node') or '').strip() or '-':<24} {str(n.get('status') or '').strip() or '-':<10}")

        # 交互式注册
        self._ph("交互式注册")
        vmc: dict = {}

        def reg_vms(nn, vms):
            if not vms: self._pi(f"[Image] 未发现节点 {nn} 下的虚拟机"); return
            self._ph(f"镜像候选（节点 {nn}）")
            if RICH_AVAILABLE and _RichTable is not None:
                t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
                t.add_column("Status",no_wrap=True); t.add_column("VMID",no_wrap=True); t.add_column("Name"); t.add_column("Node",no_wrap=True)
                for vm in vms:
                    vid = str(vm.get("vmid") or "").strip()
                    if not vid: continue
                    nm = str(vm.get("name") or "").strip() or f"vm-{vid}"
                    st = "[bold green]registered[/bold green]" if self.dc.is_image_registered(vid) else "[dim]new[/dim]"
                    t.add_row(st, _escape_console_text(vid), _escape_console_text(nm), _escape_console_text(str(nn)))
                console.print(t)
            else:
                for vm in vms:
                    vid = str(vm.get("vmid") or "").strip(); nm = str(vm.get("name") or "").strip() or f"vm-{vid}"
                    self._pi(f"VM {vid} ({nm})")
            if not self._askb(f"是否为节点 {nn} 注册 VM 镜像?", default=False): return
            for vm in vms:
                vid = str(vm.get("vmid") or "").strip()
                if not vid: continue
                nm = str(vm.get("name") or "").strip() or f"vm-{vid}"
                if not self._askb(f"是否将 VM {vid} ({nm}) 注册为可调度镜像?", default=False): continue
                if self.dc.is_image_registered(vid): self._pi(f"[Image] 已存在镜像 {vid}，跳过"); continue
                tags = self._askcsv('镜像标签 Tags (如 "navigation, slam")', default="")
                mr = self._aski("最低硬件需求 Min RAM (GB)", default=4, min_v=1)
                ng = self._askb("Need GPU?", default=False)
                self.dc.create_and_register_image(
                    vid, nm, "pve", int(vid), str(nn),
                    is_template=bool(vm.get("template")),
                    tags=sorted(tags), min_ram=int(mr), need_gpu=ng)
                self._ps(f"[Image] 已注册镜像：{vid} labels={sorted(tags)}")

        mn = str(master.pve_node_name or "master")
        if self._askb(f"是否注册 master 节点 {mn} 的 VM?", default=False):
            try: vmc[mn] = self.dc.list_node_vms(mn)
            except Exception as e: self._pe(f"[Image] 获取 master 节点 {mn} 的 VM 列表失败：{e}"); vmc[mn] = []
            reg_vms(mn, vmc[mn])

        for item in nodes:
            node = str(item.get("node") or "").strip()
            st = str(item.get("status") or "").strip()
            if not node or node == master.pve_node_name: continue
            self._ph(f"节点注册：{node} (status={st})")
            role = self._prompt("该节点是 [S]erver 还是 [R]obot？(输入 i 跳过)", default="i").strip().lower()
            if role in {"i","skip"}: self._pi("[Node] 已跳过"); continue
            addr = self.dc.resolve_node_ssh_ip(node)
            if not addr: addr = self._prompt("无法自动解析节点 IP，请手动输入可 SSH 的 IP/Hostname", default="").strip()
            if not addr: self._pe("[Node] 未提供可 SSH 地址，已跳过"); continue
            self._pi(f"[Node] SSH 地址：{addr}")
            try:
                if role in {"s","server"}:
                    host = self.dc.create_and_register_server_host(
                        node, addr, node, node,
                        ssh_username=su, ssh_password=spw, ssh_port=sp)
                elif role in {"r","robot"}:
                    cc = self._aski("cpu_cores", default=4, min_v=1)
                    rg = self._aski("ram_gb", default=4, min_v=1)
                    hg = self._askb("has_gpu", default=False)
                    lbs = self._askcsv('labels (逗号分隔)', default="")
                    caps = {"cpu_cores": cc, "ram_gb": rg, "has_gpu": hg}
                    host = self.dc.create_and_register_pve_host(
                        node, addr, node, node,
                        ssh_username=su, ssh_password=spw, ssh_port=sp,
                        labels=lbs, capabilities=caps)
                else: self._pe("[Node] 输入无效，仅支持 S/R/i，已跳过"); continue
            except Exception as e: self._pe(f"[Node] 注册失败：{e}"); continue
            if node not in vmc:
                try: vmc[node] = self.dc.list_node_vms(node)
                except Exception as e: self._pe(f"[Image] 获取节点 {node} 的 VM 列表失败：{e}"); vmc[node] = []
            reg_vms(node, vmc[node])
        self._ps("初始化完成"); self._pi("输入 help 查看可用指令；输入 list 查看注册资源")

    # ── Stage 2: Command loop ───────────────────────────────────────────
    def command_loop(self): self._step3()

    def _step3(self):
        self._cz_start_monitor()
        try:
            self.dc.scheduler._on_task_done = lambda tid, ok: self._task_done_events.put((tid, ok))
            self._ps("自动化调度引擎已就绪（Scheduler 内置派发）")
        except Exception as e: self._pi(f"调度引擎初始化失败（auto 将不可用）：{e}")

        session = None
        if PROMPT_TOOLKIT_AVAILABLE and _PromptSession is not None and _InMemoryHistory is not None:
            session = _PromptSession(history=_InMemoryHistory(), complete_while_typing=True)

        while True:
            try:
                if session is not None:
                    c = self._get_completer()
                    raw = session.prompt("platform> ", completer=c) if _patch_stdout is None else (
                        _patch_stdout().__enter__() or session.prompt("platform> ", completer=c))
                else: raw = self._read_line("platform> ")
                raw = (raw or "").strip()
            except (EOFError, KeyboardInterrupt): print("\n[Exit] 用户退出"); return
            if not raw: continue
            try: parts = shlex.split(raw)
            except ValueError as e: self._pe(f"[CLI] 解析命令失败：{e}"); continue
            cmd, args = parts[0].lower(), parts[1:]
            try:
                if cmd in {"exit","quit"}: print("[Exit] Bye"); return
                if cmd in {"help","h","?"}: self._print_help()
                elif cmd == "list": self._cmd_list()
                elif cmd == "tasks": self._cmd_tasks()
                elif cmd == "logs": self._cmd_logs(args)
                elif cmd == "check": self._cmd_check(args)
                elif cmd == "tag": self._cmd_tag(args)
                elif cmd == "refresh": self._cmd_refresh()
                elif cmd == "clear": self._cmd_clear()
                elif cmd == "cancel": self._cmd_cancel(args)
                elif cmd == "release": self._cmd_release(args)
                elif cmd == "info": self._cmd_info(args)
                elif cmd == "vms": self._cmd_vms()
                elif cmd == "cz-list": self._cmd_cz_list()
                elif cmd == "cz-register": self._cmd_cz_register()
                elif cmd == "deploy": self._cmd_deploy(args)
                elif cmd == "template": self._cmd_template(args)
                elif cmd == "clone-template": self._cmd_clone_template(args)
                elif cmd in {"register-baremetal","bmreg","add-baremetal"}: self._cmd_register_baremetal(args)
                elif cmd == "resize": self._cmd_resize(args)
                elif cmd == "auto": self._cmd_auto()
                else: self._pe("[CLI] 未知命令。输入 help 查看帮助。")
            except Exception as e: self._pe(f"[CLI] 命令执行异常：{e}")

    # ── Command implementations (thin wrappers → DataCenter) ────────────

    def _cmd_deploy(self, args):
        if len(args)!=2: self._pi("用法：deploy <vmid> <host_id>"); return
        try: vmid = int(args[0])
        except ValueError: self._pe("[DEPLOY] vmid 必须是整数"); return
        kind = self.dc.get_host_kind(args[1])
        L, lp = self._setup_logger("deploy"); self._pstep("任务已开始"); self._pi(f"日志写入: {lp}")
        try:
            if kind == "BareMetal":
                # 裸机目标 → 交互式选择 Clonezilla 镜像
                ch = self._cz_choose("选择源端 Clonezilla 镜像（发送端）", only_free=True)
                if ch is None: return
                siso, _ = ch
                self.dc.deploy_baremetal(str(vmid), args[1], siso, logger=L)
            else:
                self.dc.deploy_pve(vmid, args[1], logger=L)
            self._ps("[DEPLOY] 部署完成")
        except Exception as e: self._pe(f"[DEPLOY] {e}"); self._psuggest(e)

    def _cmd_template(self, args):
        if len(args)!=1: self._pi("用法：template <vmid>"); return
        try: vmid = int(args[0])
        except ValueError: self._pe("[template] vmid 必须是整数"); return
        L, lp = self._setup_logger("template"); self._pstep("任务已开始"); self._pi(f"日志写入: {lp}")
        try:
            n = self.dc.template_vm(vmid, logger=L)
            self._ps(f"[template] VM {vmid} 已经模板化" if not n else f"[template] VM {vmid} 已模板化")
        except Exception as e: self._pe(f"[template] {e}"); self._psuggest(e)

    def _cmd_clone_template(self, args):
        if len(args) not in {3,4}: self._pi("用法：clone-template <t_vmid> <n_vmid> <target_node> [new_name]"); return
        try: tv = int(args[0]); nv = int(args[1])
        except ValueError: self._pe("[clone-template] vmid 都必须是整数"); return
        nn = args[3] if len(args)==4 else None
        L, lp = self._setup_logger("clone-template"); self._pstep("任务已开始"); self._pi(f"日志写入: {lp}")
        try:
            nid = self.dc.clone_template(tv, nv, args[2], nn, logger=L)
            self._ps(f"[clone-template] 已从模板 {tv} 克隆 VM {nv} (image_id={nid}) 到节点 {args[2]}")
        except Exception as e: self._pe(f"[clone-template] {e}"); self._psuggest(e)

    def _cmd_register_baremetal(self, args):
        hid = self._ask("裸机 id", default="").strip()
        if not hid: self._pe("[register-baremetal] 裸机 id 不能为空"); return
        if hid in (self.dc.hosts or {}): self._pe(f"[register-baremetal] 已存在同名主机：{hid}"); return
        td = self._ask("裸机磁盘名（如 sda/nvme0n1）", default="").strip()
        if not td: self._pe("[register-baremetal] 裸机磁盘名 不能为空"); return
        ch = self._cz_choose("为裸机选择目标端 Clonezilla 镜像", only_free=True, preselected=args[0] if args else None)
        if ch is None: return
        iso, item = ch
        if not (getattr(item,"ip","") if hasattr(item,"ip") else ""): self._pe(f"[register-baremetal] 镜像 {iso} 缺少 IP"); return
        if not self._askb("确认注册此裸机", default=True): self._pi("[register-baremetal] 用户取消"); return
        try:
            host = self.dc.register_baremetal(hid, td, iso)
            self._ps(f"[register-baremetal] 已注册裸机：{hid} (ip={host.ip})")
        except Exception as e: self._pe(f"[register-baremetal] {e}")

    def _cmd_resize(self, args):
        """调试用：直接调整 VM 磁盘大小。"""
        if len(args) < 3: self._pi("用法：resize <vmid> <disk_name> <size_gb>"); return
        try: vmid = int(args[0])
        except ValueError: self._pe("[resize] vmid 必须是整数"); return
        dn = args[1]
        try: ns = int(args[2])
        except ValueError: self._pe("[resize] size_gb 必须是整数"); return

        disks = self.dc.master_server.get_vm_disk_info(vmid) if self.dc.master_server else []
        cur = next((d['size_gb'] for d in disks if d['disk_name'] == dn), 0)
        self._pi(f"VM {vmid} 磁盘 {dn}: {cur}G -> {ns}G ({'扩容' if ns>cur else '缩容' if ns<cur else '不变'})")

        cz_iso = ""; cz_ip = ""
        if ns < cur:
            ch = self._cz_choose("选择 Clonezilla 镜像用于修复分区表", only_free=True)
            if ch is None: return
            cz_iso, item = ch
            cz_ip = getattr(item, 'ip', '') if hasattr(item, 'ip') else ''

        L, lp = self._setup_logger("resize"); self._pstep("任务已开始"); self._pi(f"日志写入: {lp}")
        try:
            self.dc.resize_disk(vmid, dn, ns, clonezilla_iso=cz_iso, clonezilla_ip=cz_ip, logger=L)
            self._ps(f"[resize] 磁盘 {dn} 已调整到 {ns}G")
        except Exception as e: self._pe(f"[resize] {e}")

    def _cmd_auto(self):
        tag = self._ask("Target Image Tag?（为空代表任意）", default="").strip()
        rg = self._askb("Require GPU?", default=False)
        mr = self._aski("Min RAM? (GB)", default=4, min_v=1)
        pri = self._ask("Priority? (A/B/C)", default="C").strip().upper()
        if pri not in {"A","B","C"}: pri = "C"
        try:
            tid = self.dc.submit_auto_task(tag=tag, require_gpu=rg, min_ram=mr, priority=pri)
            self._ps(f">> 自动部署任务 {tid} 已提交。日志：tail -f logs/{tid}.log")
        except Exception as e: self._pe(f"[CLI] auto 指令执行失败：{e}")

    def _cmd_release(self, args):
        if len(args)!=1: self._pi("用法：release <host_id>"); return
        hid = args[0]
        if hid not in (self.dc.hosts or {}): self._pe(f"[release] 未找到主机：{hid}"); return
        kind = self.dc.get_host_kind(hid)
        L, lp = self._setup_logger("release"); self._pstep("任务已开始"); self._pi(f"日志写入: {lp}")
        try:
            if kind == "BareMetal":
                dn = self._ask("磁盘名称 (如 virtio0)", default="virtio0").strip()
                bm_sz = self._aski("裸机磁盘大小(GB)", default=64, min_v=1)
                ch = self._cz_choose("选择 Clonezilla 镜像", only_free=True)
                if ch is None: return
                iso, item = ch
                ip = getattr(item, 'ip', '') if hasattr(item, 'ip') else ''
                ok = self.dc.release_baremetal(
                    hid, disk_name=dn, baremetal_disk_gb=bm_sz,
                    clonezilla_iso=iso, clonezilla_ip=ip, logger=L)
            else:
                ok = self.dc.release_pve(hid, logger=L)
            if ok: self._ps("主机及对应镜像已释放，重置为空闲状态。")
            else: self._pe("[release] 释放失败。")
        except Exception as e: self._pe(f"[release] {e}")

    def _cmd_cancel(self, args):
        if len(args)!=1: self._pi("用法：cancel <task_id>"); return
        try:
            if self.dc.cancel_task(args[0]): self._ps(f"任务 {args[0]} 已从等待队列中移除。")
            else: self._pe(f"任务 {args[0]} 正在运行中，不支持取消。")
        except Exception as e: self._pe(f"[cancel] {e}")

    def _cmd_tag(self, args):
        if len(args)!=2: self._pi("用法：tag <id> <+label|-label>"); return
        try: t, l = self.dc.tag_entity(args[0], args[1]); self._ps(f"[tag] {t} {args[0]} labels={l}")
        except Exception as e: self._pe(f"[tag] {e}")

    # ── Display-only commands ───────────────────────────────────────────

    def _cmd_list(self):
        hosts = self.dc.get_hosts_display()
        if not hosts: self._pi("[List] 当前没有已注册主机"); return
        self._ph("[List] 已注册主机")
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
            t.add_column("Status",no_wrap=True); t.add_column("Kind",no_wrap=True); t.add_column("ID",no_wrap=True)
            t.add_column("IP"); t.add_column("Hostname"); t.add_column("Labels"); t.add_column("Task Info")
            kc = {"PVE":"bold magenta","Server":"bold cyan","BareMetal":"bold yellow"}
            for h in hosts:
                ks = f"[{kc.get(h['kind'],'bold')}]{h['kind']}[/{kc.get(h['kind'],'bold')}]"
                ss = "[bold red]BUSY[/bold red]" if h["is_busy"] else "[bold green]FREE[/bold green]"
                t.add_row(ss, ks, _escape_console_text(h["id"]), _escape_console_text(h["ip"]),
                          _escape_console_text(h["name"]), _escape_console_text(",".join(h["labels"]) if h["labels"] else "-"),
                          _escape_console_text(h["task_info"]))
            console.print(t)
        else:
            for h in hosts: print(f"  {'BUSY' if h['is_busy'] else 'FREE':<6} {h['kind']:<10} {h['id']:<18} {h['ip']:<18} {h['name']}")

    def _cmd_tasks(self):
        run = self.dc.get_running_tasks_display(); pend = self.dc.get_pending_tasks_display()
        self._ph("[Tasks] 任务状态"); now = time.time()
        if RICH_AVAILABLE and _RichTable is not None:
            console.print("[bold]\nRunning[/bold] 正在运行的任务：")
            if not run: console.print("  - (none)")
            else:
                t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
                t.add_column("Task ID",no_wrap=True); t.add_column("Image",no_wrap=True)
                t.add_column("Host",no_wrap=True); t.add_column("Duration",no_wrap=True)
                for r in run: t.add_row(_escape_console_text(str(r.get("task_id") or "")),
                    _escape_console_text(str(r.get("image_id") or "-")),
                    _escape_console_text(str(r.get("host_id") or "-")),
                    _escape_console_text(self._fmt_dur(now - float(r.get("allocated_at") or 0))))
                console.print(t)
            console.print("[bold]\nPending[/bold] 等待中的任务：")
            if not pend: console.print("  - (none)")
            else:
                t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
                t.add_column("Task ID",no_wrap=True); t.add_column("Priority",no_wrap=True); t.add_column("Requirements")
                for p in pend: t.add_row(_escape_console_text(str(p.get("task_id") or "")),
                    _escape_console_text(str(p.get("priority") or "-")),
                    _escape_console_text(DataCenter.format_task_requirements_summary(p.get("requirements"))))
                console.print(t)
        else:
            print("\n[Running]:")
            for r in run: print(f"  - {r.get('task_id')} image={r.get('image_id')} host={r.get('host_id')} dur={self._fmt_dur(now-float(r.get('allocated_at') or 0))}")
            print("\n[Pending]:")
            for p in pend: print(f"  - {p.get('task_id')} prio={p.get('priority')} req={DataCenter.format_task_requirements_summary(p.get('requirements'))}")

    def _cmd_logs(self, args):
        if not args: self._pi("用法：logs <task_id> [n]"); return
        tid = args[0]; n = 20
        if len(args)>=2:
            try: n = int(args[1])
            except: self._pe("[logs] n 必须是整数"); return
        n = max(1, min(500, n))
        lp = Path(__file__).resolve().parent / "logs" / f"{tid}.log"
        if not lp.exists(): self._pe(f"[logs] 未找到日志文件：{lp}"); return
        try:
            from collections import deque
            tail = deque(maxlen=n)
            with open(lp, encoding="utf-8", errors="replace") as f:
                for line in f: tail.append(line.rstrip("\n"))
            content = "\n".join(tail)
        except Exception as e: self._pe(f"[logs] 读取失败：{e}"); return
        title = f"logs/{tid}.log (tail {n})"
        if RICH_AVAILABLE:
            try:
                from rich.text import Text
                txt = Text(content)
                txt.highlight_regex(r"\[ERROR\]", style="bold red"); txt.highlight_regex(r"\[WARN\]", style="bold yellow")
                txt.highlight_regex(r"\[INFO\]", style="dim"); txt.highlight_regex(r"Traceback \(most recent call last\):", style="bold red")
                console.print(_RichPanel(txt, title=_escape_console_text(title), border_style="cyan") if _RichPanel else txt)
            except Exception: console.rule(_escape_console_text(title)); print(content)
        else: print(f"\n== {title} =="); print(content)

    def _cmd_check(self, args):
        if len(args)!=1: self._pi("用法：check <host_id>"); return
        h = (self.dc.hosts or {}).get(args[0])
        if h is None: self._pe(f"[check] 未找到主机：{args[0]}"); return
        ip = str(getattr(h,"ip","") or "").strip()
        if not ip: self._pe(f"[check] 主机 {args[0]} 未配置 ip"); return
        results = []
        try:
            cmd = ["ping","-n","1","-w","1000",ip] if os.name=="nt" else ["ping","-c","1","-W","1",ip]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            ok = p.returncode==0; detail = "ok" if ok else (p.stderr.strip() or p.stdout.strip() or "failed")
            results.append(("Ping",ok,detail))
        except subprocess.TimeoutExpired: results.append(("Ping",False,"timeout"))
        except Exception as e: results.append(("Ping",False,str(e)))
        try:
            fn = getattr(h,"execute_ssh",None) if hasattr(h,"execute_ssh") else None
            if fn: fn("true"); results.append(("SSH",True,"connected"))
            else: results.append(("SSH",False,"no execute_ssh"))
        except Exception as e: results.append(("SSH",False,str(e)))
        self._ph(f"[Check] Host {args[0]} ({ip})")
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
            t.add_column("Step",no_wrap=True); t.add_column("Result",no_wrap=True); t.add_column("Detail")
            for s,ok,d in results:
                t.add_row(_escape_console_text(s), "[bold green]PASS[/bold green]" if ok else "[bold red]FAIL[/bold red]", _escape_console_text(d))
            console.print(t)
        else:
            for s,ok,d in results: print(f"- {'[PASS]' if ok else '[FAIL]'} {s}: {d}")

    def _cmd_info(self, args):
        if len(args)!=2: self._pi("用法：info host <host_id> | info image <image_id>"); return
        t = str(args[0] or "").strip().lower()
        if t=="host": self._cmd_info_host(args[1])
        elif t=="image": self._cmd_info_image(args[1])
        else: self._pi("用法：info host <host_id> | info image <image_id>")

    def _cmd_info_host(self, hid):
        try: self.dc.refresh_image_locations()
        except: pass
        h = (self.dc.hosts or {}).get(hid)
        if h is None: self._pe(f"[info] 未找到主机：{hid}"); return
        k = self.dc.get_host_kind(hid)
        hd = {x["id"]:x for x in self.dc.get_hosts_display()}.get(hid,{})
        busy = hd.get("is_busy",False) if hd else False
        lbs = hd.get("labels",[]) if hd else []; ti = hd.get("task_info","-") if hd else "-"
        ip = str(getattr(h,"ip","") or "-"); hn = str(getattr(h,"hostname","") or "-")
        pn = str(getattr(h,"pve_node_name","") or "-")
        self._ph(f"[Info] Host {hid}")
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_header=False)
            t.add_column("k",style="dim",no_wrap=True); t.add_column("v")
            t.add_row("ID", _escape_console_text(hid)); t.add_row("Kind", _escape_console_text(str(k)))
            t.add_row("Labels", _escape_console_text(",".join(lbs) if lbs else "-"))
            t.add_row("IP", _escape_console_text(ip)); t.add_row("Hostname", _escape_console_text(hn))
            t.add_row("PVE Node", _escape_console_text(pn))
            t.add_row("Scheduler", "[bold red]BUSY[/bold red]" if busy else "[bold green]FREE[/bold green]")
            t.add_row("Task", _escape_console_text(ti))
            console.print(t)
        else:
            print(f"  ID:{hid} Kind:{k} IP:{ip} Hostname:{hn} PVE:{pn} Scheduler:{'BUSY' if busy else 'FREE'} Task:{ti} Labels:{lbs}")

    def _cmd_info_image(self, iid):
        try: self.dc.refresh_image_locations()
        except: pass
        imgs = {x["id"]:x for x in self.dc.get_images_display()}
        img = imgs.get(iid)
        if img is None: self._pe(f"[info image] 未找到镜像：{iid}"); return
        self._ph(f"[Info] Image {iid}")
        st = "[bold red]BUSY[/bold red]" if (RICH_AVAILABLE and img["status"]=="busy") else "[bold green]FREE[/bold green]" if RICH_AVAILABLE else img["status"].upper()
        ts = "[bold blue]YES[/bold blue]" if (RICH_AVAILABLE and img["is_template"]) else ("YES" if img["is_template"] else "-")
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_header=False)
            t.add_column("k",style="dim",no_wrap=True); t.add_column("v")
            t.add_row("ID", _escape_console_text(iid))
            if img["vmid"] is not None: t.add_row("Source VMID", _escape_console_text(str(img["vmid"])))
            t.add_row("Name", _escape_console_text(str(img["name"] or "-")))
            t.add_row("Template", ts); t.add_row("Scheduler", st)
            t.add_row("Labels", _escape_console_text(str(img["labels"])))
            t.add_row("HW", _escape_console_text(str(img["hw"])))
            console.print(t)
        else: print(f"  ID:{iid} VMID:{img['vmid']} Name:{img['name']} Template:{'yes' if img['is_template'] else 'no'} Status:{img['status']} Labels:{img['labels']} HW:{img['hw']}")

    def _cmd_vms(self):
        imgs = self.dc.get_images_display()
        if not imgs: self._pi("[VMs] 当前没有已注册的 VM 镜像"); return
        self._ph("[VMs] 已注册 VM 镜像")
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
            t.add_column("Status",no_wrap=True); t.add_column("Image ID",no_wrap=True); t.add_column("VMID",no_wrap=True)
            t.add_column("Name"); t.add_column("Version",no_wrap=True); t.add_column("Template",no_wrap=True)
            t.add_column("Labels"); t.add_column("HW")
            for x in imgs:
                ss = "[bold red]BUSY[/bold red]" if x["status"]=="busy" else "[bold green]FREE[/bold green]"
                ts = "[bold blue]YES[/bold blue]" if x["is_template"] else "-"
                t.add_row(ss, _escape_console_text(str(x["id"])),
                          _escape_console_text(str(x["vmid"]) if x["vmid"] is not None else "-"),
                          _escape_console_text(str(x["name"] or "-")),
                          _escape_console_text(str(x["version"] or "-")), ts,
                          _escape_console_text(str(x["labels"])), _escape_console_text(str(x["hw"])))
            console.print(t)
        else:
            for x in imgs: print(f"  - {x['id']} vmid={x['vmid']} name={x['name']} status={x['status']} labels={x['labels']}")

    def _cmd_cz_list(self):
        cz = self.dc.get_clonezilla_list()
        if not cz: self._pi("当前没有已注册的 Clonezilla 镜像"); return
        self._ph("[Clonezilla] 已注册镜像")
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
            t.add_column("ISO Name"); t.add_column("Node",no_wrap=True); t.add_column("IP",no_wrap=True); t.add_column("Status",no_wrap=True)
            for c in cz:
                ss = "[bold red]BUSY[/bold red]" if c["status"]=="busy" else "[bold green]FREE[/bold green]"
                t.add_row(_escape_console_text(c["iso_name"]), _escape_console_text(c["node"]),
                          _escape_console_text(c["ip"]), ss)
            console.print(t)
        else:
            for c in cz: print(f"  {c['iso_name']} node={c['node']} ip={c['ip']} status={c['status'].upper()}")

    def _cmd_cz_register(self):
        m = self.dc.master_server
        if m is None: self._pe("[Clonezilla] master_server 未设置"); return
        nn = str(getattr(m,"pve_node_name","") or getattr(m,"hostname","") or "master")
        try: isos = self.dc.scan_clonezilla_isos(nn)
        except Exception as e: self._pe(f"[Clonezilla] 扫描镜像失败：{e}"); return
        self._cz_seen.update(isos)
        pending = [n for n in isos if n not in set((self.dc.clonezilla_images or {}).keys())]
        if not pending: self._pi("未发现新的 Clonezilla 镜像"); return
        self._cz_register_isos(nn, pending)

    def _cmd_refresh(self):
        with console.status("正在刷新资源（nodes/vms/status）..."):
            try: self.dc.connect_master()
            except Exception as e: self._pe(f"[refresh] 重连 master 失败：{e}"); return
            try: nodes = self.dc.get_nodes()
            except Exception as e: self._pe(f"[refresh] 获取 nodes 失败：{e}"); return
            online = sum(1 for n in (nodes or []) if isinstance(n,dict) and str(n.get("status") or "").lower() in {"online","ok"})
            try: self.dc.refresh_image_locations()
            except: pass
        self._ps(f"[refresh] 完成：nodes={online}/{len(nodes)} online；已刷新已纳管镜像位置")

    def _cmd_clear(self):
        try: os.system("cls" if os.name=="nt" else "clear")
        except: pass

    @staticmethod
    def _print_help():
        console.rule("[bold]Help[/bold]")
        cmds = [
            ("list","list","列出当前注册的所有主机（含 FREE/BUSY）"),
            ("tasks","tasks","查看 Running/Pending 任务"),
            ("logs","logs <task_id> [n]","查看任务日志末尾 n 行（默认 20）"),
            ("check","check <host_id>","主机连通性诊断：ping + SSH 握手"),
            ("tag","tag <id> <+label|-label>","动态修改主机/镜像标签"),
            ("refresh","refresh","刷新 nodes 状态与已纳管镜像位置"),
            ("clear","clear","清空终端屏幕"),
            ("cancel","cancel <task_id>","取消 Pending 任务"),
            ("release","release <host_id>","释放主机及其关联镜像"),
            ("info host","info host <host_id>","查看主机详情"),
            ("info image","info image <image_id>","查看镜像详情"),
            ("vms","vms","列出已注册的 VM 镜像"),
            ("cz-list","cz-list","列出已注册的 Clonezilla ISO 镜像"),
            ("cz-register","cz-register","扫描并注册新 Clonezilla 镜像"),
            ("deploy","deploy <vmid> <host_id>","统一部署入口：自动按目标类型走 PVE 迁移或裸机 Clonezilla 克隆"),
            ("template","template <vmid>","将已注册 VM 转为 PVE 模板"),
            ("clone-template","clone-template <t_vmid> <n_vmid> <target> [name]","从模板 full clone 并注册"),
            ("auto","auto","自动部署问答向导"),
            ("register-baremetal","register-baremetal [cz_iso]","注册裸机并绑定 Clonezilla 镜像"),
            ("help","help","显示本帮助"),("exit","exit","退出"),
        ]
        if RICH_AVAILABLE and _RichTable is not None:
            t = _RichTable(box=_rich_box.SIMPLE, show_lines=False)
            t.add_column("Command",style="bold",no_wrap=True); t.add_column("Usage",no_wrap=True); t.add_column("Description")
            for cmd,usage,desc in cmds: t.add_row(_escape_console_text(cmd), _escape_console_text(usage), _escape_console_text(desc))
            console.print(t); console.print("\n[dim]提示：pip install rich prompt_toolkit 可获得彩色/补全体验[/dim]")
        else:
            for _,usage,desc in cmds: print(f"  {usage}\n    {desc}")
            print("\n提示：pip install rich prompt_toolkit 可获得彩色/补全体验")

# ── Helper methods ──────────────────────────────────────────────────

# ── Main entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    """主入口：初始化 InteractivePlatform 并运行交互式部署流程。"""
    import sys
    
    try:
        platform = InteractivePlatform()
        # 显示启动信息
        print("\n" + "="*60)
        print("  FineCycle - 自动化迁移部署平台")
        print("="*60 + "\n")
        
        # 检查是否已有 master_server 配置
        # 这里可以根据需要选择是否自动进入引导流程
        # 方案1：总是进入引导流程
        should_bootstrap = True
        
        # 方案2：检查是否有现有配置
        # should_bootstrap = platform.dc.master_server is None
        
        if should_bootstrap:
            print("首次运行，开始初始化配置...\n")
            platform.bootstrap_and_discovery()
        else:
            print("检测到已有配置，跳过初始化...\n")
        
        # 进入命令循环
        print("\n" + "="*60)
        print("  进入命令模式，输入 help 查看帮助")
        print("="*60 + "\n")
        platform.command_loop()
        
    except KeyboardInterrupt:
        print("\n\n用户中断，退出程序。")
        sys.exit(0)
    except Exception as e:
        print(f"\n程序异常退出: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
