# infrastructure/base_host.py
import threading
import json
import re
import shlex
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from domain.image import RobotAppImage

class BaseHost(ABC):
    """
    主机抽象基类
    
    定义所有类型主机（服务器、PVE机器人、裸机机器人）必须具备的能力。
    
    增加：后台监听能力，通过 Topic (host_id) 动态更新 image_info。
    """
    def __init__(self, host_id: str, ip: str, hostname: str, image_info: dict = None):
        self.host_id = host_id
        self.ip = ip
        self.hostname = hostname
        self.image_info = image_info
        self.images: Dict[str, RobotAppImage] = {}
        self.is_online = False

        # 控制“迁移后是否需要将磁盘从 VMs 落盘到 local-lvm”。
        # - None: 保持旧行为（由调用方/宿主类决定，默认为 True）
        # - True: 迁移后执行 VMs -> local-lvm（通常用于机器人主机）
        # - False: 迁移后保持磁盘在 VMs（通常用于服务器节点，避免无意义数据搬运）
        self.disk_localization_enabled: Optional[bool] = None

        # 当后台线程/调度器执行流水线时，可将 quiet=True 以避免 print() 打乱交互式 CLI 提示符。
        self.quiet: bool = False

        # --- 监听器相关初始化 ---
        self._stop_event = threading.Event()
        self._listener_thread: Optional[threading.Thread] = None

        # 初始化完成后，自动开启监听线程
        self.start_listening()

    def _emit(self, message: str) -> None:
        if getattr(self, "quiet", False):
            return
        print(message)

    def start_listening(self):
        """启动后台监听线程，订阅 host_id 话题"""
        if self._listener_thread and self._listener_thread.is_alive():
            return

        self._emit(f"[{self.hostname}] Starting listener on topic: '{self.host_id}'...")
        self._stop_event.clear()
        self._listener_thread = threading.Thread(target=self._msg_listener_loop, daemon=True)
        self._listener_thread.start()

    def stop_listening(self):
        """停止监听线程"""
        if self._listener_thread:
            self._stop_event.set()
            self._listener_thread.join(timeout=2)
            self._emit(f"[{self.hostname}] Listener stopped.")

    def _msg_listener_loop(self):
        """
        后台循环：模拟/实现消息订阅逻辑
        实际项目中，这里应该替换为subscribe 阻塞循环
        """

        # 下面是一个模拟循环，用于演示框架逻辑，实际使用请替换
        while not self._stop_event.is_set():
            try:
                # 模拟：此处应该是一个阻塞的 recv() 或者 subscribe 回调
                # 为了演示，我们假设这里有一个 polling 机制 (实际中建议用 Callback)
                # message = broker.wait_for_message(topic=self.host_id, timeout=1)

                time.sleep(1) # 避免死循环占用CPU

            except Exception as e:
                self._emit(f"[{self.hostname}] Listener Error: {e}")
                time.sleep(5)

    def _on_message_callback(self, payload: str):
        """
        内部回调：当监听到消息时被调用

        假设 payload 是 JSON 格式的镜像信息
        """
        try:
            data = json.loads(payload)
            # 验证数据是否包含镜像关键信息
            if 'image_id' in data and 'version' in data:
                self.register_image(data)
            else:
                self._emit(f"[{self.hostname}] Warning: Received invalid image format on topic {self.host_id}")
        except json.JSONDecodeError:
            self._emit(f"[{self.hostname}] Error: Failed to decode JSON payload.")

    def register_image(self, image_info: dict):
        """
        核心功能：注册/更新镜像信息
        """
        self._emit(f"[{self.hostname}] ⚡ Signal Received on '{self.host_id}' -> Registering Image...")
        image = RobotAppImage(
            image_id=str(image_info.get('image_id')),
            name=str(image_info.get('name', f"RobotApp_{image_info.get('image_id')}")),
            version=str(image_info.get('version', '0.0.0')),
            source_vm_id=int(image_info.get('source_vm_id', 0)),
        )
        self.images[image.image_id] = image
        self._emit(f"[{self.hostname}] ✅ Image Updated: {image.name} (v{image.version})")

    @abstractmethod
    def check_health(self) -> bool:
        """获取主机本身的运行状况"""
        pass

    @abstractmethod
    def get_internal_app_status(self) -> Dict[str, Any]:
        """获取内部运行应用状态"""
        pass

    def __repr__(self):
        img_str = f" [Img: {self.image_info.get('name')}]" if self.image_info else ""
        return f"[{self.__class__.__name__}] {self.hostname} ({self.ip}){img_str}"


