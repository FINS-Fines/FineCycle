from __future__ import annotations

import json
import ipaddress
import os
import platform
import re
import shlex
import time
from typing import Any, Dict, Optional

import paramiko

from .base_host import PveCapableHost

try:
    from proxmoxer import ProxmoxAPI
except Exception:  # pragma: no cover
    ProxmoxAPI = None


class ServerHost(PveCapableHost):
    """数据中心服务器主机。

    主要职责：
    - 作为 source 节点执行 `qm` 操作（迁移前准备、迁移命令等）
    - PVE 集群发现与资源扫描
    - 模板/镜像管理（不涉及重启或网络配置修改）
    """

    def __init__(
        self,
        host_id: str,
        ip: str,
        hostname: str,
        pve_node_name: str,
        image_info: dict | None = None,
        *,
        connect_pve_api: bool = True,
        pve_api_username: str = 'root@pam',
        pve_api_password: str | None = None,
        pve_api_port: int = 8006,
        pve_verify_ssl: bool = False,
        ssh_username: str = 'root',
        ssh_password: str | None = None,
        ssh_port: int = 22,
        ssh_key_filename: str | None = None,
        ssh_timeout_s: float = 5.0,
    ):
        super().__init__(host_id, ip, hostname, image_info)
        self.pve_node_name = pve_node_name

        self.disk_localization_enabled = False

        # --- PVE API ---
        self.pve_api = None
        self._pve_api_username = pve_api_username
        self._pve_api_password = pve_api_password
        self._pve_api_port = pve_api_port
        self._pve_verify_ssl = pve_verify_ssl
        if connect_pve_api:
            self._connect_pve_api()

        # --- SSH (lazy connect) ---
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh_username = ssh_username
        self._ssh_password = ssh_password
        self._ssh_port = ssh_port
        self._ssh_key_filename = ssh_key_filename
        self._ssh_timeout_s = ssh_timeout_s

    def _connect_pve_api(self):
        if ProxmoxAPI is None:
            raise ImportError(
                "缺少依赖 proxmoxer：请先安装 `pip install proxmoxer`，用于连接 PVE API。"
            )
        host = f"{self.ip}:{self._pve_api_port}"
        self.pve_api = ProxmoxAPI(
            host,
            user=self._pve_api_username,
            password=self._pve_api_password,
            verify_ssl=self._pve_verify_ssl,
        )
        _ = self.pve_api.version.get()

    def _ensure_ssh_connected(self):
        try:
            transport = self.ssh_client.get_transport() if self.ssh_client else None
            if transport and transport.is_active():
                return
        except Exception:
            pass

        self.ssh_client.connect(
            hostname=self.ip,
            username=self._ssh_username,
            password=self._ssh_password,
            port=self._ssh_port,
            key_filename=self._ssh_key_filename,
            timeout=self._ssh_timeout_s,
        )

    def _run_ssh_command(self, cmd: str):
        self._ensure_ssh_connected()
        _stdin, stdout, stderr = self.ssh_client.exec_command(cmd)
        out = stdout.read().decode()
        err = stderr.read().decode()
        rc = stdout.channel.recv_exit_status()
        return out, err, rc

    def execute_ssh(self, cmd: str):
        out, err, _rc = self._run_ssh_command(cmd)
        return out, err

    def execute_ssh_with_status(self, cmd: str):
        return self._run_ssh_command(cmd)

    def check_health(self) -> bool:
        try:
            if self.pve_api is not None:
                _ = self.pve_api.version.get()
                return True
        except Exception:
            pass
        try:
            transport = self.ssh_client.get_transport() if self.ssh_client else None
            if transport and transport.is_active():
                transport.send_ignore()
                return True
        except Exception:
            pass
        try:
            if platform.system() == "Windows":
                result = os.system(f"ping -n 1 {self.ip} >nul 2>&1")
            else:
                result = os.system(f"ping -c 1 {self.ip} >/dev/null 2>&1")
            return result == 0
        except Exception:
            return False

    def get_internal_app_status(self) -> Dict[str, Any]:
        return {}

    # ------------------------------------------------------------------
    # PVE Cluster Discovery (moved from interactive_runner)
    # ------------------------------------------------------------------

    def pvesh_get_json(self, path: str, *, extra_args: str = "") -> Any:
        cmd = f"timeout 20s pvesh get {path} {extra_args} --output-format json"
        out, err = self.execute_ssh(cmd)
        if err and str(err).strip():
            self._emit(f"⚠ pvesh stderr: {str(err).strip()}")
        try:
            return json.loads(out or "null")
        except Exception as e:
            raise RuntimeError(f"解析 pvesh JSON 失败: {cmd!r}; stdout={out!r}") from e

    def get_nodes(self) -> list[dict[str, Any]]:
        data = self.pvesh_get_json("/nodes")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def list_node_vms(self, node: str) -> list[dict[str, Any]]:
        data = self.pvesh_get_json(f"/nodes/{node}/qemu")
        if not isinstance(data, list):
            return []
        vms: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            vmid = str(item.get("vmid") or "").strip()
            if not vmid:
                continue
            name = str(item.get("name") or "").strip() or f"vm-{vmid}"
            raw_template = item.get("template")
            is_template = bool(raw_template) and str(raw_template).strip().lower() not in {"0", "false", "none"}
            vms.append({
                "vmid": vmid, "name": name, "node": node,
                "type": "qemu", "template": is_template,
            })
        return vms

    def scan_clonezilla_isos(self, node: str) -> list[str]:
        data = self.pvesh_get_json(f"/nodes/{node}/storage/local/content")
        if not isinstance(data, list):
            return []
        iso_names: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            volid = str(item.get("volid") or "").strip()
            if not volid.startswith("local:iso/clonezilla"):
                continue
            if not volid.endswith(".iso"):
                continue
            iso_name = volid.split("local:iso/", 1)[-1].strip()
            if iso_name:
                iso_names.append(iso_name)
        seen: set[str] = set()
        out: list[str] = []
        for iso_name in iso_names:
            if iso_name not in seen:
                seen.add(iso_name)
                out.append(iso_name)
        return out

    def find_vm_current_node(self, vmid: int) -> Optional[str]:
        try:
            resources = self.pve_api.cluster.resources.get(type="vm") or []
        except Exception:
            return None
        for r in resources:
            if not isinstance(r, dict):
                continue
            if r.get("type") != "qemu":
                continue
            try:
                if int(r.get("vmid", 0)) == int(vmid):
                    node = r.get("node")
                    return str(node) if node else None
            except Exception:
                continue
        return None

    def node_ips_via_api(self, node_name: str) -> list[str]:
        try:
            net_list = self.pve_api.nodes(node_name).network.get()
        except Exception:
            return []
        ips: list[str] = []
        for item in net_list or []:
            if not isinstance(item, dict):
                continue
            addr = item.get("address")
            if isinstance(addr, str) and self._is_ipv4(addr) and not addr.startswith("127."):
                ips.append(addr)
            cidr = item.get("cidr")
            if isinstance(cidr, str) and "/" in cidr:
                maybe_ip = cidr.split("/", 1)[0].strip()
                if self._is_ipv4(maybe_ip) and not maybe_ip.startswith("127."):
                    ips.append(maybe_ip)
            ips.extend(self._extract_ipv4_candidates(str(item)))
        return self._unique_ips(ips)

    def node_ips_via_pvesh(self, node_name: str) -> list[str]:
        try:
            out, err = self.execute_ssh(
                f"pvesh get /nodes/{shlex.quote(str(node_name))}/network --output-format json"
            )
        except Exception:
            return []
        text = (out or "") + (err or "")
        try:
            data = json.loads(out or "[]")
        except Exception:
            return self._extract_ipv4_candidates(text)
        ips: list[str] = []
        if isinstance(data, list):
            for item in data:
                ips.extend(self._extract_ipv4_candidates(str(item)))
        else:
            ips.extend(self._extract_ipv4_candidates(str(data)))
        return self._unique_ips(ips)

    def resolve_node_ssh_ip(self, node_name: str) -> Optional[str]:
        ips = self.node_ips_via_api(node_name)
        if not ips:
            ips = self.node_ips_via_pvesh(node_name)
        return self._pick_best_ip(ips)

    # ------------------------------------------------------------------
    # IP / Network utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _is_ipv4(value: str) -> bool:
        try:
            return isinstance(ipaddress.ip_address(value.strip()), ipaddress.IPv4Address)
        except Exception:
            return False

    @classmethod
    def _extract_ipv4_candidates(cls, text: str) -> list[str]:
        candidates = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text or "")
        ips: list[str] = []
        for c in candidates:
            if cls._is_ipv4(c) and not c.startswith("127."):
                ips.append(c)
        return ips

    @staticmethod
    def _unique_ips(ips: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for ip in ips:
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
        return out

    @staticmethod
    def _pick_best_ip(ips: list[str]) -> Optional[str]:
        def score(ip: str) -> int:
            try:
                a = ipaddress.ip_address(ip)
            except Exception:
                return -100
            if a.is_loopback:
                return -100
            if a.is_link_local:
                return -50
            if a.is_private:
                return 100
            if a.is_global:
                return 10
            return 0
        best: Optional[str] = None
        best_score = -1000
        for ip in ips or []:
            s = score(ip)
            if s > best_score:
                best, best_score = ip, s
        return best

    @staticmethod
    def parse_clonezilla_ip(iso_name: str) -> Optional[str]:
        m = re.fullmatch(r"clonezilla-((?:\d{1,3}\.){3}\d{1,3})\.iso", str(iso_name or "").strip())
        if not m:
            return None
        ip_value = str(m.group(1) or "").strip()
        try:
            ipaddress.ip_address(ip_value)
            return ip_value
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Clonezilla CD-ROM boot preparation
    # ------------------------------------------------------------------

    def add_cdrom_and_set_boot(
        self, vm_id: int, iso_storage: str, iso_path: str, bios_type: str = "seabios",
    ) -> bool:
        """为 VM 挂载 Clonezilla ISO 并设置为仅从 CD-ROM 启动。"""
        self._emit(f"[{self.hostname}] 为VM {vm_id} 添加CD-ROM启动盘...")
        try:
            current_config = self.pve_api.nodes(self.pve_node_name).qemu(vm_id).config.get()
            cdrom_id = None
            for iface in ["ide0", "ide1", "ide2", "ide3"]:
                if iface not in current_config:
                    cdrom_id = iface; break
                value = current_config[iface]
                if isinstance(value, str) and 'media=cdrom' in value:
                    cdrom_id = iface; break
            if cdrom_id is None:
                raise Exception(f"VM {vm_id} 没有可用的IDE接口")

            cdrom_config = {cdrom_id: f"{iso_storage}:{iso_path},media=cdrom"}
            update_params = {**cdrom_config, 'boot': f"order={cdrom_id};", 'bootdisk': cdrom_id}
            if bios_type:
                update_params['bios'] = bios_type
            if bios_type == 'ovmf' and 'efidisk0' not in current_config:
                storage_list = self.pve_api.nodes(self.pve_node_name).storage.get()
                default_storage = 'local-lvm'
                for s in storage_list:
                    if s.get('content', '').find('images') != -1:
                        default_storage = s.get('storage'); break
                update_params['efidisk0'] = f'{default_storage}:1,format=raw'

            self.pve_api.nodes(self.pve_node_name).qemu(vm_id).config.post(**update_params)

            # 验证启动顺序
            time.sleep(1)
            vc = self.pve_api.nodes(self.pve_node_name).qemu(vm_id).config.get()
            if 'scsi0' in vc.get('boot', '') or 'virtio0' in vc.get('boot', ''):
                self._emit(f"[{self.hostname}] 检测到启动顺序含硬盘，强制修复...")
                self.pve_api.nodes(self.pve_node_name).qemu(vm_id).config.post(
                    boot=f"order={cdrom_id};", bootdisk=cdrom_id)

            # 保存配置快照供 release_clonezilla_config 恢复
            try:
                vm_info = getattr(self, '_clonezilla_vm_config', {})
                vm_info[str(vm_id)] = {
                    'cdrom_id': cdrom_id,
                    'original_bios': current_config.get('bios', ''),
                    'original_boot': current_config.get('boot', ''),
                    'had_efidisk': 'efidisk0' in current_config,
                    'original_boot_order': current_config.get('boot', ''),
                }
                setattr(self, '_clonezilla_vm_config', vm_info)
            except Exception:
                pass
            self._emit(f"[{self.hostname}] ✅ VM {vm_id} CD-ROM启动盘配置完成")
            return True
        except Exception as e:
            self._emit(f"[{self.hostname}] ❌ VM {vm_id} 配置失败: {e}")
            return False

    def start_vm_with_boot_from_cd(self, vm_id: int) -> bool:
        """启动 VM（已经配置为从 CD-ROM 启动）。"""
        self._emit(f"[{self.hostname}] 启动VM {vm_id} (从CD-ROM启动)...")
        try:
            vm_status = self.pve_api.nodes(self.pve_node_name).qemu(vm_id).status.current.get()
            if vm_status.get('status') == 'running':
                self._emit(f"[{self.hostname}] VM {vm_id} 正在运行，先停止...")
                self.pve_api.nodes(self.pve_node_name).qemu(vm_id).status.post('stop')
                timeout = 60
                while timeout > 0:
                    vm_status = self.pve_api.nodes(self.pve_node_name).qemu(vm_id).status.current.get()
                    if vm_status.get('status') != 'running':
                        break
                    time.sleep(2)
                    timeout -= 2
            self.pve_api.nodes(self.pve_node_name).qemu(vm_id).status.post('start')
            self._emit(f"[{self.hostname}] ✅ VM {vm_id} 已启动")
            return True
        except Exception as e:
            self._emit(f"[{self.hostname}] ❌ VM {vm_id} 启动失败: {e}")
            return False

    def release_clonezilla_config(self, vm_id: int) -> bool:
        """释放 Clonezilla 配置：停止 VM → 移除 CD/DVD → 恢复 UEFI 启动 → 清理配置。"""
        self._emit(f"[{self.hostname}] 为VM {vm_id} 移除Clonezilla配置...")
        try:
            # 1. Stop VM if running
            vm_status = self.pve_api.nodes(self.pve_node_name).qemu(vm_id).status.current.get()
            if vm_status.get('status') == 'running':
                self._emit(f"[{self.hostname}] VM {vm_id} 正在运行，先停止...")
                self.pve_api.nodes(self.pve_node_name).qemu(vm_id).status.post('stop')
                timeout = 60
                while timeout > 0:
                    vm_status = self.pve_api.nodes(self.pve_node_name).qemu(vm_id).status.current.get()
                    if vm_status.get('status') != 'running':
                        self._emit(f"[{self.hostname}] VM {vm_id} 已停止")
                        break
                    time.sleep(2)
                    timeout -= 2

            current_config = self.pve_api.nodes(self.pve_node_name).qemu(vm_id).config.get()

            saved_config = {}
            try:
                vm_info = getattr(self, '_clonezilla_vm_config', {})
                saved_config = vm_info.get(str(vm_id), {})
            except Exception:
                pass

            # Remove CD-ROM devices
            devices_to_delete = []
            cdrom_id = saved_config.get('cdrom_id', '')
            if cdrom_id and cdrom_id in current_config:
                current_value = current_config.get(cdrom_id)
                if current_value and 'media=cdrom' in str(current_value):
                    devices_to_delete.append(cdrom_id)
            else:
                for i in range(4):
                    ide_key = f"ide{i}"
                    if ide_key in current_config:
                        value = current_config[ide_key]
                        if isinstance(value, str) and 'media=cdrom' in value:
                            devices_to_delete.append(ide_key)
                            break

            if devices_to_delete:
                for device in devices_to_delete:
                    self.pve_api.nodes(self.pve_node_name).qemu(vm_id).config.post(delete=device)

            update_params = {}
            original_bios = saved_config.get('original_bios', '')
            if original_bios and current_config.get('bios', '') != original_bios:
                update_params['bios'] = original_bios

            original_boot = saved_config.get('original_boot', '')
            if original_boot and current_config.get('boot', '') != original_boot:
                update_params['boot'] = original_boot

            had_efidisk = saved_config.get('had_efidisk', False)
            if not had_efidisk and 'efidisk0' in current_config:
                self.pve_api.nodes(self.pve_node_name).qemu(vm_id).config.post(delete='efidisk0')

            if update_params:
                self.pve_api.nodes(self.pve_node_name).qemu(vm_id).config.post(**update_params)

            # Clean saved config
            try:
                vm_info = getattr(self, '_clonezilla_vm_config', {})
                vm_info.pop(str(vm_id), None)
                setattr(self, '_clonezilla_vm_config', vm_info)
            except Exception:
                pass

            return True
        except Exception as e:
            self._emit(f"[{self.hostname}] ❌ VM {vm_id} 配置恢复失败: {e}")
            return False


    # ------------------------------------------------------------------
    # Disk operations
    # ------------------------------------------------------------------

    def get_vm_disk_info(self, vmid: int) -> list:
        """获取 VM 的所有磁盘信息（名称/设备/大小/存储/格式）。"""
        disks = []
        try:
            config = self.pve_api.nodes(self.pve_node_name).qemu(vmid).config.get()
            for key, value in config.items():
                for pattern in ['virtio', 'scsi', 'sata', 'ide']:
                    if key.startswith(pattern):
                        disks.append({
                            'disk_name': key,
                            'disk_device': self._extract_disk_device(value),
                            'size_gb': self._extract_disk_size_gb(value),
                            'storage': self._extract_disk_storage(value),
                            'format': self._extract_disk_format(value),
                        })
                        break
        except Exception as e:
            self._emit(f"[{self.hostname}] 获取 VM {vmid} 磁盘信息失败: {e}")
        return disks

    @staticmethod
    def _extract_disk_device(config_value: str) -> str:
        if ':' in config_value:
            part = config_value.split(':', 1)[1]
            return part.split(',')[0] if ',' in part else part
        return ""

    @staticmethod
    def _extract_disk_size_gb(config_value: str) -> int:
        m = re.search(r'size=(\d+)(?:G|GB)?', config_value, re.IGNORECASE)
        if m: return int(m.group(1))
        m = re.search(r'size=(\d+)M', config_value, re.IGNORECASE)
        if m: return int(int(m.group(1)) / 1024)
        return 0

    @staticmethod
    def _extract_disk_storage(config_value: str) -> str:
        return config_value.split(':', 1)[0] if ':' in config_value else ""

    @staticmethod
    def _extract_disk_format(config_value: str) -> str:
        m = re.search(r'format=(\w+)', config_value)
        return m.group(1) if m else "qcow2"

    def get_disk_path(self, vmid: int, disk_device: str, storage: str) -> str:
        # 1) local-lvm: try LVM block device
        if storage == 'local-lvm':
            lvm_name = disk_device.replace('-', '--')
            lvm_path = f"/dev/mapper/pve-{lvm_name}"
            try:
                out, _ = self.execute_ssh(f"test -b {lvm_path} && echo 'exists'")
                if "exists" in out: return lvm_path
            except Exception: pass
        # 2) directory storage: try default qcow2 path
        qcow2_path = f"/var/lib/vz/images/{vmid}/{disk_device}.qcow2"
        try:
            out, _ = self.execute_ssh(f"test -f {qcow2_path} && echo 'exists'")
            if "exists" in out: return qcow2_path
        except Exception: pass
        # 3) fallback: search from root — handles custom storage mounts (e.g. /mnt/pve/VMs/)
        search_name = disk_device.split('/')[-1]
        find_cmd = f"find / -name '*{search_name}' 2>/dev/null | head -1"
        self._emit(f"[{self.hostname}] 查找磁盘: {find_cmd}")
        try:
            out, _ = self.execute_ssh(find_cmd)
            path = (out or "").strip()
            self._emit(f"[{self.hostname}] find 结果: {path}")
            if path: return path
        except Exception: pass
        return qcow2_path

    def expand_disk(self, vmid: int, disk_name: str, size_gb: int) -> bool:
        try:
            out, _ = self.execute_ssh(f"qm resize {vmid} {disk_name} {size_gb}G")
            return True
        except Exception as e:
            self._emit(f"[{self.hostname}] qm resize 失败: {e}")
            return False

    def shrink_disk(self, vmid: int, disk_name: str, size_gb: int, storage: str = "") -> bool:
        disks = self.get_vm_disk_info(vmid)
        for d in disks:
            if d['disk_name'] == disk_name:
                storage = storage or d['storage']
                disk_path = self.get_disk_path(vmid, d['disk_device'], storage)
                break
        else:
            disk_path = ""
        if not disk_path:
            self._emit(f"[{self.hostname}] 未找到磁盘: {disk_name}")
            return False
        self._emit(f"[{self.hostname}] 缩容 {disk_path} -> {size_gb}G")
        try:
            out, err = self.execute_ssh(f"qemu-img resize --shrink {disk_path} {size_gb}G")
            if err and ("error" in err.lower() or "failed" in err.lower()):
                if "smaller than current size" in err.lower():
                    self._emit(f"[{self.hostname}] 磁盘已经是目标大小或更小")
                    return True
                self._emit(f"[{self.hostname}] qemu-img resize 失败: {err}")
                return False
            self._emit(f"[{self.hostname}] qemu-img resize 完成")
            return True
        except Exception as e:
            self._emit(f"[{self.hostname}] qemu-img resize 异常: {e}")
            return False

    def update_disk_config_shrink(self, vmid: int, disk_name: str, size_gb: int) -> bool:
        """缩容后更新 VM 配置文件中的磁盘大小（匹配 1.1 DiskManager._update_vm_disk_size_shrink）"""
        config_path = f"/etc/pve/qemu-server/{vmid}.conf"
        # 备份
        try:
            self.execute_ssh(f"cp {config_path} {config_path}.bak.$(date +%Y%m%d%H%M%S)")
        except Exception as e:
            self._emit(f"[{self.hostname}] 备份配置失败: {e}")
        # 查看当前配置行
        debug_out, _ = self.execute_ssh(f"grep '^{disk_name}:' {config_path}")
        self._emit(f"[{self.hostname}] 当前配置行: {(debug_out or '').strip()}")
        # 方法1: sed 精确替换 size=xxxG
        sed_cmd = (
            f"sed -i 's/\\({disk_name}: [^,]*,\\)size=[0-9]*G/\\1size={size_gb}G/' {config_path}"
        )
        self._emit(f"[{self.hostname}] 修改配置: {sed_cmd}")
        try:
            out, err = self.execute_ssh(sed_cmd)
            if err and "error" in err.lower():
                self._emit(f"[{self.hostname}] sed 失败: {err}")
                return False
            verify_out, _ = self.execute_ssh(f"grep '^{disk_name}:' {config_path}")
            self._emit(f"[{self.hostname}] 修改后配置行: {(verify_out or '').strip()}")
            # 验证修改结果
            expected = f"size={size_gb}G"
            if expected not in (verify_out or ""):
                self._emit(f"[{self.hostname}] sed 未生效，尝试 perl 备选方案...")
                # 方法2: perl 非贪婪匹配，兼容任意参数顺序
                perl_cmd = (
                    f"perl -i -pe 's/({disk_name}:.*?)size=\\d+G/$1size={size_gb}G/' {config_path}"
                )
                self.execute_ssh(perl_cmd)
                verify_out2, _ = self.execute_ssh(f"grep '^{disk_name}:' {config_path}")
                self._emit(f"[{self.hostname}] perl 后配置行: {(verify_out2 or '').strip()}")
                if expected not in (verify_out2 or ""):
                    self._emit(f"[{self.hostname}] 配置修改失败，请手动更新 {config_path}")
                    return False
            self._emit(f"[{self.hostname}] 配置文件磁盘大小已更新为 {size_gb}G")
            return True
        except Exception as e:
            self._emit(f"[{self.hostname}] 修改配置异常: {e}")
            return False

    def stop_vm(self, vmid: int, timeout: int = 120) -> None:
        try:
            self.pve_api.nodes(self.pve_node_name).qemu(vmid).status.post('shutdown')
            time.sleep(5)
            for _ in range(timeout // 2):
                status = self.pve_api.nodes(self.pve_node_name).qemu(vmid).status.current.get()
                if status.get('status') != 'running': return
                time.sleep(2)
            self.pve_api.nodes(self.pve_node_name).qemu(vmid).status.post('stop')
            time.sleep(3)
        except Exception as e:
            self._emit(f"[{self.hostname}] 停止 VM {vmid} 出错: {e}")

    def start_vm(self, vmid: int) -> None:
        try:
            self.pve_api.nodes(self.pve_node_name).qemu(vmid).status.post('start')
            time.sleep(3)
        except Exception as e:
            self._emit(f"[{self.hostname}] 启动 VM {vmid} 出错: {e}")
