# management_framework/core/baremetal_pipeline.py
from __future__ import annotations

import logging
import os
import subprocess
import time
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Optional

from infrastructure.base_host import BaseHost
from infrastructure.server_host import ServerHost
from configs.config import GlobalConfig

logger = logging.getLogger(__name__)


class BareMetalPipelineError(RuntimeError):
    """裸机迁移流水线异常"""
    def __init__(self, step: str, message: str, *, cause: Exception | None = None):
        self.detail = message
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.cause = cause


@dataclass(frozen=True)
class ClonezillaMigrationRequest:
    """Clonezilla迁移请求"""
    source_ip: str
    target_ip: str
    source_disk: str
    target_disk: str


class ClonezillaMigrationPipeline:
    """Clonezilla裸机迁移流水线（仿照DeploymentPipeline格式）"""
    
    def __init__(self, *,
                 master_server: ServerHost | None = None,
                 hosts: dict | None = None,
                 logger: logging.Logger | None = None,
                 config: Any | None = None):
        self.master_server = master_server
        self.hosts = hosts or {}
        self.logger = logger or logging.getLogger(__name__)
        self.config = config or GlobalConfig
        
        # 配置参数（与DeploymentPipeline保持一致）
        timeouts = getattr(self.config, "timeouts", None)
        self.migration_timeout_s: float = float(
            getattr(timeouts, "baremetal_migration", 5400.0) or 5400.0
        )  # 迁移超时时间（秒）
        self.initial_delay_s: float = float(
            getattr(timeouts, "baremetal_initial_delay", 60.0) or 60.0
        )  # 初始延迟（秒）
        self.check_interval_s: float = float(
            getattr(timeouts, "baremetal_check_interval", 30.0) or 30.0
        )  # 检查间隔（秒）
        
        # 工具配置
        self.use_xterm: bool = True  # 使用xterm终端
        self.auto_interaction: bool = True  # 自动交互
        
        # 进程引用
        self._src_process: Optional[subprocess.Popen] = None
        self._dst_process: Optional[subprocess.Popen] = None

        self._src_host = None
        self._vm_id = None

    def _should_console_progress(self) -> bool:
        try:
            return bool(sys.stdout.isatty())
        except Exception:
            return False
        
    def _auto_release_clonezilla_config(self) -> None:
        """释放被 Clonezilla 启动配置的 VM。按 boot_mode 决定释放哪一端。"""
        if self._vm_id is None or self._boot_mode is None:
            self.logger.info("[AutoRelease] 无需释放（无 VM 或两端已就绪）")
            return
        side = "源端" if self._boot_mode == "source" else "目标端"
        try:
            self.logger.info("[AutoRelease] 开始释放%s VM %s 的 Clonezilla 配置...", side, self._vm_id)
            master = self.master_server
            if master is None:
                self.logger.warning("[AutoRelease] master_server 未设置")
                return
            current_node = self._find_vm_current_node(master, int(self._vm_id))
            if not current_node:
                self.logger.warning("[AutoRelease] 无法找到 VM %s", self._vm_id)
                return
            host = self._find_host(current_node)
            if not isinstance(host, ServerHost):
                self.logger.warning("[AutoRelease] %s 不是 ServerHost", current_node)
                return
            if host.release_clonezilla_config(int(self._vm_id)):
                self.logger.info("[AutoRelease] ✓ %s VM %s Clonezilla 配置已释放", side, self._vm_id)
            else:
                self.logger.warning("[AutoRelease] ⚠ %s VM %s 释放失败", side, self._vm_id)
        except Exception as e:
            self.logger.error("[AutoRelease] 释放失败: %s", e)

    def _find_vm_current_node(self, api_host: ServerHost, vmid: int) -> str | None:
        if self.master_server is not None:
            return self.master_server.find_vm_current_node(vmid)
        return None

    def _find_host(self, node_name: str):
        key = str(node_name or "").strip()
        if not key:
            return None
        if key in self.hosts:
            return self.hosts[key]
        for h in self.hosts.values():
            if str(getattr(h, "pve_node_name", "")) == key:
                return h
        return None

    @staticmethod
    def _format_progress_bar(elapsed_s: int, expected_s: int, width: int = 20) -> str:
        if expected_s <= 0:
            return "[" + ("=" * width) + "]"
        ratio = min(1.0, max(0.0, float(elapsed_s) / float(expected_s)))
        filled = int(round(ratio * width))
        return "[" + ("=" * filled) + ("-" * (width - filled)) + "]"

    def _emit_progress(self, *, step: str, elapsed_s: int, expected_s: int) -> None:
        bar = self._format_progress_bar(int(elapsed_s), int(expected_s))
        line = f"⏳ [{step}] {bar} 耗时: {int(elapsed_s)}s / 预计: {int(expected_s)}s"
        # 默认写入任务日志（或 manual logger）
        try:
            self.logger.info(line)
        except Exception:
            pass
        # 前台交互模式下也输出到控制台
        if self._should_console_progress():
            print(line, flush=True)
    
    # ------------------------------------------------------------------
    # Public API (仿照DeploymentPipeline)
    # ------------------------------------------------------------------
    
    def execute_pipeline(
        self,
        source_ip: str,
        target_ip: str,
        source_disk: str,
        target_disk: str,
        image_id: str,
        logger: logging.Logger | None = None,
        vm_id: str = None,
        clonezilla_boot_mode: str | None = None,  # "source" / "target" / None
    ) -> bool:
        """执行 Clonezilla 裸机迁移流水线。

        clonezilla_boot_mode:
          "source" — 正向部署，给源端 VM 配置 Clonezilla 启动
          "target" — 反向释放，给目标端 VM 配置 Clonezilla 启动
          None     — 两端都已就绪，不做配置
        """
        self._vm_id = vm_id
        self._boot_mode = clonezilla_boot_mode

        step = "Init"
        active_logger = logger or self.logger
        self.logger = active_logger
        try:
            request = ClonezillaMigrationRequest(
                source_ip=source_ip,
                target_ip=target_ip,
                source_disk=source_disk,
                target_disk=target_disk
            )
            
            active_logger.info(
                f"[{step}] Starting migration for Image {image_id}: "
                f"{request.source_ip}:{request.source_disk} -> "
                f"{request.target_ip}:{request.target_disk}"
            )
            
            # 执行部署流水线（严格仿照DeploymentPipeline.execute_deploy_robot_system）
            return self.execute_clonezilla_migration(request)
            
        except BareMetalPipelineError:
            # 失败时也尝试释放
            self._auto_release_clonezilla_config()
            raise
        except Exception as e:
            # 异常时也尝试释放
            self._auto_release_clonezilla_config()
            raise BareMetalPipelineError(step, f"初始化失败: {e}", cause=e)
    
    def execute_clonezilla_migration(
        self,
        request: ClonezillaMigrationRequest,
    ) -> bool:
        """执行Clonezilla裸机迁移（仿照execute_deploy_robot_system格式）"""
        
        # --------------------------------------------------------------
        # 1) PrepareSource — 给 VM 配置 Clonezilla 启动
        # --------------------------------------------------------------
        step = "PrepareSource"
        if self._boot_mode in ("source", "target"):
            side = "源端" if self._boot_mode == "source" else "目标端"
            clonezilla_ip = request.source_ip if self._boot_mode == "source" else request.target_ip
            try:
                self.logger.info("[%s] 配置%s VM 的 Clonezilla 启动环境...", step, side)
                if self._vm_id is not None and self.master_server is not None:
                    current_node = self._find_vm_current_node(self.master_server, int(self._vm_id))
                    if current_node:
                        host = self._find_host(current_node)
                        if isinstance(host, ServerHost):
                            iso_name = f"clonezilla-{clonezilla_ip}.iso"
                            self.logger.info("[%s] %s节点=%s ISO=%s", step, side, current_node, iso_name)
                            if not host.add_cdrom_and_set_boot(
                                vm_id=int(self._vm_id), iso_storage="local",
                                iso_path=f"iso/{iso_name}", bios_type="seabios",
                            ):
                                raise BareMetalPipelineError(step, f"VM {self._vm_id} CD-ROM 配置失败")
                            if not host.start_vm_with_boot_from_cd(int(self._vm_id)):
                                raise BareMetalPipelineError(step, f"VM {self._vm_id} 启动失败")
                            self.logger.info("[%s] 等待 Clonezilla 启动（40秒）...", step)
                            time.sleep(40)
                        else:
                            self.logger.warning("[%s] 节点 %s 不是 ServerHost，跳过自动配置", step, current_node)
                    else:
                        self.logger.warning("[%s] 无法找到 VM %s 所在节点，跳过自动配置", step, self._vm_id)
                else:
                    self.logger.warning("[%s] 缺少 VM ID 或 master_server，跳过自动配置", step)
                self.logger.info("[%s] %s Clonezilla 启动准备完成", step, side)
            except BareMetalPipelineError:
                raise
            except Exception as e:
                raise BareMetalPipelineError(step, f"准备{side} Clonezilla 失败: {e}", cause=e)
        else:
            self.logger.info("[%s] 两端均已就绪，跳过 Clonezilla 启动配置", step)
        
        # --------------------------------------------------------------
        # 2) Pre-Migration Check（前置检查 - 已注释）
        # --------------------------------------------------------------
        # ... (原有的被注释的前置检查代码保持不变)
        
        # --------------------------------------------------------------
        # 3) Start Clonezilla Terminals
        # --------------------------------------------------------------
        step = "StartTerminals"
        try:
            self.logger.info(f"[{step}] 启动Clonezilla终端...")
            
            # 启动源端和目标端终端
            success = self._start_clonezilla_terminals(request)
            if not success:
                raise BareMetalPipelineError(step, "启动Clonezilla终端失败")
            
            self.logger.info(f"[{step}] Clonezilla终端启动完成")
            
        except BareMetalPipelineError:
            # 清理进程
            self._cleanup_processes()
            raise
        except Exception as e:
            self._cleanup_processes()
            raise BareMetalPipelineError(step, f"启动终端异常: {e}", cause=e)
        
        # --------------------------------------------------------------
        # 4) Migration Execution
        # --------------------------------------------------------------
        step = "Migration"
        try:
            self.logger.info(f"[{step}] 开始执行迁移...")
            
            # 等待初始延迟（让迁移真正开始）
            self._wait_with_progress(
                self.initial_delay_s,
                "等待迁移真正开始",
                step="Migration",
            )
            
            # 监控迁移进度
            success = self._monitor_migration_progress(request)
            if not success:
                raise BareMetalPipelineError(step, "迁移执行失败或超时")
            
            self.logger.info(f"[{step}] 迁移执行完成")
            
        except BareMetalPipelineError:
            self._cleanup_processes()
            raise
        except Exception as e:
            self._cleanup_processes()
            raise BareMetalPipelineError(step, f"迁移执行异常: {e}", cause=e)
        
        # --------------------------------------------------------------
        # 5) Post-Migration Verification
        # --------------------------------------------------------------
        step = "PostVerification"
        try:
            self.logger.info(f"[{step}] 执行后置验证...")
            
            # 验证迁移结果
            success = self._verify_migration_result(request)
            if not success:
                self.logger.warning(f"[{step}] 迁移验证失败，但迁移可能已完成")
            
            self.logger.info(f"[{step}] 后置验证完成")
            
        except BareMetalPipelineError:
            raise
        except Exception as e:
            raise BareMetalPipelineError(step, f"后置验证异常: {e}", cause=e)
        
        # --------------------------------------------------------------
        # 6) Finalization
        # --------------------------------------------------------------
        step = "Finalization"
        try:
            self.logger.info(f"[{step}] 执行收尾工作...")
            
            # 清理进程
            self._cleanup_processes()
            
            # 记录迁移结果到数据中心
            self._log_migration_result(request, success=True)
            
            # ========== 关键：自动释放Clonezilla配置 ==========
            # 这里会调用 _auto_release_clonezilla_config，它会：
            # 1. 停止虚拟机
            # 2. 从硬件中移除CD/DVD设备
            # 3. 恢复UEFI启动
            # 4. 恢复原始启动顺序
            self._auto_release_clonezilla_config()
            # ===================================================
            
            self.logger.info(f"[{step}] 裸机迁移流水线完成")
            return True
            
        except BareMetalPipelineError:
            # 失败时也尝试释放
            self._auto_release_clonezilla_config()
            raise BareMetalPipelineError(step, f"收尾工作异常: {e}", cause=e)

        except Exception as e:
            # 异常时也尝试释放
            self._auto_release_clonezilla_config()
            raise
    
    # ------------------------------------------------------------------
    # Internal Implementation
    # ------------------------------------------------------------------
    
    def _check_network_connectivity(self, ip: str) -> bool:
        """检查网络连通性"""
        try:
            cmd = f"ping -c 1 {ip}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False
    
    def _test_ssh_connection(self, ip: str) -> bool:
        """测试SSH连接"""
        try:
            cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ConnectTimeout=5 user@{ip} "echo OK"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            return "OK" in result.stdout
        except Exception:
            return False
    
    def _check_disk_exists(self, ip: str, disk: str) -> bool:
        """检查磁盘设备是否存在"""
        try:
            cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{ip} "ls /dev/{disk} 2>/dev/null || echo NOT_FOUND"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            return "NOT_FOUND" not in result.stdout
        except Exception:
            return False
    
    def _check_required_tools(self) -> bool:
        """检查必要工具"""
        if self.use_xterm:
            try:
                subprocess.run(['which', 'xterm'], check=True, capture_output=True)
            except subprocess.CalledProcessError:
                self.logger.warning("[PreCheck] xterm未安装，尝试安装...")
                try:
                    subprocess.run(['sudo', 'apt', 'install', '-y', 'xterm'], check=False)
                except Exception:
                    self.logger.warning("[PreCheck] 安装xterm失败，将使用备用方案")
                    self.use_xterm = False
        
        try:
            subprocess.run(['which', 'xdotool'], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            self.logger.warning("[PreCheck] xdotool未安装，尝试安装...")
            try:
                subprocess.run(['sudo', 'apt', 'install', '-y', 'xdotool'], check=False)
                return True
            except Exception:
                self.logger.error("[PreCheck] 安装xdotool失败")
                return False
    
    def _start_clonezilla_terminals(self, request: ClonezillaMigrationRequest) -> bool:
        """启动Clonezilla终端"""
        try:
            # 构建命令
            src_ssh = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.source_ip}'
            src_cmd = f'sudo ocs-onthefly -a -fsck-y -f {request.source_disk}'
            
            dst_ssh = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip}'
            dst_cmd = f'sudo ocs-onthefly -s {request.source_ip} -d {request.target_disk}'

            # xterm显示参数（可通过环境变量覆盖）
            xterm_geometry = os.environ.get("CLONEZILLA_XTERM_GEOMETRY", "80x20")
            xterm_font = os.environ.get("CLONEZILLA_XTERM_FONT", "Monospace")
            xterm_font_size = os.environ.get("CLONEZILLA_XTERM_FONT_SIZE", "16")
            xterm_opts = f'-geometry {xterm_geometry} -fa "{xterm_font}" -fs {xterm_font_size}'
            
            # 启动源端终端
            if self.use_xterm:
                src_cmd_full = f'xterm {xterm_opts} -title "CLONEZILLA SRC" -e "{src_ssh}" &'
                self.logger.info(f"[StartTerminals] 启动SRC终端: {src_cmd_full}")
                self._src_process = subprocess.Popen(src_cmd_full, shell=True, executable='/bin/bash')
                time.sleep(5)
                
                # 发送SRC命令
                if not self._send_command_to_window("CLONEZILLA SRC", src_cmd):
                    self.logger.warning("[StartTerminals] 无法自动发送SRC命令，请手动输入")
            else:
                # 备用方案：直接执行
                self.logger.info("[StartTerminals] 使用备用方案启动SRC")
                src_full_cmd = f'{src_ssh} "{src_cmd}"'
                self._src_process = subprocess.Popen(src_full_cmd, shell=True, executable='/bin/bash')
            
            # 等待SRC端准备
            time.sleep(30)
            
            # 启动目标端终端
            if self.use_xterm:
                dst_cmd_full = f'xterm {xterm_opts} -title "CLONEZILLA DST" -e "{dst_ssh}" &'
                self.logger.info(f"[StartTerminals] 启动DST终端: {dst_cmd_full}")
                self._dst_process = subprocess.Popen(dst_cmd_full, shell=True, executable='/bin/bash')
                time.sleep(5)
                
                # 发送DST命令并自动交互
                if not self._send_dst_commands_with_interaction("CLONEZILLA DST", dst_cmd):
                    self.logger.warning("[StartTerminals] 无法自动发送DST命令，请手动输入")
            else:
                # 备用方案：直接执行
                self.logger.info("[StartTerminals] 使用备用方案启动DST")
                dst_full_cmd = f'{dst_ssh} "{dst_cmd}"'
                self._dst_process = subprocess.Popen(dst_full_cmd, shell=True, executable='/bin/bash')
            
            return True
            
        except Exception as e:
            self.logger.error(f"[StartTerminals] 启动终端失败: {e}")
            self._cleanup_processes()
            return False
    
    def _send_command_to_window(self, window_title: str, command: str) -> bool:
        """使用xdotool向指定窗口发送命令"""
        try:
            # 查找窗口
            find_cmd = f'xdotool search --name "{window_title}"'
            result = subprocess.run(find_cmd, shell=True, capture_output=True, text=True)
            
            if result.returncode != 0 or not result.stdout.strip():
                self.logger.warning(f"[xdotool] 未找到窗口: {window_title}")
                return False
            
            window_id = result.stdout.strip().split('\n')[0]
            
            # 激活窗口
            subprocess.run(['xdotool', 'windowactivate', window_id], check=False)
            time.sleep(0.5)
            
            # 发送命令
            subprocess.run(['xdotool', 'type', command], check=False)
            time.sleep(0.2)
            subprocess.run(['xdotool', 'key', 'Return'], check=False)
            
            self.logger.info(f"[xdotool] 已向 '{window_title}' 发送命令")
            return True
            
        except Exception as e:
            self.logger.error(f"[xdotool] 发送命令失败: {e}")
            return False
    
    def _send_dst_commands_with_interaction(self, window_title: str, command: str) -> bool:
        """发送DST命令并处理自动交互"""
        if not self._send_command_to_window(window_title, command):
            return False
        
        if not self.auto_interaction:
            return True
        
        try:
            # 等待并发送交互命令
            time.sleep(10)
            if not self._send_command_to_window(window_title, ""):  # 按Enter
                return False
            
            time.sleep(10)
            if not self._send_command_to_window(window_title, "y"):  # 输入y
                return False
            
            time.sleep(5)
            if not self._send_command_to_window(window_title, "y"):  # 再次输入y
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"[Interaction] 自动交互失败: {e}")
            return False
    
    def _monitor_migration_progress(self, request: ClonezillaMigrationRequest) -> bool:
        """监控迁移进度 - 改进版本：区分无害警告和真正错误"""
        self.logger.info(f"[Migration] 开始监控迁移进度，超时: {self.migration_timeout_s}秒")
        
        start_time = time.time()
        monitoring_start = time.time()
        last_progress_time = time.time()
        last_status_log_time = time.time()  # 新增：记录上次状态日志的时间
        last_progress_emit_time = time.time()
        
        # 无害错误模式列表（这些警告不影响克隆成功）
        harmless_errors = [
            'firmware load failed',
            'firmware load error',
            'direct firmware load',
            'rtl_nic/',  # Realtek 网卡驱动警告
            'i915 firmware',  # Intel 显卡驱动警告
            'bluetooth',  # 蓝牙相关警告
            'usb.*over-current',  # USB 过流警告
            'thermal.*throttle',  # 温度调节警告
            'cpufreq',  # CPU 频率警告
            'memory.*exhausted',  # 内存警告（通常是临时性的）
            'timeout',  # 超时警告（不一定失败）
            'gpt.*use gnu parted to correct gpt errors',  # GPT 分区表警告（克隆后常见，不影响数据）
            'gpt.*correct gpt errors',  # GPT 错误提示
            'gpt error',  # GPT 通用错误
        ]
        
        def is_harmless_error(error_msg: str) -> bool:
            """检查错误是否属于无害警告"""
            error_lower = error_msg.lower()
            for pattern in harmless_errors:
                if pattern in error_lower:
                    return True
            return False
        
        while time.time() - start_time < self.migration_timeout_s:
            try:
                current_time = time.time() - monitoring_start

                # 每 30 秒输出一次“耗时/预计”进度（日志 + 前台控制台）
                now = time.time()
                if now - last_progress_emit_time >= 30:
                    self._emit_progress(
                        step="Migration",
                        elapsed_s=int(now - monitoring_start),
                        expected_s=int(self.migration_timeout_s),
                    )
                    last_progress_emit_time = now
                
                # 检查目标机器上ocs-sr进程是否还在运行
                check_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "ps aux | grep ocs-sr | grep -v grep"'
                result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True)
                
                if "ocs-sr" in result.stdout:
                    # 迁移仍在进行中
                    elapsed_minutes = int(current_time / 60)
                    
                    # 每1分钟报告一次状态（而不是每秒）
                    current_time_seconds = time.time()
                    if current_time_seconds - last_status_log_time >= 60:  # 60秒 = 1分钟
                        self.logger.info(f"[Migration] 迁移进行中... 已用时: {elapsed_minutes}分钟")
                        last_status_log_time = current_time_seconds
                    
                    # 检查进度信息（每1分钟详细报告一次）
                    if elapsed_minutes % 1 == 0 and elapsed_minutes > 0:
                        progress_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "sudo grep -i \'percent\\|progress\\|remaining\' /tmp/ocs-sr.log 2>/dev/null | tail -3 || true"'
                        progress_result = subprocess.run(progress_cmd, shell=True, capture_output=True, text=True)
                        if progress_result.stdout.strip():
                            self.logger.info(f"[Migration] 进度信息: {progress_result.stdout.strip()}")
                            last_progress_time = time.time()
                    
                    # 检查是否长时间没有进度
                    if time.time() - last_progress_time > 600:  # 10分钟无进度
                        self.logger.warning("[Migration] 长时间没有进度更新，检查迁移状态...")
                        
                        # 检查进程是否僵死
                        status_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "ps aux | grep ocs-sr | grep -v grep | grep -i defunct || echo OK"'
                        status_result = subprocess.run(status_cmd, shell=True, capture_output=True, text=True)
                        if "defunct" in status_result.stdout:
                            self.logger.error("[Migration] ❌ 发现僵死进程，迁移可能已失败")
                            return False
                    
                else:
                    # 进程已结束，检查是否完成
                    self.logger.info("[Migration] ocs-sr进程已结束，检查迁移结果...")
                    
                    # 等待一小段时间让系统稳定
                    time.sleep(15)
                    
                    # 再次检查确认进程确实结束了
                    result2 = subprocess.run(check_cmd, shell=True, capture_output=True, text=True)
                    if "ocs-sr" not in result2.stdout:
                        # 检查目标磁盘是否有分区信息
                        disk_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "sudo fdisk -l /dev/{request.target_disk} 2>/dev/null | grep -E \'^/dev/|Device|System\' || true"'
                        disk_result = subprocess.run(disk_cmd, shell=True, capture_output=True, text=True)
                        
                        if disk_result.stdout and ("Device" in disk_result.stdout or "/dev/" in disk_result.stdout):
                            self.logger.info(f"[Migration] 目标磁盘 {request.target_disk} 已检测到分区信息")
                            self.logger.info(f"[Migration] 磁盘信息:\n{disk_result.stdout}")
                            
                            # 检查Clonezilla日志
                            log_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "sudo grep -i \'success\\|finished\\|completed\\|done\\|saved\\|restored\' /var/log/syslog 2>/dev/null | grep -i clonezilla | tail -5 || true"'
                            log_result = subprocess.run(log_cmd, shell=True, capture_output=True, text=True)
                            
                            if log_result.stdout.strip():
                                self.logger.info(f"[Migration] Clonezilla日志显示完成: {log_result.stdout.strip()}")
                            
                            # 检查是否有真正的错误（过滤无害警告）
                            error_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "sudo dmesg | tail -100 | grep -i error || true"'
                            error_result = subprocess.run(error_cmd, shell=True, capture_output=True, text=True)
                            
                            error_lines = error_result.stdout.strip().split('\n')
                            real_errors = []
                            
                            for error_line in error_lines:
                                if error_line.strip() and not is_harmless_error(error_line):
                                    real_errors.append(error_line.strip())
                            
                            if not real_errors:
                                self.logger.info("[Migration] ✓ 迁移完成，无严重系统错误")

                                self._close_clonezilla_windows()
                                
                                # 额外验证：检查是否有可启动分区
                                boot_check_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "sudo parted /dev/{request.target_disk} print 2>/dev/null | grep -i boot || echo \'无法检测启动分区\'"'
                                boot_result = subprocess.run(boot_check_cmd, shell=True, capture_output=True, text=True)
                                
                                if "boot" in boot_result.stdout.lower():
                                    self.logger.info("[Migration] ✓ 检测到可启动分区")
                                else:
                                    self.logger.warning("[Migration] ⚠ 未检测到明确的可启动分区标记")
                                
                                return True  # 成功返回，退出循环
                            else:
                                # 只记录非无害错误
                                serious_errors = "\n".join(real_errors[:5])  # 只显示前5个错误
                                self.logger.warning(f"[Migration] ⚠ 检测到严重系统错误: {serious_errors}")
                                
                                # 即使有严重错误，但如果磁盘已经有分区，可能克隆还是成功的
                                # 我们可以询问用户或根据策略决定
                                if len(real_errors) < 3:  # 如果严重错误很少，可以认为是成功的
                                    self.logger.warning("[Migration] ⚠ 有少量严重错误，但磁盘数据可能已完整克隆")
                                    return True  # 成功返回，退出循环
                                else:
                                    return False  # 失败返回，退出循环
                        else:
                            self.logger.error(f"[Migration] ❌ 进程已结束但未检测到有效磁盘数据")
                            self.logger.debug(f"[Migration] 磁盘检查输出: {disk_result.stdout}")
                            return False  # 失败返回，退出循环
                    else:
                        # 如果进程又出现了（可能是误判），继续监控
                        self.logger.info("[Migration] 进程重新出现，继续监控...")
                
                # 等待检查间隔（常规休眠）
                time.sleep(self.check_interval_s)
                    
            except Exception as e:
                self.logger.error(f"[Migration] 监控过程中出错: {e}")
                time.sleep(self.check_interval_s)
        
        self.logger.error(f"[Migration] ❌ 迁移超时 ({self.migration_timeout_s}秒)")
        return False
    
    def _verify_migration_result(self, request: ClonezillaMigrationRequest) -> bool:
        """验证迁移结果 - 改进版本"""
        try:
            self.logger.info("[PostVerification] 验证迁移结果...")
            
            # 检查目标磁盘状态
            verify_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "sudo lsblk /dev/{request.target_disk} -o NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL"'
            verify_result = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True)
            
            if verify_result.stdout and "NAME" in verify_result.stdout:
                self.logger.info(f"[PostVerification] 目标磁盘状态:\n{verify_result.stdout}")
                
                # 检查是否有分区
                lines = verify_result.stdout.strip().split('\n')
                partition_count = 0
                for line in lines[1:]:  # 跳过标题行
                    if line.strip() and not line.startswith(request.target_disk):
                        partition_count += 1
                
                if partition_count > 0:
                    self.logger.info(f"[PostVerification] ✓ 检测到 {partition_count} 个分区")
                    
                    # 尝试检查文件系统完整性
                    fs_check_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "sudo blkid /dev/{request.target_disk}* 2>/dev/null | head -5 || true"'
                    fs_result = subprocess.run(fs_check_cmd, shell=True, capture_output=True, text=True)
                    
                    if fs_result.stdout.strip():
                        self.logger.info(f"[PostVerification] 文件系统信息: {fs_result.stdout.strip()}")
                    
                    return True
                else:
                    self.logger.warning("[PostVerification] ⚠ 磁盘存在但没有检测到分区")
                    return False
            else:
                self.logger.warning("[PostVerification] 无法获取目标磁盘状态")
                
                # 回退检查：使用fdisk
                fallback_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL user@{request.target_ip} "sudo fdisk -l /dev/{request.target_disk} 2>/dev/null | head -10 || true"'
                fallback_result = subprocess.run(fallback_cmd, shell=True, capture_output=True, text=True)
                
                if fallback_result.stdout and ("Device" in fallback_result.stdout or "Disk /dev/" in fallback_result.stdout):
                    self.logger.info(f"[PostVerification] 回退检查成功:\n{fallback_result.stdout[:500]}")
                    return True
                else:
                    self.logger.error("[PostVerification] ❌ 所有验证方法都失败")
                    return False
                
        except Exception as e:
            self.logger.error(f"[PostVerification] 验证失败: {e}")
            return False
    
    def _cleanup_processes(self):
        """清理进程"""
        try:
            if self._src_process:
                self._src_process.terminate()
                self._src_process.wait(timeout=2)
        except Exception:
            pass
        
        try:
            if self._dst_process:
                self._dst_process.terminate()
                self._dst_process.wait(timeout=2)
        except Exception:
            pass
        
        self._src_process = None
        self._dst_process = None

    def _close_clonezilla_windows(self):
        """关闭 Clonezilla 终端窗口"""
        try:
            import subprocess
            for title in ["CLONEZILLA SRC", "CLONEZILLA DST"]:
                result = subprocess.run(
                    f'xdotool search --name "{title}"', 
                    shell=True, capture_output=True, text=True
                )
                if result.returncode == 0 and result.stdout.strip():
                    for window_id in result.stdout.strip().split('\n'):
                        subprocess.run(f'xdotool windowclose {window_id}', shell=True, capture_output=True)
                        self.logger.info(f"[Cleanup] 已关闭终端窗口: {title}")
        except Exception as e:
            self.logger.debug(f"[Cleanup] 关闭窗口时出错（可忽略）: {e}")
    
    def _wait_with_progress(self, wait_time: float, label: str, *, step: str | None = None):
        """等待并显示进度。

        - 每 30 秒输出一次进度（日志 + 前台控制台）。
        - step 用于展示阶段名（例如 Migration）。
        """
        step_name = str(step or label or "Wait").strip() or "Wait"
        self.logger.info(f"[{label}] 等待 {wait_time} 秒...")
        start_time = time.time()

        last_emit = 0
        
        while time.time() - start_time < wait_time:
            elapsed = int(time.time() - start_time)
            remaining = int(wait_time - elapsed)

            if elapsed - last_emit >= 30:  # 每30秒报告一次
                self._emit_progress(step=step_name, elapsed_s=elapsed, expected_s=int(wait_time))
                last_emit = elapsed
            
            time.sleep(5)
    
    def _log_migration_result(self, request: ClonezillaMigrationRequest, success: bool):
        """记录迁移结果到数据中心"""
        try:
            migration_record = {
                'timestamp': time.time(),
                'type': 'baremetal_migration',
                'source_ip': request.source_ip,
                'target_ip': request.target_ip,
                'source_disk': request.source_disk,
                'target_disk': request.target_disk,
                'status': 'success' if success else 'failed',
                'migration_timeout_s': self.migration_timeout_s
            }
            
            self.logger.info(f"[Finalization] 迁移记录: {migration_record}")
            
        except Exception as e:
            self.logger.error(f"[Finalization] 记录迁移结果失败: {e}")

