from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from infrastructure.base_host import BaseHost, PveCapableHost
from infrastructure.bare_metal_host import BareMetalHost
from infrastructure.pve_host import PveHost
from infrastructure.server_host import ServerHost
from configs.config import GlobalConfig


logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    def __init__(self, step: str, message: str, *, cause: Exception | None = None):
        self.detail = message
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.cause = cause


@dataclass(frozen=True)
class VmLocation:
    vmid: int
    node: str


class DeploymentPipeline:
    """迁移部署核心流水线（用于 scheduler/CLI 调用）。

    入口：execute_pipeline(vmid, target_host_id)
    """

    def __init__(
        self,
        master_server: ServerHost,
        *,
        hosts: dict | None = None,
        logger: logging.Logger | None = None,
        task_id: str | None = None,
        config: Any | None = None,
    ):
        self.master_server = master_server
        self.hosts = hosts or {}
        self.logger = logger or logging.getLogger(__name__)
        self.config = config or GlobalConfig

        # 用于把“同一任务”的日志串起来；后台 dispatcher 会注入 task_id。
        # 手工/交互式调用时保持为 "-"。
        self.current_task_id: str = str(task_id or "-")

        # 默认超时/策略：与 DataCenter.deploy_robot_system 默认值保持一致
        timeouts = getattr(self.config, "timeouts", None)
        self.ssh_reboot_timeout_s: float = float(getattr(timeouts, "ssh_reboot", 600.0) or 600.0)
        self.ssh_reconnect_timeout_s: float = float(getattr(timeouts, "ssh_reconnect", 300.0) or 300.0)
        self.migrate_timeout_s: float = float(getattr(timeouts, "migration", 1800.0) or 1800.0)
        self.pve_node_ready_timeout_s: float = float(
            getattr(timeouts, "pve_node_ready", 180.0) or 180.0
        )
        self.reboot_on_finalize: bool = True
        self.try_recover_network_on_failure: bool = True
        migration_cfg = getattr(self.config, "migration", None)
        self.migration_mode: str = str(getattr(migration_cfg, "mode", "offline") or "offline").lower()
        self.offline_shutdown_timeout_s: float = float(
            getattr(migration_cfg, "offline_shutdown_timeout", 120.0) or 120.0
        )
        self.offline_force_stop: bool = bool(getattr(migration_cfg, "offline_force_stop", False))
        self.restart_after_offline_migration: bool = bool(
            getattr(migration_cfg, "restart_after_offline_migration", True)
        )
        system_cfg = getattr(self.config, "system", None)
        self.require_cable_confirmation: bool = bool(
            getattr(system_cfg, "require_cable_confirmation", True)
        )

        # 可选：迁移 target storage（传给 qm migrate --targetstorage）
        self.migrate_target_storage: str | None = None

        # USB 白名单：None 表示使用内置默认匹配策略
        self.usb_whitelist: list[str] | None = None

    def _log(
        self,
        level: str,
        step: str,
        msg: str,
        *,
        exc_info: BaseException | None = None,
    ) -> None:
        """结构化上下文日志。

        统一格式："[{self.current_task_id}] [{step}] {msg}"。
        """

        prefix = f"[{self.current_task_id}] [{step}] {msg}"
        lvl = str(level or "info").lower().strip()
        if lvl == "error":
            self.logger.error(prefix, exc_info=exc_info)
        elif lvl == "warning":
            self.logger.warning(prefix, exc_info=exc_info)
        elif lvl == "debug":
            self.logger.debug(prefix, exc_info=exc_info)
        elif lvl == "exception":
            self.logger.exception(prefix, exc_info=exc_info)
        else:
            self.logger.info(prefix, exc_info=exc_info)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_pipeline(self, vmid: str, target_host_id: str) -> bool:
        """执行迁移部署流水线。

        成功返回 True；失败抛出 PipelineError。
        """

        step = "Init"
        try:
            vmid_int = int(str(vmid).strip())
        except Exception as e:
            raise PipelineError(step, f"vmid 必须是整数：{vmid!r}", cause=e)

        target_host = self._find_host(target_host_id)
        if target_host is None:
            raise PipelineError(step, f"未找到目标主机/节点：{target_host_id}")

        # execute_pipeline 入口不接收 network_config/source_network_config：
        # - 目标端若是 PveHost，要求 target_host.network_config 已就绪（interactive_runner 会自动填充）
        # - 源端若是 PveHost，优先使用 src_runner.network_config，否则尝试 detect_active_network_config()
        network_config = getattr(target_host, "network_config", None)
        return self.execute_deploy_robot_system(
            target_host=target_host,
            image_id=vmid_int,
            network_config=network_config if isinstance(network_config, dict) else None,
        )

    def execute_deploy_robot_system(
        self,
        *,
        target_host: BaseHost,
        image_id: int,
        network_config: dict[str, Any] | None,
        source_host: ServerHost | None = None,
        source_network_config: dict[str, Any] | None = None,
    ) -> bool:
        """与 DataCenter.deploy_robot_system 对齐的流水线实现。

        该方法用于 DataCenter 薄封装调用；execute_pipeline() 是调度器简入口。
        """

        if isinstance(target_host, BareMetalHost):
            return self._deploy_to_bare_metal(int(image_id), target_host)

        api_host = self.master_server
        if api_host is None:
            raise PipelineError("Init", "master_server 未设置，无法使用 PVE API 轮询/发现")

        # --------------------------------------------------------------
        # Source discovery（优先 API，失败 fallback pvesh）
        # --------------------------------------------------------------
        step = "SourceDiscovery"
        try:
            current_node = self._find_vm_current_node(api_host, int(image_id))
            if not current_node:
                loc = self._find_vm_location_via_pvesh(int(image_id))
                current_node = loc.node
            if not current_node:
                raise PipelineError(step, f"无法获取 VM {image_id} 当前所在节点")

            if source_host is not None:
                src_runner: PveCapableHost = source_host  # type: ignore[assignment]
            else:
                src_runner = self._find_host(current_node)  # type: ignore[assignment]
                if src_runner is None:
                    if getattr(api_host, "pve_node_name", None) == current_node:
                        src_runner = api_host
                    else:
                        raise PipelineError(
                            step,
                            f"VM {image_id} 当前在节点 {current_node}，但 hosts 中未注册该节点主机；"
                            "请注册该节点或显式传入 source_host（需能 SSH 执行 qm）",
                        )

            if not isinstance(src_runner, PveCapableHost):
                raise PipelineError(step, f"source_host 不是 PVE 主机类型，无法执行 qm 操作：{src_runner!r}")

            self._log(
                "info",
                step,
                f"vmid={image_id} node={current_node} src={getattr(src_runner, 'hostname', '')}",
            )
        except PipelineError:
            raise
        except Exception as e:
            raise PipelineError(step, f"failed: {e}", cause=e)

        # --------------------------------------------------------------
        # 1) Network Prep (Topology-aware, Serial Reboot)
        # --------------------------------------------------------------
        step = "NetworkPrep"
        try:
            if isinstance(src_runner, PveHost):
                src_cfg: dict[str, Any] = {}
                if source_network_config:
                    src_cfg = dict(source_network_config)
                else:
                    existing = getattr(src_runner, "network_config", None)
                    if isinstance(existing, dict) and existing:
                        src_cfg = dict(existing)
                    else:
                        try:
                            src_cfg = dict(src_runner.detect_active_network_config() or {})
                        except Exception:
                            src_cfg = {}

                if not src_cfg:
                    raise PipelineError(
                        step,
                        "源端为 PveHost，但未提供 source_network_config 且无法从源端读取 active 网络配置；已熔断终止。",
                    )

                src_runner.network_config = src_cfg
                src_runner.upload_network_profiles()

                src_wired_ip = self._ip_only(str(src_cfg.get("wired_ip", "")))
                self._log("info", step, "Switching source network -> wired")
                ok = src_runner.configure_network("wired")
                if ok is False:
                    raise PipelineError(step, "源端切换 wired 失败")
                if self.require_cable_confirmation:
                    self._blocking_cable_confirm("请插入网线并按回车键确认（Source）...")
                self._reboot_and_wait_ssh(
                    src_runner,
                    timeout_s=float(self.ssh_reconnect_timeout_s),
                    grace_s=10.0,
                    label=f"{step}/Source",
                )
                if src_wired_ip:
                    src_runner.ip = src_wired_ip

            if isinstance(target_host, PveHost):
                tgt_cfg = dict(network_config or {})
                if not tgt_cfg:
                    # 这里与 DataCenter 标准保持一致：目标 PVE 端必须提供 network_config
                    raise PipelineError(step, "目标端为 PveHost，但 network_config 为空，无法生成网络 profiles")

                target_host.network_config = tgt_cfg
                target_host.upload_network_profiles()

                tgt_wired_ip = self._ip_only(str(tgt_cfg.get("wired_ip", "")))
                self._log("info", step, "Switching target network -> wired")
                ok = target_host.configure_network("wired")
                if ok is False:
                    raise PipelineError(step, "目标端切换 wired 失败")
                if self.require_cable_confirmation:
                    self._blocking_cable_confirm("请插入网线并按回车键确认（Target）...")
                self._reboot_and_wait_ssh(
                    target_host,
                    timeout_s=float(self.ssh_reconnect_timeout_s),
                    grace_s=10.0,
                    label=f"{step}/Target",
                )
                if tgt_wired_ip:
                    target_host.ip = tgt_wired_ip
            else:
                self._log("info", step, "Target is ServerHost; skip target network switching")
        except PipelineError:
            raise
        except Exception as e:
            raise PipelineError(step, f"failed: {e}", cause=e)

        # --------------------------------------------------------------
        # 2) Pre-Migration Data (source)
        # --------------------------------------------------------------
        step = "PreMigration"
        try:
            self._log(
                "info",
                step,
                f"Preparing VM {image_id} on source {getattr(src_runner, 'hostname', 'source')}",
            )
            vm_was_running_before_migration = src_runner.stop_vm_for_offline_migration(
                int(image_id),
                timeout_s=float(self.offline_shutdown_timeout_s),
                force_stop=bool(self.offline_force_stop),
            )
            src_runner.configure_image_before_migration(int(image_id))  # type: ignore[attr-defined]
            self._remove_hostpci_passthrough(src_runner, int(image_id))
            self._ensure_usb_migratable(src_runner, int(image_id))
        except PipelineError:
            raise
        except Exception as e:
            raise PipelineError(step, f"failed: {e}", cause=e)

        # --------------------------------------------------------------
        # 3) Migration
        # --------------------------------------------------------------
        step = "Migration"
        try:
            self._wait_for_pve_node_ready(
                api_host,
                src_runner,
                node_name=str(getattr(src_runner, "pve_node_name", "")),
                timeout_s=float(self.pve_node_ready_timeout_s),
            )
            self._check_cluster_quorum(api_host)
            self._log(
                "info",
                step,
                f"Migrating vmid={image_id} {getattr(src_runner, 'pve_node_name', '?')} -> {getattr(target_host, 'pve_node_name', '?')}",
            )
            out, err = src_runner.migrate_image(
                int(image_id),
                target_host,
                online=False,
                with_local_disks=True,
                target_storage=self.migrate_target_storage,
            )
            if out or err:
                self._log("info", step, f"qm migrate output:\n{(out or '') + (err or '')}")

            deadline = time.time() + float(self.migrate_timeout_s)
            while time.time() < deadline:
                try:
                    info = None
                    for r in (api_host.pve_api.cluster.resources.get(type="vm") or []):  # type: ignore[union-attr]
                        if isinstance(r, dict) and r.get("type") == "qemu" and int(r.get("vmid", 0)) == int(image_id):
                            info = r
                            break
                    if info and str(info.get("node")) == str(getattr(target_host, "pve_node_name", "")):
                        self._log(
                            "info",
                            step,
                            f"Migration confirmed: node={info.get('node')} status={info.get('status')}",
                        )
                        break
                except Exception:
                    pass
                time.sleep(3.0)
            else:
                raise PipelineError(step, "迁移校验超时：未确认 VM 已落在目标节点")
        except PipelineError:
            raise
        except Exception as e:
            raise PipelineError(step, f"failed: {e}", cause=e)

        # --------------------------------------------------------------
        # 4/5) Post-Migration Localization + Hardware Adaptation
        # --------------------------------------------------------------
        step = "PostMigration"
        try:
            matched = self._select_usb_whitelist(target_host, self.usb_whitelist)
            target_host.image_info = dict(getattr(target_host, "image_info", None) or {})
            target_host.image_info["usb_whitelist"] = matched
            self._log("info", step, f"USB whitelist matched on target: {matched}")

            target_host.configure_image_after_migration(int(image_id))  # type: ignore[attr-defined]
        except PipelineError:
            raise
        except Exception as e:
            raise PipelineError(step, f"failed: {e}", cause=e)

        # --------------------------------------------------------------
        # 6) Finalization
        # --------------------------------------------------------------
        step = "Finalization"
        try:
            if isinstance(target_host, PveHost):
                self._log("info", step, "Switching target network -> wireless")
                self._finalize_pve_wireless(
                    target_host,
                    label=step,
                    ssh_reconnect_timeout_s=float(self.ssh_reconnect_timeout_s),
                    reboot_on_finalize=self.reboot_on_finalize,
                )
            else:
                self._log("info", step, "Target is ServerHost; skip target wireless finalize")

            if isinstance(src_runner, PveHost):
                self._log("info", step, "Switching source network -> wireless")
                self._finalize_pve_wireless(
                    src_runner,
                    label=f"{step}/Source",
                    ssh_reconnect_timeout_s=float(self.ssh_reconnect_timeout_s),
                    reboot_on_finalize=self.reboot_on_finalize,
                )

            if vm_was_running_before_migration and self.restart_after_offline_migration:
                self._wait_for_pve_node_ready(
                    api_host,
                    target_host,
                    node_name=str(getattr(target_host, "pve_node_name", "")),
                    timeout_s=float(self.pve_node_ready_timeout_s),
                )
                self._log("info", step, f"Restarting VM after offline migration: vmid={image_id}")
                target_host.start_vm_after_migration(  # type: ignore[attr-defined]
                    int(image_id),
                    timeout_s=float(self.offline_shutdown_timeout_s),
                )

            self._log("info", step, f"部署完成：vmid={image_id} -> {getattr(target_host, 'hostname', '')}")
            return True
        except PipelineError:
            raise
        except Exception as e:
            if self.try_recover_network_on_failure:
                try:
                    if isinstance(target_host, PveHost):
                        target_host.configure_network("wireless")
                        if self.reboot_on_finalize:
                            target_host.reboot_system()
                    if isinstance(src_runner, PveHost):
                        src_runner.configure_network("wireless")
                        if self.reboot_on_finalize:
                            src_runner.reboot_system()
                except Exception:
                    pass
            raise PipelineError(step, f"failed: {e}", cause=e)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_host(self, host_id_or_node: str):
        key = str(host_id_or_node or "").strip()
        if not key:
            return None
        if key in self.hosts:
            return self.hosts[key]
        for h in self.hosts.values():
            if str(getattr(h, "pve_node_name", "")) == key:
                return h
        return None

    def _find_vm_current_node(self, api_host: ServerHost, vmid: int) -> str | None:
        return self.master_server.find_vm_current_node(vmid)

    def _find_vm_location_via_pvesh(self, vmid: int) -> VmLocation:
        step = "Discovery/pvesh"
        cmd = "pvesh get /cluster/resources --type vm --output-format json"
        try:
            out, err = self.master_server.execute_ssh(cmd)
        except Exception as e:
            raise PipelineError(step, f"执行 pvesh 失败：{e}", cause=e)

        if err and err.strip():
            self._log("debug", step, f"stderr: {err.strip()}")

        try:
            data = json.loads(out or "[]")
        except Exception as e:
            raise PipelineError(step, f"pvesh 输出非 JSON：{out[:200]!r}", cause=e)

        if not isinstance(data, list):
            raise PipelineError(step, f"pvesh 输出结构异常：{type(data)!r}")

        for r in data:
            if not isinstance(r, dict):
                continue
            if r.get("type") != "qemu":
                continue
            try:
                if int(r.get("vmid", 0)) != int(vmid):
                    continue
            except Exception:
                continue
            node = r.get("node")
            if node:
                return VmLocation(vmid=int(vmid), node=str(node))

        raise PipelineError(step, f"未在集群资源中找到 vmid={vmid}")

    @staticmethod
    def _ip_only(value: str | None) -> str:
        v = (value or "").strip()
        if not v:
            return ""
        if "/" in v:
            return str(ipaddress.IPv4Interface(v).ip)
        return v

    @staticmethod
    def _is_vidpid(value: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", value.strip()))

    @staticmethod
    def _is_vid_prefix(value: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-fA-F]{4}:", value.strip()))

    @classmethod
    def _match_vidpid(cls, vidpid: str, pattern: str) -> bool:
        vp = vidpid.strip().lower()
        p = pattern.strip().lower()
        if cls._is_vidpid(p):
            return vp == p
        if cls._is_vid_prefix(p):
            return vp.startswith(p)
        return False

    def _blocking_cable_confirm(self, prompt: str) -> None:
        if not sys.stdin.isatty():
            raise PipelineError("Interaction", "当前为非交互环境，无法执行网线插入确认。")
        input(prompt)

    def _wait_for_ssh(
        self,
        host: BaseHost,
        *,
        timeout_s: float,
        interval_s: float = 3.0,
        label: str = "SSH",
    ) -> bool:
        deadline = time.time() + float(timeout_s)
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                out, _ = host.execute_ssh("echo OK")  # type: ignore[attr-defined]
                if "OK" in (out or ""):
                    self._log("info", label, f"{getattr(host, 'hostname', '')} SSH ready")
                    return True
            except Exception as e:
                msg = str(e)
                if "Broken pipe" in msg or "broken pipe" in msg:
                    self._log("info", label, f"{getattr(host, 'hostname', '')} SSH broken pipe, retrying...")
                else:
                    self._log("info", label, f"{getattr(host, 'hostname', '')} waiting... (attempt={attempt})")
            time.sleep(float(interval_s))
        return False

    def _ensure_ssh_reachable(
        self,
        host: BaseHost,
        *,
        candidates: Iterable[str],
        timeout_s: float,
        label: str,
    ) -> str | None:
        original_ip = getattr(host, "ip", "")
        tried: list[str] = []
        for ip in candidates:
            ip_s = str(ip or "").strip()
            if not ip_s or ip_s in tried:
                continue
            tried.append(ip_s)
            try:
                host.ip = ip_s  # type: ignore[attr-defined]
            except Exception:
                pass
            if self._wait_for_ssh(host, timeout_s=timeout_s, label=label):
                return ip_s
        try:
            host.ip = original_ip  # type: ignore[attr-defined]
        except Exception:
            pass
        return None

    def _reboot_and_wait_ssh(
        self,
        host: BaseHost,
        *,
        timeout_s: float,
        grace_s: float = 10.0,
        label: str,
    ) -> None:
        def get_boot_id() -> str | None:
            try:
                out, _ = host.execute_ssh("cat /proc/sys/kernel/random/boot_id || true")  # type: ignore[attr-defined]
                v = (out or "").strip().splitlines()
                bid = v[-1].strip() if v else ""
                return bid or None
            except Exception:
                return None

        before_boot_id = get_boot_id()

        host.reboot_system()  # type: ignore[attr-defined]
        time.sleep(float(grace_s))

        deadline = time.time() + float(timeout_s)
        saw_down = False
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                out, _ = host.execute_ssh("echo OK")  # type: ignore[attr-defined]
                if "OK" not in (out or ""):
                    time.sleep(2.0)
                    continue

                after_boot_id = get_boot_id()
                if before_boot_id and after_boot_id and after_boot_id == before_boot_id:
                    self._log(
                        "info",
                        label,
                        f"{getattr(host, 'hostname', '')} SSH up but reboot not completed (boot_id unchanged)",
                    )
                    time.sleep(2.0)
                    continue

                if before_boot_id is None and not saw_down:
                    self._log(
                        "info",
                        label,
                        f"{getattr(host, 'hostname', '')} SSH still up, waiting for reboot to take effect",
                    )
                    time.sleep(2.0)
                    continue

                self._log(
                    "info",
                    label,
                    f"{getattr(host, 'hostname', '')} reboot completed (attempt={attempt})",
                )
                return
            except Exception:
                saw_down = True
                time.sleep(2.0)

        raise PipelineError(label, f"SSH reconnect timeout after reboot: host={getattr(host, 'hostname', '')} ip={getattr(host, 'ip', '')}")

    def _finalize_pve_wireless(
        self,
        host: PveHost,
        *,
        label: str,
        ssh_reconnect_timeout_s: float,
        reboot_on_finalize: bool,
    ) -> None:
        wireless_ip = self._ip_only(str((host.network_config or {}).get("wireless_ip", "")))
        wired_ip = self._ip_only(str((host.network_config or {}).get("wired_ip", "")))

        reachable = self._ensure_ssh_reachable(
            host,
            candidates=[getattr(host, "ip", ""), wired_ip, wireless_ip],
            timeout_s=float(ssh_reconnect_timeout_s),
            label=label,
        )
        if not reachable:
            raise PipelineError(
                label,
                f"节点 SSH 不可达，无法切回无线；current_ip={getattr(host, 'ip', '')} wired_ip={wired_ip} wireless_ip={wireless_ip}",
            )

        host.configure_network("wireless")
        if reboot_on_finalize:
            self._reboot_and_wait_ssh(
                host,
                timeout_s=float(ssh_reconnect_timeout_s),
                grace_s=10.0,
                label=label,
            )

        if wireless_ip:
            host.ip = wireless_ip

    def _check_cluster_quorum(self, api_host: ServerHost) -> None:
        step = "Quorum"
        try:
            items = api_host.pve_api.cluster.status.get()  # type: ignore[union-attr]
            quorate: bool | None = None
            for it in items or []:
                if isinstance(it, dict) and ("quorate" in it):
                    try:
                        quorate = bool(int(it.get("quorate")))
                    except Exception:
                        quorate = bool(it.get("quorate"))
                    break
            if quorate is not True:
                raise PipelineError(step, "集群未达法定人数（quorum=false），禁止发起迁移")
        except PipelineError:
            raise
        except Exception:
            out, err = api_host.execute_ssh("pvecm status | egrep -i 'Quorate|Writable|Filesystem' || true")
            text = (out or "") + (err or "")
            if re.search(r"Quorate:\s*Yes", text, flags=re.IGNORECASE) is None:
                raise PipelineError(step, f"无法确认 quorum，pvecm status 输出不含 Quorate: Yes\n{text}")

        out2, _ = api_host.execute_ssh("test -w /etc/pve && echo WRITABLE || echo READONLY")
        if "WRITABLE" not in (out2 or ""):
            raise PipelineError(step, "/etc/pve 不可写（pmxcfs 可能只读），禁止发起迁移")

    def _wait_for_pve_node_ready(
        self,
        api_host: ServerHost,
        src_runner: BaseHost,
        *,
        node_name: str,
        timeout_s: float,
        interval_s: float = 3.0,
    ) -> None:
        step = "ClusterReady"
        node_name = str(node_name or "").strip()
        deadline = time.time() + float(timeout_s)
        last_reason = ""

        while time.time() < deadline:
            ready = True

            # 1) 源节点本机 quorum / pmxcfs 可写
            try:
                out, err = src_runner.execute_ssh(
                    "pvecm status | egrep -i 'Quorate|Writable|Filesystem' || true"
                )  # type: ignore[attr-defined]
                text = (out or "") + (err or "")
                if re.search(r"Quorate:\s*Yes", text, flags=re.IGNORECASE) is None:
                    ready = False
                    last_reason = "source quorum not ready"
            except Exception as e:
                ready = False
                last_reason = f"source pvecm status failed: {e}"

            if ready:
                try:
                    out2, _ = src_runner.execute_ssh(
                        "test -w /etc/pve && echo WRITABLE || echo READONLY"
                    )  # type: ignore[attr-defined]
                    if "WRITABLE" not in (out2 or ""):
                        ready = False
                        last_reason = "source /etc/pve not writable"
                except Exception as e:
                    ready = False
                    last_reason = f"source /etc/pve check failed: {e}"

            # 2) API 视角：节点在线
            if ready and node_name:
                api_online: bool | None = None
                try:
                    items = api_host.pve_api.cluster.resources.get(type="node") or []  # type: ignore[union-attr]
                    for it in items:
                        if not isinstance(it, dict) or it.get("type") != "node":
                            continue
                        if str(it.get("node")) != node_name:
                            continue
                        status = str(it.get("status", "")).lower()
                        api_online = status in {"online", "ok", "running"}
                        break
                except Exception:
                    api_online = None

                if api_online is False:
                    ready = False
                    last_reason = f"api reports node {node_name} offline"

            if ready:
                self._log("info", step, f"Node ready: {node_name or 'unknown'}")
                return

            self._log(
                "info",
                step,
                f"Waiting for PVE node to be ready: {node_name or 'unknown'} (reason={last_reason})",
            )
            time.sleep(float(interval_s))

        raise PipelineError(step, f"等待节点就绪超时：node={node_name or 'unknown'} last={last_reason}")

    def _deploy_to_bare_metal(self, vmid: int, target: BareMetalHost) -> bool:
        raise PipelineError(
            "BareMetal",
            f"DeploymentPipeline 不支持直接部署到裸机目标 {getattr(target, 'host_id', getattr(target, 'hostname', ''))}。"
            "请使用 DataCenter.deploy_baremetal() 执行 Clonezilla 裸机部署流程。",
        )

    def _remove_hostpci_passthrough(self, host: BaseHost, vmid: int) -> None:
        try:
            out, err = host.execute_ssh(f"qm config {int(vmid)}")  # type: ignore[attr-defined]
            text = (out or "") + (err or "")
            keys = sorted(set(re.findall(r"^hostpci\d+(?=:)", text, flags=re.MULTILINE)))
            for k in keys:
                host.execute_ssh(f"qm set {int(vmid)} --delete {k}")  # type: ignore[attr-defined]
            if keys:
                self._log(
                    "info",
                    f"HostPCI/{getattr(host, 'hostname', '')}",
                    f"Removed passthrough: {keys}",
                )
        except Exception as e:
            raise PipelineError("PreMigration", f"remove hostpci passthrough failed: {e}", cause=e)

    def _ensure_usb_migratable(self, host: BaseHost, vmid: int) -> None:
        out, err = host.execute_ssh(f"qm config {int(vmid)}")  # type: ignore[attr-defined]
        text = (out or "") + (err or "")
        usb_keys = sorted(set(re.findall(r"^usb\d+(?=:)", text, flags=re.MULTILINE)))
        has_host_passthrough = any(re.search(rf"^{re.escape(k)}:\s*host=", text, flags=re.MULTILINE) for k in usb_keys)
        if not usb_keys and not has_host_passthrough:
            return

        for k in usb_keys:
            host.execute_ssh(f"qm set {int(vmid)} --delete {k}")  # type: ignore[attr-defined]
        host.execute_ssh(f"qm set {int(vmid)} --usb0 spice")  # type: ignore[attr-defined]

        out2, err2 = host.execute_ssh(f"qm config {int(vmid)}")  # type: ignore[attr-defined]
        text2 = (out2 or "") + (err2 or "")
        bad = re.findall(r"^usb\d+:\s*host=", text2, flags=re.MULTILINE)
        ok_spice = re.search(r"^usb0:\s*spice\b", text2, flags=re.MULTILINE) is not None
        if bad or not ok_spice:
            raise PipelineError(
                "PreMigration",
                f"USB 迁移前清理失败：仍存在本地 USB 直通或未设置 usb0: spice（bad={len(bad)} ok_spice={ok_spice}）",
            )

    def _select_usb_whitelist(self, target_host: BaseHost, requested: Iterable[str] | None) -> list[str]:
        cfg_whitelist = getattr(self.config, "usb_whitelist", None)
        default_requested = list(cfg_whitelist) if isinstance(cfg_whitelist, (list, tuple)) else None
        patterns = [p.strip() for p in (requested or default_requested or []) if str(p).strip()]
        if not patterns:
            return []

        try:
            out, err = target_host.execute_ssh("lsusb")  # type: ignore[attr-defined]
            text = (out or "") + (err or "")
        except Exception as e:
            self._log(
                "warning",
                f"USB/{getattr(target_host, 'hostname', '')}",
                f"lsusb failed: {e}",
            )
            return []

        present = sorted(set(re.findall(r"\bID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\b", text)))
        matched: list[str] = []
        for vp in present:
            if any(self._match_vidpid(vp, p) for p in patterns):
                matched.append(vp.lower())
        return matched


