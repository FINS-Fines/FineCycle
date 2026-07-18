# core/datacenter.py — DataCenter: 统一门面（主机/镜像/调度/部署）
from __future__ import annotations

import json
import ipaddress
import logging
import uuid
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from configs.config import load_config
from domain.clonezilla import ClonezillaImage
from domain.image import RobotAppImage
from infrastructure.base_host import BaseHost
from infrastructure.bare_metal_host import BareMetalHost
from infrastructure.pve_host import PveHost
from infrastructure.server_host import ServerHost
from core.scheduler import (
    HostProfile, ImageProfile, Scheduler, TaskRequest, TaskRequirements,
)


class DataCenter:
    """机器人数据中心统一门面。

    职责：
    - 注册/管理主机与镜像
    - 提供统一调度入口（内部 Scheduler 已内置派发）
    - 执行各类部署流水线（PVE 迁移 / 裸机克隆 / 模板化）
    - 标签管理、资源释放等业务操作

    约束：DataCenter 不重启宿主机、不修改宿主机网络配置。
    """

    def __init__(self, name: str, config: Any | None = None, *, max_workers: int = 3):
        self.name = name
        self.hosts: Dict[str, BaseHost] = {}
        self.clonezilla_images: dict[str, ClonezillaImage] = {}
        self.master_server: Optional[ServerHost] = None
        self.config = config or load_config()
        self.scheduler = Scheduler(self, max_workers=max_workers)
        self._last_image_location_refresh_ts: float = 0.0

    # ── Basic queries ───────────────────────────────────────────────────

    def find_host(self, host_id_or_node: str) -> Optional[BaseHost]:
        key = str(host_id_or_node or "").strip()
        if not key:
            return None
        if key in self.hosts:
            return self.hosts[key]
        for h in self.hosts.values():
            if str(getattr(h, "pve_node_name", "")) == key:
                return h
        return None

    def find_vm_current_node(self, vmid: int) -> Optional[str]:
        master = self.master_server
        if master is None:
            return None
        try:
            resources = master.pve_api.cluster.resources.get(type="vm") or []
        except Exception:
            return None
        for r in resources:
            if not isinstance(r, dict) or r.get("type") != "qemu":
                continue
            try:
                if int(r.get("vmid", 0)) == int(vmid):
                    node = r.get("node")
                    return str(node) if node else None
            except Exception:
                continue
        return None

    def get_host_status(self):
        return {hid: h.check_health() for hid, h in self.hosts.items()}

    # ── Image location refresh ──────────────────────────────────────────

    def refresh_image_locations(self, *, min_interval_s: float = 2.0) -> None:
        now = time.time()
        if min_interval_s > 0 and (now - float(self._last_image_location_refresh_ts)) < float(min_interval_s):
            return
        master = self.master_server
        if master is None:
            return
        images = (getattr(self.scheduler, "images", {}) or {})
        if not images:
            self._last_image_location_refresh_ts = now
            return
        vmid_to_node: dict[int, str] = {}
        try:
            pve_api = getattr(master, "pve_api", None)
            if pve_api is not None:
                for r in (pve_api.cluster.resources.get(type="vm") or []):
                    if isinstance(r, dict) and r.get("type") == "qemu":
                        try:
                            vmid = int(r.get("vmid", 0))
                        except Exception:
                            continue
                        node = r.get("node")
                        if vmid and node:
                            vmid_to_node[vmid] = str(node)
        except Exception:
            pass
        if not vmid_to_node:
            try:
                out, _ = master.execute_ssh("pvesh get /cluster/resources --type vm --output-format json")
                for r in (json.loads(out or "[]") or []):
                    if isinstance(r, dict) and r.get("type") == "qemu":
                        try:
                            vmid = int(r.get("vmid", 0))
                        except Exception:
                            continue
                        if vmid and r.get("node"):
                            vmid_to_node[vmid] = str(r["node"])
            except Exception:
                pass
        for _image_id, img in images.items():
            if img is None:
                continue
            try:
                vmid = int(getattr(img, "source_vm_id", 0) or 0)
            except Exception:
                continue
            if vmid:
                setattr(img, "source_node", vmid_to_node.get(vmid, "unknown"))
        self._last_image_location_refresh_ts = now

    # ── Registration ────────────────────────────────────────────────────

    def set_master_server(self, host_info):
        if isinstance(host_info, ServerHost):
            self.master_server = host_info
        else:
            extra_kwargs = {}
            for k in ('pve_api_username', 'pve_api_password', 'pve_api_port',
                      'pve_verify_ssl', 'ssh_username', 'ssh_password',
                      'ssh_port', 'ssh_key_filename', 'ssh_timeout_s'):
                if k in host_info and host_info.get(k) is not None:
                    extra_kwargs[k] = host_info.get(k)
            self.master_server = ServerHost(
                host_id=host_info['host_id'], ip=host_info['ip'],
                hostname=host_info['hostname'],
                pve_node_name=host_info['pve_node_name'],
                image_info=host_info.get('image_info'), **extra_kwargs,
            )
        try:
            setattr(self.master_server, "config", self.config)
        except Exception:
            pass
        self.hosts[self.master_server.host_id] = self.master_server
        self.scheduler.register_host(
            self.master_server,
            HostProfile(host_id=self.master_server.host_id, kind='Server', labels=set(), capabilities={}),
            is_free=False,
        )
        print(f"[DataCenter] Master server set: {self.master_server.hostname}")

    def register_host(self, host_info):
        if isinstance(host_info, BaseHost):
            host = host_info
            host_type = ('Server' if isinstance(host, ServerHost) else
                         'PVE' if isinstance(host, PveHost) else
                         'BareMetal' if isinstance(host, BareMetalHost) else host.__class__.__name__)
            labels = set(getattr(host, 'labels', set()) or set())
            capabilities = dict(getattr(host, 'capabilities', {}) or {})
        else:
            host_type = host_info.get('type')
            extra_kwargs = {}
            for k in ('connect_pve_api', 'pve_api_username', 'pve_api_password',
                      'pve_api_port', 'pve_verify_ssl', 'ssh_username',
                      'ssh_password', 'ssh_port', 'ssh_key_filename', 'ssh_timeout_s'):
                if k in host_info and host_info.get(k) is not None:
                    extra_kwargs[k] = host_info.get(k)
            if host_type == 'PVE':
                host = PveHost(
                    host_id=host_info['host_id'], ip=host_info['ip'],
                    hostname=host_info['hostname'],
                    pve_node_name=host_info['pve_node_name'],
                    network_config=host_info.get('network_config'),
                    image_info=host_info.get('image_info'), **extra_kwargs,
                )
            elif host_type == 'BareMetal':
                host = BareMetalHost(
                    host_id=host_info['host_id'], hostname=host_info['hostname'],
                    target_disk=host_info.get('target_disk', ''),
                    ip=host_info.get('ip'), image_info=host_info.get('image_info'),
                )
            elif host_type == 'Server':
                host = ServerHost(
                    host_id=host_info['host_id'], ip=host_info['ip'],
                    hostname=host_info['hostname'],
                    pve_node_name=host_info['pve_node_name'],
                    image_info=host_info.get('image_info'), **extra_kwargs,
                )
            else:
                raise ValueError(f"Unknown host type: {host_type}")
            labels = set(host_info.get('labels', []) or [])
            capabilities = dict(host_info.get('capabilities', {}) or {})
        try:
            setattr(host, "config", self.config)
        except Exception:
            pass
        self.hosts[host.host_id] = host
        self.scheduler.register_host(
            host,
            HostProfile(host_id=host.host_id, kind=host_type, labels=labels, capabilities=capabilities),
            is_free=True,
        )
        print(f"[DataCenter] Registered host: {host.hostname} ({host_type})")

    def add_host_with_profile(
        self, host: BaseHost, *, kind: str, is_free: bool,
        labels: set[str] | None = None, capabilities: dict[str, Any] | None = None,
    ) -> None:
        """注册主机并同步 labels/capabilities 到 scheduler profile。"""
        labels = set(labels or set())
        capabilities = dict(capabilities or {})
        try:
            setattr(host, "labels", labels)
            setattr(host, "capabilities", capabilities)
        except Exception:
            pass
        self.hosts[str(host.host_id)] = host
        self.scheduler.register_host(
            host,
            HostProfile(host_id=str(host.host_id), kind=str(kind), labels=labels, capabilities=capabilities),
            is_free=bool(is_free),
        )
        print(f"[DataCenter] Host added: {getattr(host, 'hostname', '')} ({kind})")

    def create_and_register_server_host(self, host_id: str, ip: str, hostname: str,
                                          pve_node_name: str, *,
                                          ssh_username: str = "root", ssh_password: str | None = None,
                                          ssh_port: int = 22) -> ServerHost:
        """创建 ServerHost 并注册到数据中心。"""
        host = ServerHost(host_id=host_id, ip=ip, hostname=hostname, pve_node_name=pve_node_name,
                          connect_pve_api=False, ssh_username=ssh_username,
                          ssh_password=ssh_password, ssh_port=ssh_port)
        self.add_host_with_profile(host, kind="Server", is_free=False)
        return host

    def create_and_register_pve_host(self, host_id: str, ip: str, hostname: str,
                                       pve_node_name: str, *,
                                       ssh_username: str = "root", ssh_password: str | None = None,
                                       ssh_port: int = 22,
                                       labels: set[str] | None = None,
                                       capabilities: dict[str, Any] | None = None,
                                       network_config: dict[str, Any] | None = None,
                                       ) -> PveHost:
        """创建 PveHost，自动探测 network_config 并注册到数据中心。"""
        labels = set(labels or set())
        capabilities = dict(capabilities or {})
        # Auto-detect network config if not provided
        if network_config is None:
            try:
                network_config = self.build_network_config_for_host(
                    ip=ip, node_name=pve_node_name,
                    ssh_username=ssh_username, ssh_password=ssh_password, ssh_port=ssh_port,
                )
            except Exception:
                network_config = {}
        host = PveHost(host_id=host_id, ip=ip, hostname=hostname, pve_node_name=pve_node_name,
                       network_config=None, connect_pve_api=False,
                       ssh_username=ssh_username, ssh_password=ssh_password, ssh_port=ssh_port)
        host.network_config = dict(network_config or {})
        self.add_host_with_profile(host, kind="PVE", is_free=True,
                                   labels=labels, capabilities=capabilities)
        return host

    def create_and_register_image(self, image_id: str, name: str, version: str,
                                    source_vm_id: int, source_node: str, *,
                                    is_template: bool = False,
                                    tags: list[str] | None = None,
                                    min_ram: int = 4, need_gpu: bool = False,
                                    ) -> str:
        """创建 RobotAppImage 并注册到调度池。Returns: image_id。"""
        tags = list(tags or [])
        hw_req: dict[str, Any] = {"ram_gb": int(min_ram)}
        if need_gpu:
            hw_req["has_gpu"] = True
        image = RobotAppImage(
            image_id=str(image_id), name=str(name), version=str(version),
            source_vm_id=int(source_vm_id), is_template=bool(is_template),
        )
        try:
            setattr(image, "source_node", str(source_node))
        except Exception:
            pass
        self.register_image(image, image_profile={
            "labels": sorted(tags), "software": {}, "hardware": hw_req,
        }, is_free=True)
        return str(image_id)

    def register_image(self, image: RobotAppImage, image_profile: Optional[dict] = None, *, is_free: bool = True):
        if image_profile is None:
            image_profile = {}
        profile = ImageProfile(
            image_id=image.image_id,
            labels=set(image_profile.get('labels', []) or []),
            software=dict(image_profile.get('software', {}) or {}),
            hardware_requirements=dict(image_profile.get('hardware', {}) or {}),
        )
        self.scheduler.register_image(image, profile, is_free=is_free)

    def is_image_registered(self, image_id: str) -> bool:
        return str(image_id) in (getattr(self.scheduler, "images", {}) or {})

    def get_free_clonezilla_images(self) -> list[tuple[str, ClonezillaImage]]:
        items = []
        for iso_name, item in (self.clonezilla_images or {}).items():
            if item is not None and str(getattr(item, "status", "free") or "free").strip().lower() == "free":
                items.append((str(iso_name), item))
        items.sort(key=lambda x: x[0])
        return items

    def register_cloned_image(self, source_image_id: str, new_vmid: int,
                               clone_name: str, target_node: str) -> None:
        sch = self.scheduler
        new_image_id = str(new_vmid)
        if new_image_id in (getattr(sch, "images", {}) or {}):
            raise RuntimeError(f"镜像 {new_image_id} 已在平台注册")
        src_img = (getattr(sch, "images", {}) or {}).get(str(source_image_id))
        src_prof = (getattr(sch, "image_profiles", {}) or {}).get(str(source_image_id))
        version = str(getattr(src_img, "version", "pve") if src_img else "pve")
        image = RobotAppImage(
            image_id=new_image_id, name=str(clone_name),
            version=version, source_vm_id=int(new_vmid), is_template=False,
        )
        setattr(image, "source_node", str(target_node))
        self.register_image(image, image_profile={
            "labels": sorted(getattr(src_prof, "labels", set()) or []),
            "software": dict(getattr(src_prof, "software", {}) or {}),
            "hardware": dict(getattr(src_prof, "hardware_requirements", {}) or {}),
        }, is_free=True)

    # ── Clonezilla ISO registration ─────────────────────────────────────

    def register_clonezilla_iso(self, iso_name: str, node: str, ip: str,
                                 *, status: str = "free") -> None:
        """注册一个 Clonezilla ISO 镜像到数据中心。"""
        iso_name = str(iso_name or "").strip()
        if not iso_name:
            raise ValueError("iso_name 不能为空")
        if iso_name in (self.clonezilla_images or {}):
            raise RuntimeError(f"Clonezilla 镜像 {iso_name} 已存在")
        try:
            ipaddress.ip_address(str(ip).strip())
        except Exception:
            raise ValueError(f"无效的 IP 地址: {ip}")
        self.clonezilla_images[iso_name] = ClonezillaImage(
            iso_name=iso_name, node=str(node), ip=str(ip).strip(),
            status=str(status or "free").strip().lower(),
        )
        print(f"[DataCenter] Registered Clonezilla ISO: {iso_name} -> {ip}")

    def scan_and_register_clonezilla_isos(self) -> list[str]:
        """扫描 master 节点 local 存储中的 Clonezilla ISO，自动注册未注册的。
        Returns: 新注册的 ISO 名称列表。
        """
        master = self.master_server
        if master is None:
            return []
        node_name = str(getattr(master, "pve_node_name", "") or getattr(master, "hostname", "") or "master")
        try:
            iso_names = master.scan_clonezilla_isos(node_name)
        except Exception:
            return []
        existing = set((self.clonezilla_images or {}).keys())
        newly_registered: list[str] = []
        for iso_name in iso_names:
            if iso_name in existing:
                continue
            ip_value = ServerHost.parse_clonezilla_ip(iso_name)
            if not ip_value:
                continue
            try:
                self.register_clonezilla_iso(iso_name, node_name, ip_value)
                newly_registered.append(iso_name)
            except Exception:
                continue
        return newly_registered

    def bootstrap_master(self, master_ip: str, *,
                          ssh_username: str, ssh_password: str, ssh_port: int = 22,
                          pve_api_username: str = "root@pam", pve_api_password: str = "",
                          pve_api_port: int = 8006, pve_verify_ssl: bool = False,
                          ) -> ServerHost:
        """创建并连接 master ServerHost。失败时抛出异常。"""
        import paramiko
        try:
            master = ServerHost(
                host_id="master", ip=master_ip, hostname="master",
                pve_node_name="master",
                ssh_username=ssh_username, ssh_password=ssh_password,
                ssh_port=ssh_port,
                pve_api_username=pve_api_username,
                pve_api_password=pve_api_password,
                pve_api_port=pve_api_port,
                pve_verify_ssl=pve_verify_ssl,
            )
        except Exception as e:
            raise RuntimeError(f"连接 Master 失败: {e}") from e
        try:
            out, _ = master.execute_ssh("hostname")
            node_name = (out or "").strip().splitlines()[0].strip() if out else "master"
        except Exception:
            node_name = "master"
        master.hostname = node_name
        master.pve_node_name = node_name
        self.set_master_server(master)
        return master

    # ── QM controller helper ────────────────────────────────────────────

    def get_qm_controller(self, node_name: str) -> tuple[Any, bool]:
        """为指定 PVE 节点获取可执行 qm 命令的控制器。

        Returns: (controller, temporary) — temporary=True 表示是临时创建的，用完后应 stop_listening。
        """
        master = self.master_server
        if master is None:
            raise RuntimeError("master_server 未设置")
        host = self.find_host(node_name)
        if host is not None and hasattr(host, "execute_ssh"):
            resolved_ip = getattr(master, "resolve_node_ssh_ip", lambda n: None)(node_name) if hasattr(master, "resolve_node_ssh_ip") else None
            if resolved_ip:
                setattr(host, "ip", str(resolved_ip))
            return host, False
        node_ip = master.resolve_node_ssh_ip(node_name)
        if not node_ip:
            raise RuntimeError(f"无法解析节点 {node_name} 的 SSH 地址")
        controller = ServerHost(
            host_id=f"tmp-{node_name}", ip=str(node_ip), hostname=str(node_name),
            pve_node_name=str(node_name), connect_pve_api=False,
            ssh_username=str(getattr(master, "_ssh_username", "root")),
            ssh_password=getattr(master, "_ssh_password", None),
            ssh_port=int(getattr(master, "_ssh_port", 22) or 22),
            pve_api_username=str(getattr(master, "_pve_api_username", "root@pam")),
            pve_api_password=getattr(master, "_pve_api_password", None),
            pve_api_port=int(getattr(master, "_pve_api_port", 8006) or 8006),
            pve_verify_ssl=bool(getattr(master, "_pve_verify_ssl", False)),
        )
        controller.pve_api = getattr(master, "pve_api", None)
        return controller, True

    # ── PVE node resolution ─────────────────────────────────────────────

    def resolve_pve_node(self, target_node_id: str) -> str:
        key = str(target_node_id or "").strip()
        if not key:
            raise RuntimeError("target_node_id 不能为空")
        host = self.find_host(key)
        if host is not None:
            if isinstance(host, BareMetalHost):
                raise RuntimeError(f"目标 {key} 是裸机节点，不能作为 PVE 目标")
            node_name = str(getattr(host, "pve_node_name", "") or key).strip()
            if node_name:
                return node_name
        master = self.master_server
        if master is None:
            return key
        try:
            nodes = master.pvesh_get_json("/nodes")
        except Exception:
            return key
        known = {str(item.get("node") or "").strip() for item in (nodes or [])
                 if isinstance(item, dict) and str(item.get("node") or "").strip()}
        if not known or key in known:
            return key
        raise RuntimeError(f"未找到 PVE 节点：{key}")

    # ── Task operations ─────────────────────────────────────────────────

    def submit_auto_task(
        self, *, tag: str = "", require_gpu: bool = False,
        min_ram: int = 4, priority: str = "C",
    ) -> str:
        """根据参数创建 TaskRequest 并提交到调度器。
        Returns: task_id
        """
        time_str = time.strftime("%H%M")
        tag_str = (tag[:3].upper() if tag else "GEN")
        short_hash = uuid.uuid4().hex[:4].upper()
        task_id = f"T{time_str}-{tag_str}-{short_hash}"
        required_labels = {tag} if tag else set()
        required_caps: dict[str, Any] = {"ram_gb": int(min_ram)}
        if require_gpu:
            required_caps["has_gpu"] = True
        req = TaskRequirements(
            required_host_labels=set(required_labels),
            required_image_labels=set(required_labels),
            required_capabilities=required_caps,
        )
        task = TaskRequest(task_id=task_id, requirements=req, priority=priority)
        self.scheduler.submit_task(task)
        # 确保日志目录存在
        try:
            logs_dir = Path(__file__).resolve().parent.parent / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return task_id

    def release_task(self, host_id: str, *, force: bool = False) -> bool:
        """释放指定主机上的任务并释放资源。"""
        sch = self.scheduler
        task_id = None
        host_to_task = getattr(sch, "host_to_task", None)
        if isinstance(host_to_task, dict):
            task_id = host_to_task.get(str(host_id))
        if not task_id:
            allocations = getattr(sch, "allocations", {}) or {}
            for tid, alloc in allocations.items():
                try:
                    if str(getattr(alloc, "host_id", "")) == str(host_id):
                        task_id = str(tid)
                        break
                except Exception:
                    continue
        if not task_id:
            if force:
                sch.mark_host_free(str(host_id))
                return True
            return False
        return bool(sch.release(str(task_id)))

    def cancel_task(self, task_id: str) -> bool:
        sch = self.scheduler
        allocations = getattr(sch, "allocations", {}) or {}
        if str(task_id) in allocations:
            return False  # Running task cannot be cancelled
        return bool(sch.cancel_pending_task(str(task_id)))

    # ── Tag management ──────────────────────────────────────────────────

    def tag_entity(self, entity_id: str, operation: str) -> tuple[str, list[str]]:
        """给主机或镜像添加/删除标签。Returns: (type, updated_labels)。"""
        eid = str(entity_id or "").strip()
        op = str(operation or "").strip()
        if op[0] not in {"+", "-"} or len(op) < 2:
            raise ValueError("operation 格式错误，应为 +label 或 -label")
        label = op[1:].strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-\.]*", label):
            raise ValueError("label 仅允许字母数字开头")

        sch = self.scheduler

        if eid in (self.hosts or {}):
            h = self.hosts.get(eid)
            current = set(getattr(h, "labels", set()) or set())
            prof = (getattr(sch, "host_profiles", {}) or {}).get(eid)
            if prof is not None:
                current = set(getattr(prof, "labels", set()) or set())
            if op[0] == "+":
                current.add(label)
            else:
                current.discard(label)
            if h is not None:
                setattr(h, "labels", set(current))
            if prof is not None:
                sch.host_profiles[str(eid)] = HostProfile(
                    host_id=str(getattr(prof, "host_id", eid)),
                    kind=str(getattr(prof, "kind", "")),
                    labels=set(current),
                    capabilities=dict(getattr(prof, "capabilities", {}) or {}),
                )
            return "host", sorted(current)

        if eid in (getattr(sch, "images", {}) or {}):
            prof = (getattr(sch, "image_profiles", {}) or {}).get(eid)
            if prof is None:
                raise RuntimeError("镜像缺少 profile")
            current = set(getattr(prof, "labels", set()) or set())
            if op[0] == "+":
                current.add(label)
            else:
                current.discard(label)
            sch.image_profiles[str(eid)] = ImageProfile(
                image_id=str(getattr(prof, "image_id", eid)),
                labels=set(current),
                software=dict(getattr(prof, "software", {}) or {}),
                hardware_requirements=dict(getattr(prof, "hardware_requirements", {}) or {}),
            )
            return "image", sorted(current)

        raise RuntimeError(f"未找到主机或镜像：{eid}")

    # ── PVE migration ───────────────────────────────────────────────────

    def deploy_pve(
        self, vmid: int, target_node_id: str, *,
        logger: logging.Logger | None = None,
    ) -> str:
        """执行 PVE 在线迁移流水线。成功后在目标为 PveHost 时自动登记 allocation。"""
        from core.pve_pipeline import DeploymentPipeline, PipelineError

        master = self.master_server
        if master is None:
            raise RuntimeError("master_server 未设置，无法执行迁移")
        if str(vmid) not in getattr(self.scheduler, "images", {}):
            raise RuntimeError(f"VM {vmid} 未在平台注册，只有被纳管的镜像才允许迁移")

        target_host = self.find_host(target_node_id)
        if target_host is None:
            raise RuntimeError(f"未找到目标节点：{target_node_id}")

        # 裸机目标：应使用 deploy_baremetal() 而非 deploy_pve()
        if isinstance(target_host, BareMetalHost):
            raise RuntimeError(
                f"目标 {target_node_id} 是裸机节点，请使用 baremetal 命令执行 Clonezilla 部署。"
            )

        # 检查目标可用性
        try:
            self.ensure_target_available(str(vmid), str(target_node_id))
        except Exception as e:
            raise RuntimeError(f"无法开始部署：{e}") from e

        # 目标端网络配置探测（失败不阻塞，pipeline 内部有兜底）
        if isinstance(target_host, PveHost):
            try:
                resolved = master.resolve_node_ssh_ip(target_host.pve_node_name)
                if resolved:
                    target_host.ip = resolved
            except Exception:
                pass
            try:
                from infrastructure.pve_host import PveHost as _PveHost
                probe = PveHost(
                    host_id=f"probe-{target_host.pve_node_name}", ip=target_host.ip,
                    hostname=target_host.pve_node_name, pve_node_name=target_host.pve_node_name,
                    connect_pve_api=False, network_config=None,
                    ssh_username=getattr(target_host, "_ssh_username", "root"),
                    ssh_password=getattr(target_host, "_ssh_password", None),
                    ssh_port=int(getattr(target_host, "_ssh_port", 22)),
                    ssh_key_filename=getattr(target_host, "_ssh_key_filename", None),
                )
                network_config = _PveHost.build_network_config_from_probe(probe, target_host.ip)
                target_host.network_config = dict(network_config)
            except Exception as e:
                if logger:
                    logger.warning("[DEPLOY] 目标端 network_config 自动探测失败，将使用已有配置: %s", e)

        try:
            pipeline = DeploymentPipeline(master, hosts=self.hosts, logger=logger, config=self.config)
            pipeline.execute_pipeline(str(vmid), str(target_node_id))
        except PipelineError:
            raise
        except Exception as e:
            raise RuntimeError(f"迁移流水线执行失败: {e}") from e

        # 迁移成功后登记手工 allocation（Server 节点不参与调度，跳过）
        if not isinstance(target_host, ServerHost):
            try:
                host_id = str(getattr(target_host, "host_id", "") or target_node_id)
                self.ensure_target_available(str(vmid), host_id)
                self.scheduler.bind_manual_allocation(host_id, str(vmid))
            except Exception as e:
                if logger:
                    logger.warning("[DEPLOY] 部署已完成，但登记手工迁移关联失败：%s", e)
        try:
            self.refresh_image_locations(min_interval_s=0.0)
        except Exception:
            pass
        return str(vmid)

    # ── Template / Clone ────────────────────────────────────────────────

    def template_vm(self, vmid: int, *, logger: logging.Logger | None = None) -> bool:
        """将已注册 VM 转为 PVE 模板。"""
        master = self.master_server
        if master is None:
            raise RuntimeError("master_server 未设置")
        sch = self.scheduler
        image_id = str(vmid)
        if image_id not in (getattr(sch, "images", {}) or {}):
            raise RuntimeError(f"VM {vmid} 未在平台注册")
        if image_id in set(getattr(sch, "busy_images", set()) or set()):
            raise RuntimeError(f"VM {vmid} 当前 BUSY，不能模板化")

        current_node = self.find_vm_current_node(vmid)
        if not current_node:
            raise RuntimeError(f"无法找到 VM {vmid} 所在节点")

        controller, temporary = self.get_qm_controller(current_node)
        try:
            already = bool(controller.is_template_vm(vmid))
            if not already:
                controller.template_vm(vmid)
            img = (getattr(sch, "images", {}) or {}).get(image_id)
            if img is not None:
                setattr(img, "is_template", True)
                setattr(img, "source_node", str(current_node))
            return not already  # True = newly templated
        finally:
            if temporary and controller is not None:
                try:
                    controller.stop_listening()
                except Exception:
                    pass

    def clone_template(
        self, template_vmid: int, new_vmid: int, target_node_id: str,
        new_name: str | None = None, *, logger: logging.Logger | None = None,
    ) -> str:
        """从模板 VM 执行 full clone 并注册新镜像。Returns: new_image_id。"""
        master = self.master_server
        if master is None:
            raise RuntimeError("master_server 未设置")
        sch = self.scheduler
        source_image_id = str(template_vmid)
        new_image_id = str(new_vmid)
        if source_image_id not in (getattr(sch, "images", {}) or {}):
            raise RuntimeError(f"模板源 VM {template_vmid} 未在平台注册")
        if new_image_id in (getattr(sch, "images", {}) or {}):
            raise RuntimeError(f"新 VMID {new_vmid} 已在平台注册")

        existing_node = self.find_vm_current_node(new_vmid)
        if existing_node:
            raise RuntimeError(f"VMID {new_vmid} 已存在于节点 {existing_node}")

        current_node = self.find_vm_current_node(template_vmid)
        if not current_node:
            raise RuntimeError(f"无法找到模板 VM {template_vmid} 所在节点")

        target_node = self.resolve_pve_node(target_node_id)
        src_img = (getattr(sch, "images", {}) or {}).get(source_image_id)
        src_name = str(getattr(src_img, "name", "") or f"vm-{template_vmid}").strip()
        clone_name = str(new_name or "").strip() or f"{src_name}-clone-{new_vmid}"

        controller, temporary = self.get_qm_controller(current_node)
        try:
            if not controller.is_template_vm(template_vmid):
                # 自动模板化
                controller.template_vm(template_vmid)
                img = (getattr(sch, "images", {}) or {}).get(source_image_id)
                if img is not None:
                    setattr(img, "is_template", True)

            controller.clone_template_vm(template_vmid, new_vmid,
                                         target_node=target_node, name=clone_name, full=True)
            self.register_cloned_image(source_image_id, new_vmid, clone_name, target_node)
            return new_image_id
        finally:
            if temporary and controller is not None:
                try:
                    controller.stop_listening()
                except Exception:
                    pass

    # ── Bare-metal registration & deployment ────────────────────────────

    def register_baremetal(
        self, host_id: str, target_disk: str, clonezilla_iso_name: str,
    ) -> BareMetalHost:
        """注册裸机并绑定 Clonezilla 镜像。"""
        if host_id in (self.hosts or {}):
            raise RuntimeError(f"已存在同名主机：{host_id}")
        cz_img = (self.clonezilla_images or {}).get(clonezilla_iso_name)
        if cz_img is None:
            raise RuntimeError(f"未找到 Clonezilla 镜像：{clonezilla_iso_name}")
        if str(getattr(cz_img, "status", "free") or "free").strip().lower() != "free":
            raise RuntimeError(f"镜像 {clonezilla_iso_name} 当前不空闲")

        ip_address = str(getattr(cz_img, "ip", "") or "").strip()
        host = BareMetalHost(
            host_id=str(host_id), hostname=str(host_id),
            target_disk=str(target_disk), ip=str(ip_address),
        )
        setattr(host, "clonezilla_iso_name", str(clonezilla_iso_name))
        setattr(host, "clonezilla_ip", str(ip_address))
        self.add_host_with_profile(host, kind="BareMetal", is_free=True)
        cz_img.status = "busy"
        return host

    def deploy_baremetal(
        self, image_id: str, target_bm_id: str,
        source_clonezilla_iso: str, *,
        logger: logging.Logger | None = None,
    ) -> bool:
        """执行裸机 Clonezilla 克隆流水线。"""
        from core.baremetal_pipeline import (
            BareMetalPipelineError, ClonezillaMigrationPipeline,
        )

        master = self.master_server
        sch = self.scheduler
        try:
            vmid = int(image_id)
        except ValueError:
            raise RuntimeError("源端虚拟机 ID 必须是整数")

        if str(image_id) not in (getattr(sch, "images", {}) or {}):
            raise RuntimeError(f"未找到已注册的虚拟机：{image_id}")

        target_host = self.find_host(target_bm_id)
        if target_host is None or not isinstance(target_host, BareMetalHost):
            raise RuntimeError(f"未找到目标裸机节点：{target_bm_id}")

        source_cz = (self.clonezilla_images or {}).get(source_clonezilla_iso)
        if source_cz is None:
            raise RuntimeError(f"未找到源端 Clonezilla 镜像：{source_clonezilla_iso}")
        source_ip = str(getattr(source_cz, "ip", "") or "").strip()
        if not source_ip:
            raise RuntimeError(f"源端 Clonezilla 镜像 {source_clonezilla_iso} 缺少 IP")

        target_ip = str(getattr(target_host, "ip", "") or "").strip()
        target_disk = str(getattr(target_host, "target_disk", "") or "").strip()
        source_disk = "sda"  # 源端默认磁盘

        # 标记资源为 BUSY
        sch.mark_image_busy(str(image_id))
        source_status = str(getattr(source_cz, "status", "free") or "free").strip().lower()
        if source_status != "free":
            raise RuntimeError(f"源端镜像 {source_clonezilla_iso} 当前状态为 {source_status}")
        source_cz.status = "busy"

        pipeline = ClonezillaMigrationPipeline(
            master_server=self.master_server, hosts=self.hosts,
            logger=logger, config=self.config)
        keep_image_busy = False
        try:
            ok = pipeline.execute_pipeline(
                source_ip=source_ip, target_ip=target_ip,
                source_disk=source_disk, target_disk=target_disk,
                image_id=str(image_id), logger=logger,
                vm_id=vmid, clonezilla_boot_mode="source",
            )
            if ok:
                # 成功后保持 BUSY + 登记手工 allocation
                keep_image_busy = True
                try:
                    self.ensure_target_available(str(image_id), str(target_bm_id))
                    self.scheduler.bind_manual_allocation(str(target_bm_id), str(image_id))
                except Exception:
                    if logger:
                        logger.warning("[BAREMETAL] 部署已完成，但登记手工迁移关联失败")
            return bool(ok)
        except BareMetalPipelineError:
            raise
        finally:
            if not keep_image_busy:
                try:
                    sch.mark_image_free(str(image_id))
                except Exception:
                    pass
            source_cz.status = "free"

    # ── Utility static wrappers (delegate to ServerHost / PveHost) ──────

    @staticmethod
    def parse_clonezilla_ip(iso_name: str) -> Optional[str]:
        return ServerHost.parse_clonezilla_ip(iso_name)

    @staticmethod
    def is_ipv4(value: str) -> bool:
        return ServerHost._is_ipv4(value)

    @staticmethod
    def extract_ipv4_candidates(text: str) -> list[str]:
        return ServerHost._extract_ipv4_candidates(text)

    @staticmethod
    def pick_best_ip(ips: list[str]) -> Optional[str]:
        return ServerHost._pick_best_ip(ips)

    @staticmethod
    def parse_prefixlen(value: str) -> int:
        return PveHost._parse_prefixlen(value)

    @staticmethod
    def normalize_ipv4_cidr(ip_value: str, *, prefixlen: int) -> str:
        return PveHost._normalize_ipv4_cidr(ip_value, prefixlen=prefixlen)

    # ── Cluster discovery wrappers (delegate to master_server) ─────────

    def pvesh_get_json(self, path: str, *, extra_args: str = "") -> Any:
        master = self.master_server
        if master is None:
            raise RuntimeError("master_server 未设置")
        return master.pvesh_get_json(path, extra_args=extra_args)

    def get_nodes(self) -> list[dict[str, Any]]:
        master = self.master_server
        if master is None:
            return []
        return master.get_nodes()

    def list_node_vms(self, node: str) -> list[dict[str, Any]]:
        master = self.master_server
        if master is None:
            return []
        return master.list_node_vms(node)

    def scan_clonezilla_isos(self, node: str) -> list[str]:
        master = self.master_server
        if master is None:
            return []
        return master.scan_clonezilla_isos(node)

    def resolve_node_ssh_ip(self, node_name: str) -> Optional[str]:
        master = self.master_server
        if master is None:
            return None
        return master.resolve_node_ssh_ip(node_name)

    # ── Network config builder ──────────────────────────────────────────

    def build_network_config_for_host(self, ip: str, node_name: str,
                                       ssh_username: str = "root",
                                       ssh_password: str | None = None,
                                       ssh_port: int = 22,
                                       ssh_key_filename: str | None = None,
                                       ) -> dict[str, Any]:
        """为目标 PVE 节点自动探测并构建 network_config。"""
        defaults = getattr(self.config, "network_defaults", None)
        probe = PveHost(
            host_id=f"probe-{node_name}", ip=ip, hostname=node_name,
            pve_node_name=node_name, connect_pve_api=False, network_config=None,
            ssh_username=ssh_username, ssh_password=ssh_password,
            ssh_port=ssh_port, ssh_key_filename=ssh_key_filename,
        )
        try:
            return PveHost.build_network_config_from_probe(
                probe, ip,
                default_gateway=getattr(defaults, "gateway", "192.168.8.1") if defaults else "192.168.8.1",
                default_prefixlen=getattr(defaults, "prefixlen", 21) if defaults else 21,
                default_wired_iface=getattr(defaults, "wired_interface", "enp86s0") if defaults else "enp86s0",
                default_wifi_iface=getattr(defaults, "wifi_interface", "wlo1") if defaults else "wlo1",
            )
        finally:
            try:
                probe.stop_listening()
            except Exception:
                pass

    def connect_master(self, master_ip: str) -> None:
        """从已注册的 master_server 重连/刷新 PVE API 连接（用于 refresh 命令）。"""
        master = self.master_server
        if master is None:
            return
        try:
            if getattr(master, "pve_api", None) is not None:
                _ = master.pve_api.version.get()
                return
        except Exception:
            pass
        try:
            new_master = ServerHost(
                host_id=str(getattr(master, "host_id", "master")),
                ip=str(getattr(master, "ip", "")),
                hostname=str(getattr(master, "hostname", "master")),
                pve_node_name=str(getattr(master, "pve_node_name", "master")),
                ssh_username=str(getattr(master, "_ssh_username", "root")),
                ssh_password=getattr(master, "_ssh_password", None),
                ssh_port=int(getattr(master, "_ssh_port", 22)),
                pve_api_username=str(getattr(master, "_pve_api_username", "root@pam")),
                pve_api_password=getattr(master, "_pve_api_password", None),
                pve_api_port=int(getattr(master, "_pve_api_port", 8006)),
                pve_verify_ssl=bool(getattr(master, "_pve_verify_ssl", False)),
            )
            self.master_server = new_master
            self.hosts[str(new_master.host_id)] = new_master
            self.scheduler.hosts[str(new_master.host_id)] = new_master
        except Exception as e:
            raise RuntimeError(f"重连 master 失败: {e}") from e

    # ── Display data helpers (return plain dicts for CLI rendering) ────

    def get_host_kind(self, host_id: str) -> str:
        h = (self.hosts or {}).get(str(host_id))
        if h is None:
            return "unknown"
        if isinstance(h, BareMetalHost):
            return "BareMetal"
        if isinstance(h, ServerHost):
            return "Server"
        if isinstance(h, PveHost):
            return "PVE"
        return h.__class__.__name__

    def get_hosts_display(self) -> list[dict[str, Any]]:
        """返回主机展示列表（供 CLI 渲染），纯数据不含任何 infrastructure 类型。"""
        sch = self.scheduler
        busy_set = set(getattr(sch, "busy_hosts", set()) or set())
        host_to_task = getattr(sch, "host_to_task", None)
        if not isinstance(host_to_task, dict):
            host_to_task = {}
        allocations = getattr(sch, "allocations", {}) or {}
        host_profiles = getattr(sch, "host_profiles", {}) or {}
        master_id = self.master_server.host_id if self.master_server else None
        result: list[dict[str, Any]] = []
        for hid, h in sorted((self.hosts or {}).items(), key=lambda x: str(x[0])):
            kind = self.get_host_kind(hid)
            is_busy = str(hid) in busy_set
            ip = str(getattr(h, "ip", "") or "-")
            node = str(getattr(h, "pve_node_name", "") or "").strip()
            hostname = str(getattr(h, "hostname", "") or "").strip()
            name = node or hostname or "-"
            if master_id and str(hid) == str(master_id):
                name = f"{name} (master)" if name != "-" else "(master)"
            labels: list[str] = []
            prof = host_profiles.get(str(hid))
            if prof is not None:
                try:
                    labels = sorted(list(getattr(prof, "labels", set()) or set()))
                except Exception:
                    pass
            if not labels:
                try:
                    labels = sorted(list(getattr(h, "labels", set()) or set()))
                except Exception:
                    pass
            task_info = "-"
            tid = host_to_task.get(str(hid))
            if not tid and is_busy:
                for t_id, alloc in allocations.items():
                    try:
                        if str(getattr(alloc, "host_id", "")) == str(hid):
                            tid = str(t_id)
                            break
                    except Exception:
                        continue
            if tid:
                alloc = allocations.get(str(tid))
                image_id = str(getattr(alloc, "image_id", "") or "") if alloc else ""
                task_info = f"{tid} (image={image_id})" if image_id else str(tid)
            result.append({
                "id": str(hid), "kind": kind, "ip": ip, "name": name,
                "is_busy": is_busy, "labels": labels, "task_info": task_info,
            })
        return result

    def get_images_display(self) -> list[dict[str, Any]]:
        """返回镜像展示列表（纯数据）。"""
        sch = self.scheduler
        images = getattr(sch, "images", {}) or {}
        profiles = getattr(sch, "image_profiles", {}) or {}
        free_set = set(getattr(sch, "free_images", set()) or set())
        busy_set = set(getattr(sch, "busy_images", set()) or set())
        result: list[dict[str, Any]] = []
        for image_id in sorted(images.keys(), key=lambda x: str(x)):
            img = images.get(image_id)
            prof = profiles.get(image_id)
            result.append({
                "id": str(image_id),
                "vmid": getattr(img, "source_vm_id", None) if img else None,
                "name": getattr(img, "name", "") if img else "",
                "version": getattr(img, "version", "") if img else "",
                "is_template": bool(getattr(img, "is_template", False)) if img else False,
                "status": "busy" if str(image_id) in busy_set else "free" if str(image_id) in free_set else "unknown",
                "labels": sorted(getattr(prof, "labels", set()) or []) if prof else [],
                "hw": dict(getattr(prof, "hardware_requirements", {}) or {}) if prof else {},
            })
        return result

    def get_clonezilla_list(self) -> list[dict[str, Any]]:
        """返回 Clonezilla ISO 列表（纯数据）。"""
        result: list[dict[str, Any]] = []
        for iso_name in sorted((self.clonezilla_images or {}).keys(), key=lambda x: str(x)):
            item = (self.clonezilla_images or {}).get(iso_name)
            if item is None:
                continue
            result.append({
                "iso_name": str(getattr(item, "iso_name", iso_name)),
                "node": str(getattr(item, "node", "-") or "-"),
                "ip": str(getattr(item, "ip", "-") or "-"),
                "status": str(getattr(item, "status", "free") or "free").strip().lower(),
            })
        return result

    def get_pending_tasks_display(self) -> list[dict[str, Any]]:
        """返回等待中任务列表（纯数据）。"""
        return self.scheduler.get_pending_tasks()

    def get_running_tasks_display(self) -> list[dict[str, Any]]:
        """返回运行中任务列表（纯数据）。"""
        return self.scheduler.get_running_tasks()

    @staticmethod
    def format_task_requirements_summary(req: Any) -> str:
        """把 TaskRequirements 格式化成紧凑的一行文本（用于表格展示）。"""
        if req is None:
            return "-"
        parts: list[str] = []
        try:
            hk = getattr(req, "required_host_kinds", None)
            if hk:
                parts.append(f"host_kinds={sorted(list(hk))}")
            hl = getattr(req, "required_host_labels", None)
            if hl:
                parts.append(f"host_labels={sorted(list(hl))}")
            il = getattr(req, "required_image_labels", None)
            if il:
                parts.append(f"image_labels={sorted(list(il))}")
            caps = getattr(req, "required_capabilities", None)
            if caps:
                parts.append(f"caps={dict(caps)}")
            sw = getattr(req, "required_software", None)
            if sw:
                parts.append(f"sw={dict(sw)}")
        except Exception:
            return str(req)
        return "; ".join(parts) if parts else "(none)"

    # ── Manual migration allocation ────────────────────────────────────

    def resize_disk(
        self, vmid: int, disk_name: str, new_size_gb: int, *,
        clonezilla_iso: str = "", clonezilla_ip: str = "",
        logger: logging.Logger | None = None,
    ) -> bool:
        """直接调整 VM 磁盘到指定大小（调试用）。扩容在线，缩容需 Clonezilla 修复分区表。"""
        master = self.master_server
        if master is None:
            raise RuntimeError("master_server 未设置")

        disks = master.get_vm_disk_info(vmid)
        current_size = 0; storage = ""
        for d in disks:
            if d['disk_name'] == disk_name:
                current_size = d['size_gb']; storage = d['storage']; break
        if not current_size:
            raise RuntimeError(f"未找到磁盘: {disk_name}")
        if new_size_gb == current_size:
            if logger: logger.info("[Resize] 大小相同，无需操作")
            return True

        if new_size_gb > current_size:
            if logger: logger.info("[Resize] 在线扩容: %dG -> %dG", current_size, new_size_gb)
            return master.expand_disk(vmid, disk_name, new_size_gb)

        # 缩容
        if not clonezilla_iso or not clonezilla_ip:
            raise RuntimeError("缩容需要 Clonezilla ISO 和 IP")
        if logger: logger.info("[Resize] 缩容: %dG -> %dG", current_size, new_size_gb)
        master.stop_vm(vmid)
        if not master.shrink_disk(vmid, disk_name, new_size_gb, storage):
            raise RuntimeError("磁盘缩容失败")
        if not master.add_cdrom_and_set_boot(vmid, "local", f"iso/{clonezilla_iso}", "seabios"):
            raise RuntimeError("Clonezilla 启动配置失败")
        master.start_vm(vmid)
        if logger: logger.info("[Resize] 等待 Clonezilla 启动（40s）...")
        time.sleep(40)
        self._send_gdisk_via_xdotool(clonezilla_ip, logger)
        time.sleep(10)
        master.stop_vm(vmid)
        master.release_clonezilla_config(vmid)
        master.update_disk_config_shrink(vmid, disk_name, new_size_gb)
        return True

    # ── End of DataCenter ───────────────────────────────────────────────

    def get_registered_vmid(self, image_id: str) -> int:
        """返回已注册镜像对应的 source_vm_id。"""
        sch = self.scheduler
        image = (getattr(sch, "images", {}) or {}).get(str(image_id))
        if image is None:
            raise RuntimeError(f"未找到已注册镜像：{image_id}")
        vmid = getattr(image, "source_vm_id", None)
        if vmid is None:
            vmid = image_id
        return int(vmid)

    def ensure_target_available(self, image_id: str, host_id: str) -> None:
        """检查目标主机是否被其它任务占用。"""
        sch = self.scheduler
        h2t = getattr(sch, "host_to_task", {}) or {}
        i2t = getattr(sch, "image_to_task", {}) or {}
        existing_host_task = str(h2t.get(str(host_id)) or "").strip()
        existing_image_task = str(i2t.get(str(image_id)) or "").strip()
        if existing_host_task and existing_host_task != existing_image_task:
            raise RuntimeError(f"目标主机 {host_id} 当前已被其它任务占用（task_id={existing_host_task}）")

    def release_baremetal(
        self, host_id: str, *,
        disk_name: str, baremetal_disk_gb: int,
        clonezilla_iso: str, clonezilla_ip: str,
        logger: logging.Logger | None = None,
    ) -> bool:
        """裸机释放：扩容→反向克隆→缩容到原始大小→释放。"""
        from core.baremetal_pipeline import ClonezillaMigrationPipeline

        master = self.master_server
        if master is None:
            raise RuntimeError("master_server 未设置")

        # 从 allocation 获取 VMID
        sch = self.scheduler
        allocations = getattr(sch, "allocations", {}) or {}
        vmid = 0
        for tid, alloc in allocations.items():
            try:
                if str(getattr(alloc, "host_id", "")) == str(host_id):
                    vmid = self.get_registered_vmid(str(getattr(alloc, "image_id", "")))
                    break
            except Exception:
                continue
        if not vmid:
            raise RuntimeError(f"无法找到裸机 {host_id} 关联的 VM")

        bm_host = self.find_host(host_id)
        if bm_host is None or not isinstance(bm_host, BareMetalHost):
            raise RuntimeError(f"主机 {host_id} 不是裸机")
        source_ip = str(getattr(bm_host, "ip", "") or "").strip()
        source_disk = str(getattr(bm_host, "target_disk", "") or "").strip()
        if not source_ip or not source_disk:
            raise RuntimeError(f"裸机 {host_id} 缺少 IP 或磁盘信息")

        # 1) 记录原始 VM 磁盘大小（缩容目标），扩容到比裸机略大
        disks = master.get_vm_disk_info(vmid)
        original_size = 0
        storage = ""
        for d in disks:
            if d['disk_name'] == disk_name:
                original_size = d['size_gb']
                storage = d['storage']
                break
        if not original_size:
            raise RuntimeError(f"未找到磁盘: {disk_name}")

        expand_size = max(baremetal_disk_gb + 1, original_size)
        if expand_size > original_size:
            if logger: logger.info("[Release] 扩容: %dG -> %dG (裸机=%dG)", original_size, expand_size, baremetal_disk_gb)
            if not master.expand_disk(vmid, disk_name, expand_size):
                raise RuntimeError("磁盘扩容失败")

        # 2) 反向 Clonezilla 克隆（Pipeline 处理 Clonezilla 启动配置和释放）
        if logger: logger.info("[Release] 反向克隆: %s(%s) -> VM %d(%s)",
                               host_id, source_disk, vmid, clonezilla_ip)
        clonezilla_img = (self.clonezilla_images or {}).get(clonezilla_iso)
        if clonezilla_img and getattr(clonezilla_img, "status", "free") != "free":
            raise RuntimeError(f"Clonezilla 镜像 {clonezilla_iso} 不空闲")
        if clonezilla_img:
            clonezilla_img.status = "busy"
        try:
            pipeline = ClonezillaMigrationPipeline(
                master_server=master, hosts=self.hosts,
                logger=logger, config=self.config)
            pipeline.execute_pipeline(
                source_ip=source_ip, target_ip=clonezilla_ip,
                source_disk=source_disk, target_disk="sda",
                image_id=str(vmid), logger=logger,
                vm_id=vmid, clonezilla_boot_mode="target")
        finally:
            if clonezilla_img:
                clonezilla_img.status = "free"

        # 3) 缩容回原始大小
        if expand_size > original_size:
            if logger: logger.info("[Release] 缩容: %dG -> %dG (原始)", expand_size, original_size)
            master.stop_vm(vmid)
            if not master.shrink_disk(vmid, disk_name, original_size, storage):
                raise RuntimeError("磁盘缩容失败，请手动检查 VM 状态")
            if not master.add_cdrom_and_set_boot(vmid, "local", f"iso/{clonezilla_iso}", "seabios"):
                raise RuntimeError("Clonezilla 启动配置失败")
            master.start_vm(vmid)
            if logger: logger.info("[Release] 等待 Clonezilla 启动（40秒）...")
            time.sleep(40)
            self._send_gdisk_via_xdotool(clonezilla_ip, logger)
            time.sleep(10)
            master.stop_vm(vmid)
            master.release_clonezilla_config(vmid)
            master.update_disk_config_shrink(vmid, disk_name, original_size)

        if logger: logger.info("[Release] 释放调度状态...")
        return self.release_task(host_id)

    @staticmethod
    def _send_gdisk_via_xdotool(clonezilla_ip: str, logger: logging.Logger | None = None) -> None:
        """通过 xdotool 向 Clonezilla 终端发送 gdisk 修复命令。"""
        import subprocess
        xterm_opts = '-geometry 80x20 -fa "Monospace" -fs 16'
        ssh_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null user@{clonezilla_ip}'
        if logger: logger.info("[Resize] 启动 Clonezilla gdisk 终端...")
        subprocess.Popen(
            f'xterm {xterm_opts} -title "CLONEZILLA GDISK" -e "{ssh_cmd}" &',
            shell=True, executable='/bin/bash')
        time.sleep(8)

        def _send(title, cmd):
            import subprocess as sp
            r = sp.run(f'xdotool search --name "{title}"', shell=True, capture_output=True, text=True)
            if r.stdout.strip():
                wid = r.stdout.strip().split('\n')[0]
                sp.run(['xdotool', 'windowactivate', wid], check=False)
                time.sleep(0.5)
                sp.run(['xdotool', 'type', cmd], check=False)
                time.sleep(0.2)
                sp.run(['xdotool', 'key', 'Return'], check=False)
            else:
                if logger: logger.warning("[Resize] 未找到 Clonezilla gdisk 窗口 '%s'，请手动修复分区表", title)

        T = "CLONEZILLA GDISK"
        _send(T, "sudo gdisk /dev/sda"); time.sleep(1)
        _send(T, "x"); time.sleep(1)
        _send(T, "e"); time.sleep(1)
        _send(T, "v"); time.sleep(2)
        _send(T, "w"); time.sleep(1)
        _send(T, "y"); time.sleep(1)
        _send(T, "exit"); time.sleep(1)
        _send(T, "poweroff")
        if logger: logger.info("[Resize] gdisk 修复命令已发送")

    def release_pve(self, host_id: str, *, logger: logging.Logger | None = None) -> bool:
        """PVE 主机 release：先迁回 master 再释放调度状态。裸机仅释放状态。"""
        master = self.master_server
        if master is None:
            raise RuntimeError("master_server 未设置")

        host = self.find_host(host_id)
        if host is None:
            raise RuntimeError(f"未找到主机：{host_id}")

        if not isinstance(host, PveHost):
            raise RuntimeError("当前仅支持 PVE 机器人主机执行迁回")

        # 获取关联的 allocation
        sch = self.scheduler
        allocations = getattr(sch, "allocations", {}) or {}
        task_id = None
        for tid, alloc in allocations.items():
            try:
                if str(getattr(alloc, "host_id", "")) == str(host_id):
                    task_id = str(tid)
                    break
            except Exception:
                continue
        if task_id is None:
            raise RuntimeError(f"未找到主机 {host_id} 关联的任务")

        alloc = allocations.get(str(task_id))
        image_id = str(getattr(alloc, "image_id", "") or "").strip()
        if not image_id:
            raise RuntimeError(f"任务 {task_id} 缺少关联 image_id")

        try:
            vmid = self.get_registered_vmid(image_id)
        except Exception as e:
            raise RuntimeError(f"无法解析迁回 VMID：{e}") from e

        current_node = self.find_vm_current_node(vmid)
        expected_node = str(getattr(host, "pve_node_name", "") or "").strip()
        if current_node and expected_node and str(current_node).strip() != expected_node:
            raise RuntimeError(
                f"VM {vmid} 当前位于节点 {current_node}，与主机 {host_id} 不一致，拒绝迁回。"
            )

        # 迁回：目标为 master
        try:
            self.deploy_pve(vmid, master.host_id, logger=logger)
        except Exception as e:
            raise RuntimeError(f"迁回主服务器失败：{e}") from e

        # 释放调度状态
        ok = bool(sch.release(str(task_id)))
        try:
            self.refresh_image_locations(min_interval_s=0.0)
        except Exception:
            pass
        return ok

    # ── End of DataCenter ───────────────────────────────────────────────