class PveCapableHost(BaseHost):
    """具备 Proxmox VE `qm` 操作能力的中间主机类。

    供 ServerHost 和 PveHost 继承，BareMetalHost 不继承本类。

    要求子类提供：
    - execute_ssh(cmd) -> tuple[str, str]
    - execute_ssh_with_status(cmd) -> tuple[str, str, int]（可选，有默认实现）
    - hostname: str
    - image_info: dict | None（可选，用于 USB whitelist）
    """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_line(self, message: str) -> None:
        emit = getattr(self, "_emit", None)
        if callable(emit):
            try:
                emit(message)
                return
            except Exception:
                pass
        print(message)

    def _execute_ssh_with_status(self, cmd: str) -> tuple[str, str, int]:
        exec_with_status = getattr(self, "execute_ssh_with_status", None)
        if callable(exec_with_status):
            return exec_with_status(cmd)
        out, err = self.execute_ssh(cmd)
        rc = 0 if not str(err or "").strip() else 1
        return out, err, rc

    # ------------------------------------------------------------------
    # VM config queries
    # ------------------------------------------------------------------

    def get_vm_config_text(self, image_id: int) -> str:
        out, err, rc = self._execute_ssh_with_status(f"qm config {int(image_id)}")
        text = (out or "") + (err or "")
        if rc != 0:
            raise RuntimeError(text.strip() or f"读取 VM {image_id} 配置失败")
        return text

    def get_vm_runtime_status(self, image_id: int) -> str:
        out, err, rc = self._execute_ssh_with_status(f"qm status {int(image_id)}")
        text = (out or "") + (err or "")
        if rc != 0:
            raise RuntimeError(text.strip() or f"读取 VM {image_id} 状态失败")
        match = re.search(r"status:\s*(\S+)", text)
        if match:
            return str(match.group(1)).strip().lower()
        return ""

    def is_template_vm(self, image_id: int) -> bool:
        config_text = self.get_vm_config_text(image_id)
        return bool(re.search(r"^template:\s*1\s*$", config_text, flags=re.MULTILINE))

    def stop_vm_for_offline_migration(
        self,
        image_id: int,
        *,
        timeout_s: float = 120.0,
        force_stop: bool = False,
    ) -> bool:
        """Stop a running VM before an offline migration.

        Returns True when the VM was running and should be restarted after migration.
        """
        image_id = int(image_id)
        status = self.get_vm_runtime_status(image_id)
        if status != "running":
            self._emit_line(f"VM {image_id} status is {status or 'unknown'}; offline migration can proceed.")
            return False

        timeout_i = max(1, int(float(timeout_s or 120.0)))
        self._emit_line(f"Shutting down VM {image_id} before offline migration (timeout={timeout_i}s)...")
        out, err, rc = self._execute_ssh_with_status(f"qm shutdown {image_id}")
        if (out or err) and rc != 0:
            self._emit_line(f"[WARN] qm shutdown returned exit={rc}: {(out or '') + (err or '')}")

        deadline = time.time() + timeout_i
        while time.time() < deadline:
            try:
                status = self.get_vm_runtime_status(image_id)
                if status != "running":
                    self._emit_line(f"VM {image_id} stopped for offline migration (status={status or 'unknown'}).")
                    return True
            except Exception:
                pass
            time.sleep(3.0)

        if force_stop:
            self._emit_line(f"[WARN] VM {image_id} did not shut down cleanly; forcing stop.")
            out, err, rc = self._execute_ssh_with_status(f"qm stop {image_id}")
            if rc != 0:
                text = ((out or "") + (err or "")).strip()
                raise RuntimeError(text or f"qm stop {image_id} failed with exit code {rc}")
            for _ in range(20):
                status = self.get_vm_runtime_status(image_id)
                if status != "running":
                    self._emit_line(f"VM {image_id} stopped for offline migration (status={status or 'unknown'}).")
                    return True
                time.sleep(3.0)

        raise RuntimeError(
            f"VM {image_id} is still running after {timeout_i}s; offline migration requires a stopped VM"
        )

    def start_vm_after_migration(self, image_id: int, *, timeout_s: float = 120.0) -> tuple[str, str]:
        image_id = int(image_id)
        status = self.get_vm_runtime_status(image_id)
        if status == "running":
            self._emit_line(f"VM {image_id} is already running.")
            return "", ""

        self._emit_line(f"Starting VM {image_id} after offline migration...")
        out, err, rc = self._execute_ssh_with_status(f"qm start {image_id}")
        text = (out or "") + (err or "")
        if rc != 0:
            raise RuntimeError(text.strip() or f"qm start {image_id} failed with exit code {rc}")

        deadline = time.time() + max(1, float(timeout_s or 120.0))
        while time.time() < deadline:
            try:
                if self.get_vm_runtime_status(image_id) == "running":
                    self._emit_line(f"VM {image_id} started.")
                    return out, err
            except Exception:
                pass
            time.sleep(3.0)

        raise RuntimeError(f"VM {image_id} did not reach running state after start")

    # ------------------------------------------------------------------
    # Template operations
    # ------------------------------------------------------------------

    def template_vm(self, image_id: int) -> tuple[str, str]:
        image_id = int(image_id)
        status = self.get_vm_runtime_status(image_id)
        if status == "running":
            raise RuntimeError(f"VM {image_id} 当前处于运行状态，请先关机后再模板化")
        if self.is_template_vm(image_id):
            return "", ""
        out, err, rc = self._execute_ssh_with_status(f"qm template {image_id}")
        text = (out or "") + (err or "")
        if rc != 0:
            raise RuntimeError(text.strip() or f"模板化 VM {image_id} 失败")
        return out, err

    def clone_template_vm(
        self,
        template_vmid: int,
        new_vmid: int,
        *,
        target_node: str,
        name: str | None = None,
        full: bool = True,
    ) -> tuple[str, str]:
        template_vmid = int(template_vmid)
        new_vmid = int(new_vmid)
        if not self.is_template_vm(template_vmid):
            raise RuntimeError(f"VM {template_vmid} 不是模板，无法执行模板克隆")
        args: list[str] = ["qm", "clone", str(template_vmid), str(new_vmid)]
        if full:
            args.extend(["--full", "1"])
        if target_node:
            args.extend(["--target", shlex.quote(str(target_node))])
        if name:
            args.extend(["--name", shlex.quote(str(name))])
        out, err, rc = self._execute_ssh_with_status(" ".join(args))
        text = (out or "") + (err or "")
        if rc != 0:
            raise RuntimeError(text.strip() or f"从模板 {template_vmid} 克隆 VM {new_vmid} 失败")
        return out, err

    # ------------------------------------------------------------------
    # Migration prepare / finalize
    # ------------------------------------------------------------------

    def configure_image_before_migration(self, image_id: int):
        """迁移前准备：USB 归一为 spice；local-lvm -> VMs。"""
        self._emit_line(f"Configuring Image {image_id} for migration...")
        import os as _os
        from configs.config import GlobalConfig as _GlobalConfig

        # 1) Normalize migration-blocking VM config.
        try:
            conf_out, conf_err = self.execute_ssh(f"qm config {image_id}")
            conf_text = (conf_out or "") + (conf_err or "")
            delete_keys = sorted(
                set(re.findall(r"^(?:usb\d+|clipboard)(?=:)", conf_text, flags=re.MULTILINE)),
                key=lambda key: (key != "clipboard", key),
            )
            for k in delete_keys:
                out_del, err_del, rc_del = self._execute_ssh_with_status(f"qm set {image_id} --delete {k}")
                if rc_del != 0:
                    raise RuntimeError((out_del or "") + (err_del or "") or f"failed to delete {k}")
            self.execute_ssh(f"qm set {image_id} --usb0 spice")
        except Exception as e:
            self._emit_line(f"[{getattr(self, 'hostname', '')}] Migration config normalization error: {e}")

        cfg = getattr(self, "config", None) or _GlobalConfig
        system_cfg = getattr(cfg, "system", None)
        if bool(getattr(system_cfg, "demo_mode", False)):
            self._emit_line("[DEMO] Skipping disk preparation...")
            return

        # 2) local-lvm -> VMs
        try:
            online_opt = ""
            try:
                st_out, st_err = self.execute_ssh(f"qm status {image_id}")
                st_text = (st_out or "") + (st_err or "")
                if "status: running" in st_text or "running" in st_text:
                    online_opt = "--online 1"
            except Exception:
                online_opt = ""

            conf_out, conf_err = self.execute_ssh(f"qm config {image_id}")
            conf_text = (conf_out or "") + (conf_err or "")
            disk_keys: list[str] = []
            for line in conf_text.splitlines():
                if "local-lvm" not in line:
                    continue
                if ":" not in line:
                    continue
                key = line.split(":", 1)[0].strip()
                if not key.startswith(("scsi", "sata", "ide", "virtio")):
                    continue
                disk_keys.append(key)

            if disk_keys:
                self._emit_line(
                    f"[{getattr(self, 'hostname', '')}] Preparing disks local-lvm -> VMs: {', '.join(disk_keys)}"
                )
                for key in disk_keys:
                    move_cmd = f"qm move-disk {image_id} {key} VMs --delete 1 {online_opt}".strip()
                    out_mv, err_mv = self.execute_ssh(move_cmd)
                    if (err_mv or "").strip():
                        self._emit_line(
                            f"[{getattr(self, 'hostname', '')}] qm move-disk stderr ({key}): {(err_mv or '').strip()}"
                        )
                        if "snapshots" in (err_mv or "").lower() and "delete" in (err_mv or "").lower():
                            retry_cmd = f"qm move-disk {image_id} {key} VMs --delete 0 {online_opt}".strip()
                            self.execute_ssh(retry_cmd)
        except Exception as e:
            self._emit_line(f"[{getattr(self, 'hostname', '')}] Move local-lvm -> VMs disk error: {e}")

    def configure_image_after_migration(self, image_id: int):
        """迁移后收尾：磁盘落盘 + 设置启动盘 + USB 白名单直通。"""
        import os as _os
        from configs.config import GlobalConfig as _GlobalConfig

        self._emit_line(f"Configuring Image {image_id} after migration...")

        cfg = getattr(self, "config", None) or _GlobalConfig
        system_cfg = getattr(cfg, "system", None)
        demo_mode = bool(getattr(system_cfg, "demo_mode", False))

        move_disks_to_local = getattr(self, "disk_localization_enabled", None)
        if move_disks_to_local is None:
            move_disks_to_local = True
        if demo_mode:
            self._emit_line("[DEMO] Skipping disk localization...")
            move_disks_to_local = False

        hostname = getattr(self, "hostname", "")

        if move_disks_to_local:
            try:
                online_opt = ""
                try:
                    st_out, st_err = self.execute_ssh(f"qm status {image_id}")
                    st_text = (st_out or "") + (st_err or "")
                    if "status: running" in st_text or "running" in st_text:
                        online_opt = "--online 1"
                except Exception:
                    online_opt = ""

                conf_out, conf_err = self.execute_ssh(f"qm config {image_id}")
                conf_text = (conf_out or "") + (conf_err or "")

                boot_disk: str | None = None
                m = re.search(r"^bootdisk:\s*(\S+)", conf_text, flags=re.MULTILINE)
                if m:
                    boot_disk = m.group(1).strip()

                disk_keys: list[str] = []
                snapshot_delete_blocked: list[str] = []
                for line in conf_text.splitlines():
                    if ":" not in line:
                        continue
                    key = line.split(":", 1)[0].strip()
                    if not key.startswith(("scsi", "sata", "ide", "virtio")):
                        continue
                    if "VMs" not in line:
                        continue
                    disk_keys.append(key)

                if disk_keys:
                    self._emit_line(f"[{hostname}] Localizing disks VMs -> local-lvm: {', '.join(disk_keys)}")
                    for key in disk_keys:
                        move_cmd = f"qm move-disk {image_id} {key} local-lvm --delete 1 {online_opt}".strip()
                        out_mv, err_mv = self.execute_ssh(move_cmd)
                        if (err_mv or "").strip() and "snapshots" in (err_mv or "").lower() and "delete" in (err_mv or "").lower():
                            snapshot_delete_blocked.append(key)
                            self.execute_ssh(f"qm move-disk {image_id} {key} local-lvm --delete 0 {online_opt}".strip())
                        if boot_disk is None:
                            boot_disk = key

                # Re-read config, prefer local-lvm disks as boot
                try:
                    conf2_out, conf2_err = self.execute_ssh(f"qm config {image_id}")
                    conf2_text = (conf2_out or "") + (conf2_err or "")
                    local_candidates: list[str] = []
                    for line in conf2_text.splitlines():
                        if ":" not in line:
                            continue
                        key = line.split(":", 1)[0].strip()
                        if not key.startswith(("scsi", "sata", "ide", "virtio")):
                            continue
                        if "local-lvm" not in line:
                            continue
                        if "media=cdrom" in line or "cloudinit" in line:
                            continue
                        local_candidates.append(key)
                    prefer = ["scsi0", "virtio0", "sata0", "ide0", "scsi1", "virtio1", "sata1", "ide1"]
                    for k in prefer:
                        if k in local_candidates:
                            boot_disk = k
                            break
                    else:
                        if local_candidates:
                            boot_disk = local_candidates[0]
                except Exception:
                    pass

                if boot_disk:
                    self.execute_ssh(f"qm set {image_id} --bootdisk {boot_disk}")
                    self.execute_ssh(f"qm set {image_id} --boot order={boot_disk}")
            except Exception as e:
                self._emit_line(f"[{hostname}] Disk move/set boot error: {e}")
        else:
            self._emit_line(f"[{hostname}] Skip disk localization (keep disks on VMs).")

        # 2) USB passthrough
        try:
            conf_out, conf_err = self.execute_ssh(f"qm config {image_id}")
            conf_text = (conf_out or "") + (conf_err or "")
            usb_keys = sorted(set(re.findall(r"^usb\d+(?=:)", conf_text, flags=re.MULTILINE)))
            for k in usb_keys:
                self.execute_ssh(f"qm set {image_id} --delete {k}")

            wl = None
            image_info = getattr(self, "image_info", None)
            if isinstance(image_info, dict):
                wl = image_info.get("usb_whitelist")
            if wl is None:
                wl = _os.getenv("PVE_USB_WHITELIST")

            if isinstance(wl, str):
                whitelist = [x.strip() for x in wl.split(",") if x.strip()]
            elif isinstance(wl, (list, tuple)):
                whitelist = [str(x).strip() for x in wl if str(x).strip()]
            else:
                whitelist = []

            if not whitelist and _os.getenv("PVE_USB_PASSTHROUGH_ALL", "").strip().lower() in ("1", "true", "yes"):
                out, err = self.execute_ssh("lsusb")
                whitelist = re.findall(r"\bID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\b", (out or "") + (err or ""))

            if whitelist:
                for idx, usb_id in enumerate(whitelist[:10]):
                    self.execute_ssh(f"qm set {image_id} --usb{idx} host={usb_id}")
        except Exception as e:
            self._emit_line(f"[{hostname}] USB passthrough error: {e}")

    def migrate_image(
        self,
        image_id: int,
        target_host: Any,
        *,
        online: bool = False,
        with_local_disks: bool = False,
        target_storage: str | None = None,
        offline_fallback: bool = False,
    ):
        """将虚拟机从当前宿主节点迁移到 target_host。"""
        self._emit_line(
            f"[{getattr(self, 'hostname', '')}] Migrating VM {image_id} "
            f"-> {getattr(target_host, 'pve_node_name', '?')}..."
        )
        try:
            ok = bool(target_host.check_health())
            if not ok:
                self._emit_line(
                    f"[WARN] 目标节点 {getattr(target_host, 'hostname', '')} 本机不可达，"
                    "但仍尝试迁移（集群内可能可达）。"
                )
        except Exception:
            pass

        if online:
            self._emit_line("[INFO] Online migration is disabled; using offline migration without --online.")

        args: list[str] = ["qm", "migrate", str(image_id), str(getattr(target_host, "pve_node_name", ""))]
        if with_local_disks:
            args.extend(["--with-local-disks", "1"])
        if target_storage:
            args.extend(["--targetstorage", str(target_storage)])

        migrate_cmd = " ".join(args)
        out, err, rc = self._execute_ssh_with_status(migrate_cmd)
        self._emit_line(f"迁移命令输出: {out}{err}")
        if rc != 0:
            text = ((out or "") + (err or "")).strip()
            retryable = False
            if offline_fallback and retryable:
                offline_args = [arg for arg in args if arg != "--online"]
                offline_cmd = " ".join(offline_args)
                self._emit_line(
                    "[WARN] Online migration is not supported for this VM config; "
                    "retrying offline migration. VM downtime is expected."
                )
                out2, err2, rc2 = self._execute_ssh_with_status(offline_cmd)
                self._emit_line(f"离线迁移命令输出: {out2}{err2}")
                if rc2 == 0:
                    return out2, err2
                text2 = ((out2 or "") + (err2 or "")).strip()
                raise RuntimeError(
                    "Online migration failed, and offline fallback also failed.\n"
                    f"online: {text}\n"
                    f"offline: {text2 or f'exit code {rc2}'}"
                )
            raise RuntimeError(text or f"Migration command failed with exit code {rc}")
        return out, err

