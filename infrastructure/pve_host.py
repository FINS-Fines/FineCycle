# infrastructure/pve_host.py
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Any

from .base_host import PveCapableHost
import time
import os
import platform
import subprocess
import ipaddress
import paramiko
import re
import hashlib
from configs.netconf_templates import WIRED_TEMPLATE, WIRELESS_TEMPLATE

try:
    from proxmoxer import ProxmoxAPI
except Exception:  # pragma: no cover
    ProxmoxAPI = None

if TYPE_CHECKING:
    from .server_host import ServerHost


class PveHost(PveCapableHost):
    """
    安装了PVE的机器人主机。
    
    特点：作为接收端，接受来自Server的VM迁移。
    """
    def __init__(
        self,
        host_id: str,
        ip: str,
        hostname: str,
        pve_node_name: str,
        network_config: dict = None,
        image_info: dict = None,
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
        self.network_config = network_config

        # 机器人主机落地后需要将磁盘从 VMs 迁回 local-lvm（只在最终落地这一步做一次数据搬运）。
        self.disk_localization_enabled = True

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

        if self.network_config:
            self.upload_network_profiles()

    # --- 基础连接 相关 ---
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

    # --- 网络配置相关 ---
    @staticmethod
    def _netmask_to_prefixlen(netmask: str) -> int:
        """Convert dotted netmask (e.g. 255.255.255.0) to prefix length (e.g. 24)."""
        try:
            nm = str(netmask).strip()
            network = ipaddress.IPv4Network(f"0.0.0.0/{nm}")
            return int(network.prefixlen)
        except Exception as e:
            raise ValueError(f"Invalid netmask: {netmask}") from e

    @classmethod
    def _normalize_ipv4_cidr(
        cls,
        ip_value: str,
        *,
        netmask: str | None = None,
        prefixlen: int | None = None,
        default_prefixlen: int = 24,
    ) -> str:
        """Normalize IPv4 address input into CIDR form.

        Accepts:
        - "192.168.8.200/21" (already CIDR)
        - "192.168.8.200" + prefixlen
        - "192.168.8.200" + netmask
        """
        raw = str(ip_value or "").strip()
        if not raw:
            raise ValueError("ip_value is empty")

        if "/" in raw:
            iface = ipaddress.IPv4Interface(raw)
            return str(iface)

        ipaddress.IPv4Address(raw)

        if prefixlen is not None:
            return f"{raw}/{int(prefixlen)}"
        if netmask:
            return f"{raw}/{cls._netmask_to_prefixlen(netmask)}"
        return f"{raw}/{int(default_prefixlen)}"

    def _read_remote_file_text(self, filepath: str) -> str:
        self._ensure_ssh_connected()
        sftp = self.ssh_client.open_sftp()
        try:
            with sftp.file(filepath, 'r') as f:
                data = f.read()
                if isinstance(data, bytes):
                    return data.decode(errors='replace')
                return str(data)
        finally:
            sftp.close()

    @staticmethod
    def _parse_ifupdown_interfaces(content: str) -> dict[str, dict[str, str]]:
        """Parse Debian ifupdown /etc/network/interfaces style content.

        Returns: iface_name -> options dict (address/netmask/gateway/bridge-ports/wpa-ssid/wpa-psk/...)
        """
        blocks: dict[str, dict[str, str]] = {}
        current_iface: str | None = None

        for raw_line in (content or '').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue

            m = re.match(r'^iface\s+(\S+)\s+inet\s+(\S+)', line)
            if m:
                current_iface = m.group(1)
                blocks.setdefault(current_iface, {})
                continue

            if current_iface is None:
                continue

            # key value
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key, value = parts[0].strip(), parts[1].strip()
            blocks[current_iface][key] = value

        return blocks

    @classmethod
    def _extract_network_config_from_interfaces(cls, content: str) -> dict[str, Any]:
        blocks = cls._parse_ifupdown_interfaces(content)

        cfg: dict[str, Any] = {}

        # wired: vmbr0
        vmbr0 = blocks.get('vmbr0') or {}
        if vmbr0.get('gateway'):
            cfg['gateway'] = vmbr0.get('gateway')
        if vmbr0.get('bridge-ports'):
            cfg['wired_iface'] = vmbr0.get('bridge-ports')

        addr = vmbr0.get('address')
        if addr:
            # support old style: address + netmask
            if '/' in addr:
                cfg['wired_ip'] = str(ipaddress.IPv4Interface(addr))
                cfg['prefixlen'] = int(ipaddress.IPv4Interface(addr).network.prefixlen)
            else:
                nm = vmbr0.get('netmask')
                cidr = cls._normalize_ipv4_cidr(addr, netmask=nm)
                cfg['wired_ip'] = cidr
                cfg['prefixlen'] = int(ipaddress.IPv4Interface(cidr).network.prefixlen)

        # wireless: pick the iface block that contains wpa-ssid or wpa-psk first
        wifi_iface = None
        wifi_block: dict[str, str] | None = None
        for iface_name, opts in blocks.items():
            if 'wpa-ssid' in opts or 'wpa-psk' in opts:
                wifi_iface = iface_name
                wifi_block = opts
                break
        if wifi_block is None:
            # fallback: wlan0
            if 'wlan0' in blocks:
                wifi_iface = 'wlan0'
                wifi_block = blocks.get('wlan0')

        if wifi_iface and wifi_block:
            cfg['wifi_iface'] = wifi_iface
            if wifi_block.get('wpa-ssid'):
                cfg['wifi_ssid'] = wifi_block.get('wpa-ssid')
            if wifi_block.get('wpa-psk'):
                cfg['wifi_psk'] = wifi_block.get('wpa-psk')
            if wifi_block.get('gateway') and not cfg.get('gateway'):
                cfg['gateway'] = wifi_block.get('gateway')

            waddr = wifi_block.get('address')
            if waddr:
                if '/' in waddr:
                    cfg['wireless_ip'] = str(ipaddress.IPv4Interface(waddr))
                    cfg.setdefault('prefixlen', int(ipaddress.IPv4Interface(waddr).network.prefixlen))
                else:
                    nm = wifi_block.get('netmask')
                    cidr = cls._normalize_ipv4_cidr(waddr, netmask=nm)
                    cfg['wireless_ip'] = cidr
                    cfg.setdefault('prefixlen', int(ipaddress.IPv4Interface(cidr).network.prefixlen))

        return cfg

    def detect_network_config(self) -> dict[str, Any]:
        """Read remote interfaces files and infer config for automation.

        Priority:
        - /etc/network/interfaces.wired
        - /etc/network/interfaces.wireless
        - /etc/network/interfaces
        """
        result: dict[str, Any] = {}

        def merge(src: dict[str, Any]):
            for k, v in (src or {}).items():
                if v is None or v == '':
                    continue
                # don't overwrite existing
                result.setdefault(k, v)

        for path in (
            '/etc/network/interfaces.wired',
            '/etc/network/interfaces.wireless',
            '/etc/network/interfaces',
        ):
            try:
                content = self._read_remote_file_text(path)
            except Exception:
                continue
            merge(self._extract_network_config_from_interfaces(content))

        # Ensure IPs have CIDR when possible
        if result.get('wired_ip') and '/' in str(result['wired_ip']):
            result['wired_ip'] = str(ipaddress.IPv4Interface(str(result['wired_ip'])))
        if result.get('wireless_ip') and '/' in str(result['wireless_ip']):
            result['wireless_ip'] = str(ipaddress.IPv4Interface(str(result['wireless_ip'])))

        return result

    def detect_active_network_config(self) -> dict[str, Any]:
        """仅读取当前生效的 /etc/network/interfaces 并推断网络参数。

        说明：为了避免引入脏数据风险，这里不读取/解析 /etc/network/interfaces.wired
        或 /etc/network/interfaces.wireless。
        """

        try:
            content = self._read_remote_file_text('/etc/network/interfaces')
        except Exception:
            return {}
        cfg = self._extract_network_config_from_interfaces(content)
        # Normalize CIDR strings
        if cfg.get('wired_ip') and '/' in str(cfg['wired_ip']):
            cfg['wired_ip'] = str(ipaddress.IPv4Interface(str(cfg['wired_ip'])))
        if cfg.get('wireless_ip') and '/' in str(cfg['wireless_ip']):
            cfg['wireless_ip'] = str(ipaddress.IPv4Interface(str(cfg['wireless_ip'])))
        return cfg

    @classmethod
    def build_network_config_from_probe(
        cls,
        probe: "PveHost",
        fallback_ip: str,
        *,
        default_gateway: str = "192.168.8.1",
        default_prefixlen: int = 21,
        default_wired_iface: str = "enp86s0",
        default_wifi_iface: str = "wlo1",
    ) -> dict[str, Any]:
        """自动探测并构建 network_config，使用环境变量兜底。

        可用环境变量：
        - PVE_GATEWAY, PVE_PREFIXLEN / PVE_NETMASK
        - PVE_WIRED_IP, PVE_WIRELESS_IP
        - PVE_WIRED_IFACE, PVE_WIFI_IFACE
        - PVE_WIFI_SSID, PVE_WIFI_PSK
        """
        detected: dict[str, Any] = {}
        try:
            detected = probe.detect_network_config() or {}
        except Exception:
            detected = {}

        detected_gateway = (
            str(detected.get("gateway") or "").strip()
            or os.getenv("PVE_GATEWAY")
            or str(default_gateway)
        )
        detected_prefixlen = detected.get("prefixlen")
        detected_wired_ip = (
            str(detected.get("wired_ip") or "").strip()
            or os.getenv("PVE_WIRED_IP")
            or fallback_ip
        )
        detected_wireless_ip = (
            str(detected.get("wireless_ip") or "").strip()
            or os.getenv("PVE_WIRELESS_IP")
            or fallback_ip
        )
        detected_wired_iface = (
            str(detected.get("wired_iface") or "").strip()
            or os.getenv("PVE_WIRED_IFACE")
            or str(default_wired_iface)
        )
        detected_wifi_iface = (
            str(detected.get("wifi_iface") or "").strip()
            or os.getenv("PVE_WIFI_IFACE")
            or str(default_wifi_iface)
        )
        detected_wifi_ssid = (
            str(detected.get("wifi_ssid") or "").strip()
            or os.getenv("PVE_WIFI_SSID")
            or ""
        )
        detected_wifi_psk = (
            str(detected.get("wifi_psk") or "").strip()
            or os.getenv("PVE_WIFI_PSK")
        )

        gateway = detected_gateway
        if not gateway:
            raise RuntimeError("gateway 为空：远程探测与环境变量均未提供 PVE_GATEWAY")

        prefixlen_raw: str | None = os.getenv("PVE_PREFIXLEN")
        if not prefixlen_raw:
            if detected_prefixlen is not None and str(detected_prefixlen).strip():
                prefixlen_raw = str(detected_prefixlen)
            else:
                detected_netmask = str(detected.get("netmask") or "").strip()
                if detected_netmask:
                    try:
                        prefixlen_raw = str(cls._netmask_to_prefixlen(detected_netmask))
                    except Exception:
                        prefixlen_raw = None
        if not prefixlen_raw and os.getenv("PVE_NETMASK"):
            try:
                prefixlen_raw = str(cls._netmask_to_prefixlen(os.getenv("PVE_NETMASK", "")))
            except Exception:
                prefixlen_raw = None
        prefixlen_raw = prefixlen_raw or str(default_prefixlen)
        try:
            prefixlen = cls._parse_prefixlen(prefixlen_raw)
        except Exception:
            prefixlen = cls._netmask_to_prefixlen(prefixlen_raw)

        wired_ip = cls._normalize_ipv4_cidr(detected_wired_ip, prefixlen=prefixlen)
        wireless_ip = cls._normalize_ipv4_cidr(detected_wireless_ip, prefixlen=prefixlen)

        return {
            "gateway": gateway,
            "prefixlen": prefixlen,
            "wired_ip": wired_ip,
            "wireless_ip": wireless_ip,
            "wired_iface": detected_wired_iface,
            "wifi_iface": detected_wifi_iface,
            "wifi_ssid": detected_wifi_ssid,
            "wifi_psk": detected_wifi_psk,
        }

    @classmethod
    def _parse_prefixlen(cls, value: str) -> int:
        v = (value or "").strip()
        if v.startswith("/"):
            v = v[1:].strip()
        if not v.isdigit():
            raise ValueError(f"Invalid prefix length: {value}")
        p = int(v)
        if not (0 <= p <= 32):
            raise ValueError(f"Invalid prefix length: {value}")
        return p

    def upload_network_profiles(self):
        """生成并上传 wired/wireless 配置文件到 PVE 主机。

        规则：
        - 不解析/修改远端已有的 interfaces.* 文件，避免脏数据。
        - Hash Compare & Overwrite：仅当远端文件不存在或 MD5 不一致时才覆盖写入。
        """
        self._emit(f"[{self.hostname}] Uploading network profiles (hash-compare overwrite)...")

        # 支持两种输入：
        # 1) 直接提供 CIDR（如 wired_ip: 192.168.8.200/21）
        # 2) 继续提供旧式 netmask/prefixlen，我们会自动转换
        netmask = self.network_config.get('netmask')
        default_prefixlen = self.network_config.get('prefixlen')
        wired_prefixlen = self.network_config.get('wired_prefixlen', default_prefixlen)
        wireless_prefixlen = self.network_config.get('wireless_prefixlen', default_prefixlen)

        wired_ip_raw = self.network_config.get('wired_ip', self.ip)
        wireless_ip_raw = self.network_config.get('wireless_ip', self.ip)

        wired_ip_cidr = self._normalize_ipv4_cidr(
            wired_ip_raw,
            netmask=netmask,
            prefixlen=wired_prefixlen,
        )
        wireless_ip_cidr = self._normalize_ipv4_cidr(
            wireless_ip_raw,
            netmask=netmask,
            prefixlen=wireless_prefixlen,
        )
        
        # 1. 渲染有线配置
        wired_content = WIRED_TEMPLATE.format(
            ip=wired_ip_cidr,
            gateway=self.network_config.get('gateway'),
            physical_interface=self.network_config.get('wired_iface', 'eth0')
        )
        
        # 2. 渲染无线配置
        wireless_content = WIRELESS_TEMPLATE.format(
            ip=wireless_ip_cidr, # 注意：通常无线IP不同
            gateway=self.network_config.get('gateway'),
            wifi_interface=self.network_config.get('wifi_iface', 'wlan0'),
            ssid=self.network_config.get('wifi_ssid'),
            psk=self.network_config.get('wifi_psk')
        )

        def md5_local(text: str) -> str:
            return hashlib.md5((text or "").encode("utf-8")).hexdigest()

        def md5_remote(path: str) -> str | None:
            try:
                out, _ = self.execute_ssh(f"test -f {path} && md5sum {path} | awk '{{print $1}}' || true")
                v = (out or "").strip().splitlines()
                return v[-1].strip() if v and v[-1].strip() else None
            except Exception:
                return None

        def ensure_file(path: str, content: str) -> None:
            local = md5_local(content)
            remote = md5_remote(path)
            if remote == local:
                self._emit(f"[{self.hostname}] OK: {path} already up-to-date (md5={local})")
                return
            self._write_remote_file(path, content)
            self._emit(f"[{self.hostname}] Updated: {path} (md5 {remote or 'missing'} -> {local})")

        try:
            ensure_file("/etc/network/interfaces.wired", wired_content)
            ensure_file("/etc/network/interfaces.wireless", wireless_content)
            self._emit(f"[{self.hostname}] Network profiles ready.")
        except Exception as e:
            self._emit(f"[{self.hostname}] Failed to upload network profiles: {e}")

    def _write_remote_file(self, filepath, content):
        """辅助函数：通过 SSH 写入文件"""
        # 确保 SSH 已连接，否则 open_sftp() 会失败
        self._ensure_ssh_connected()
        sftp = self.ssh_client.open_sftp()
        with sftp.file(filepath, 'w') as f:
            f.write(content)
        sftp.close()

    # --- 基础控制 ---
    def _run_ssh_command(self, cmd):
        """通过SSH执行命令，带自动重连机制"""
        try:
            # 尝试发送心跳包检测连接是否存活
            if self.ssh_client.get_transport():
                self.ssh_client.get_transport().send_ignore()
        except Exception:
            self._emit(f"[{self.hostname}] SSH连接已断开，正在重连...")
            try:
                self._ensure_ssh_connected()
                self._emit(f"[{self.hostname}] SSH重连成功。")
            except Exception as e:
                self._emit(f"[{self.hostname}] SSH重连失败: {e}")
                raise e

        # 确保已连接
        self._ensure_ssh_connected()

        # 执行命令
        _stdin, stdout, stderr = self.ssh_client.exec_command(cmd)
        out = stdout.read().decode()
        err = stderr.read().decode()
        rc = stdout.channel.recv_exit_status()
        return out, err, rc

    def execute_ssh(self, cmd):
        out, err, _rc = self._run_ssh_command(cmd)
        return out, err

    def execute_ssh_with_status(self, cmd):
        return self._run_ssh_command(cmd)

    def wait_for_connection(self, timeout=300) -> bool:
        """测试是否能够连到机器人主机 (Ping/SSH)"""
        self._emit(f"[{self.hostname}] Waiting for connection (Timeout: {timeout}s)...")
        end_time = time.time() + timeout
        ping_cmd = "ping -n 1" if platform.system() == "Windows" else "ping -c 1"
        while time.time() < end_time:
            response = os.system(f"{ping_cmd} {self.ip} >nul 2>&1" if platform.system() == "Windows" else f"{ping_cmd} {self.ip} >/dev/null 2>&1")
            if response == 0:
                self._emit(f"[{self.hostname}] Connection successful.")
                return True
            time.sleep(2)
        self._emit(f"[{self.hostname}] Connection timeout.")
        return False

    def reboot_system(self):
        """重启PVE物理主机"""
        self._emit(f"[{self.hostname}] Rebooting system...")
        try:
            cmd = f"reboot"  # 以root身份通过SSH重启
            _, stderr = self.execute_ssh(cmd)
            if not stderr:
                self._emit(f"[{self.hostname}] Reboot command sent successfully.")
                return True
            else:
                self._emit(f"[{self.hostname}] Reboot failed: {stderr}")
                return False
        except Exception as e:
            self._emit(f"[{self.hostname}] Reboot error: {e}")
            return False

    def check_health(self) -> bool:
        """检测PVE主机是否在线（优先检查SSH连接）。"""
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

        # fallback：ping
        try:
            if platform.system() == "Windows":
                result = os.system(f"ping -n 1 {self.ip} >nul 2>&1")
            else:
                result = os.system(f"ping -c 1 {self.ip} >/dev/null 2>&1")
            return result == 0
        except Exception:
            return False

    def get_internal_app_status(self) -> Dict[str, Any]:
        """返回主机内部应用状态（占位实现）。"""
        return {}

    def configure_network(self, mode: str) -> bool:
        """
        远程修改 /etc/network/interfaces
        input: mode ('wired' | 'wireless')
        """
        target_file = f"/etc/network/interfaces.{mode}"
        self._emit(f"[{self.hostname}] Switching network config to MODE: {mode}...")
        
        cmd_check = f"ls {target_file}"
        stdout, _ = self.execute_ssh(cmd_check)
        if target_file not in stdout:
            self._emit(f"[{self.hostname}] Error: Target config profile {target_file} not found!")
            return False

        try:
            # 1) 强制备份当前配置（时间戳），确保可回滚
            ts_out, _ = self.execute_ssh("date +%Y%m%d_%H%M%S")
            ts = (ts_out or "").strip().splitlines()[-1] if (ts_out or "").strip() else "unknown"
            backup = f"/etc/network/interfaces.bak_{ts}"
            self.execute_ssh(f"cp /etc/network/interfaces {backup}")
            self._emit(f"[{self.hostname}] Backed up /etc/network/interfaces -> {backup}")

            # 2) 覆盖
            cmd_switch = f"cp {target_file} /etc/network/interfaces"
            self.execute_ssh(cmd_switch)
            
            # 注意：这里我们不立即重启网络服务，因为 reboot_system() 会在外部被调用
            # 如果不重启系统，仅仅想重载网络，可以用 `ifreload -a` (需安装 ifupdown2)
            
            self._emit(f"[{self.hostname}] Network config switched to {mode}. Pending reboot.")
            return True
        except Exception as e:
            self._emit(f"[{self.hostname}] Switch network error: {e}")
            return False



    def update_application(self, repository_info: dict) -> str:
        """
        通知PVE机器人主机拉取最新代码并重启应用
        
        input: repository_info (包含repo_url, branch, repo_path, restart_cmd)
        
        output: str (执行结果日志)
        """
        repo_url = repository_info.get('repo_url')
        branch = repository_info.get('branch', 'main') # 默认main分支
        repo_path = repository_info.get('repo_path', '/root/app')
        restart_cmd = repository_info.get('restart_cmd', '')

        logs = []
        try:
            # 进入代码目录
            cmd_cd = f"cd {repo_path}"
            self.execute_ssh(cmd_cd)
            # 切换分支
            cmd_checkout = f"cd {repo_path} && git fetch && git checkout {branch}"
            out, err = self.execute_ssh(cmd_checkout)
            logs.append(out + err)
            # 拉取最新代码
            cmd_pull = f"cd {repo_path} && git pull {repo_url} {branch}"
            out, err = self.execute_ssh(cmd_pull)
            logs.append(out + err)
            # 重启应用
            if restart_cmd:
                out, err = self.execute_ssh(f"cd {repo_path} && {restart_cmd}")
                logs.append(out + err)
            return "\n".join(logs)
        except Exception as e:
            return f"Update failed: {e}"
