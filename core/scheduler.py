"""core/scheduler.py — 统一调度器：任务队列 + 资源分配 + 后台派发执行。

闭环：submit_task → try_schedule → allocate → dispatch(pipeline) → release → re-schedule
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Set

from domain.image import RobotAppImage
from infrastructure.base_host import BaseHost

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HostProfile:
    host_id: str
    kind: str  # 'PVE' | 'BareMetal' | 'Server'
    labels: Set[str] = field(default_factory=set)
    capabilities: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageProfile:
    image_id: str
    labels: Set[str] = field(default_factory=set)
    software: Dict[str, Any] = field(default_factory=dict)
    hardware_requirements: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskRequirements:
    required_host_kinds: Optional[Set[str]] = None
    required_host_labels: Set[str] = field(default_factory=set)
    required_image_labels: Set[str] = field(default_factory=set)
    required_capabilities: Dict[str, Any] = field(default_factory=dict)
    required_software: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Allocation:
    task_id: str
    host_id: str
    image_id: str
    allocated_at: float


@dataclass
class TaskRequest:
    task_id: str
    requirements: TaskRequirements
    target_image_id: Optional[str] = None
    priority: str = "C"
    estimated_duration_s: Optional[float] = None
    estimated_migration_s: Optional[float] = None
    estimated_resume_penalty_s: Optional[float] = None
    estimated_start_time: Optional[float] = None
    submitted_at: float = field(default_factory=time.time)


# ── Scheduler ────────────────────────────────────────────────────────────────

class Scheduler:
    """统一调度器（Image-Centric, FIFO + 优先级 + 内置派发执行）。

    submit_task() 后立即尝试调度并派发；release() 后自动重新调度等待队列。
    """

    def __init__(
        self,
        datacenter: Any = None,  # DataCenter reference for pipeline execution
        *,
        max_workers: int = 3,
        on_task_done: Optional[Callable[[str, bool], None]] = None,
    ) -> None:
        self._dc = datacenter
        self._on_task_done = on_task_done

        # ── Resource registries ──
        self.hosts: Dict[str, BaseHost] = {}
        self.host_profiles: Dict[str, HostProfile] = {}
        self.free_hosts: Deque[str] = deque()
        self._free_hosts_set: Set[str] = set()
        self.busy_hosts: Set[str] = set()

        self.images: Dict[str, RobotAppImage] = {}
        self.image_profiles: Dict[str, ImageProfile] = {}
        self.free_images: Deque[str] = deque()
        self._free_images_set: Set[str] = set()
        self.busy_images: Set[str] = set()

        # ── Task queues ──
        self.pending_tasks: Deque[TaskRequest] = deque()
        self.allocations: Dict[str, Allocation] = {}
        self.host_to_task: Dict[str, str] = {}
        self.image_to_task: Dict[str, str] = {}

        # ── Internal dispatch ──
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._dispatch_lock = threading.Lock()

        # Require cable confirmation for background dispatch?
        if self._dc is not None:
            config = getattr(self._dc, "config", None)
            system_cfg = getattr(config, "system", None) if config is not None else None
            self._require_cable_confirmation = bool(
                getattr(system_cfg, "require_cable_confirmation", False)
            )
        else:
            self._require_cable_confirmation = False
        require_confirm = (os.environ.get("MF_PIPELINE_REQUIRE_CABLE_CONFIRMATION") or "").strip().lower()
        if require_confirm in {"1", "true", "yes", "y"}:
            self._require_cable_confirmation = True
        elif require_confirm in {"0", "false", "no", "n"}:
            self._require_cable_confirmation = False

    # ── Registration ────────────────────────────────────────────────────

    def register_host(self, host: BaseHost, profile: HostProfile, *, is_free: bool = True) -> None:
        self.hosts[str(host.host_id)] = host
        self.host_profiles[str(profile.host_id)] = profile
        if is_free:
            self.mark_host_free(str(profile.host_id))
        else:
            self.mark_host_busy(str(profile.host_id))

    def register_image(self, image: RobotAppImage, profile: ImageProfile, *, is_free: bool = True) -> None:
        self.images[str(image.image_id)] = image
        self.image_profiles[str(profile.image_id)] = profile
        if is_free:
            self.mark_image_free(str(profile.image_id))
        else:
            self.mark_image_busy(str(profile.image_id))

    # ── State transitions ───────────────────────────────────────────────

    @staticmethod
    def _remove_from_queue(q: Deque[str], item: str) -> None:
        if not q:
            return
        try:
            q.remove(item)
        except ValueError:
            return

    def mark_host_free(self, host_id: str) -> None:
        host_id = str(host_id)
        self.busy_hosts.discard(host_id)
        if host_id not in self._free_hosts_set:
            self.free_hosts.append(host_id)
            self._free_hosts_set.add(host_id)

    def mark_host_busy(self, host_id: str) -> None:
        host_id = str(host_id)
        self.busy_hosts.add(host_id)
        if host_id in self._free_hosts_set:
            self._free_hosts_set.discard(host_id)
            self._remove_from_queue(self.free_hosts, host_id)

    def mark_image_free(self, image_id: str) -> None:
        image_id = str(image_id)
        self.busy_images.discard(image_id)
        if image_id not in self._free_images_set:
            self.free_images.append(image_id)
            self._free_images_set.add(image_id)

    def mark_image_busy(self, image_id: str) -> None:
        image_id = str(image_id)
        self.busy_images.add(image_id)
        if image_id in self._free_images_set:
            self._free_images_set.discard(image_id)
            self._remove_from_queue(self.free_images, image_id)

    # ── Task submission (with immediate dispatch attempt) ───────────────

    def submit_task(self, task: TaskRequest) -> None:
        self.pending_tasks.append(task)
        self._append_task_log(str(task.task_id), f"[Scheduler] task submitted: task_id={task.task_id}")
        # 立即尝试调度 + 派发
        self._try_dispatch_next()

    # ── Task query ──────────────────────────────────────────────────────

    def get_running_tasks(self) -> list[dict]:
        tasks: list[dict] = []
        for task_id, alloc in (self.allocations or {}).items():
            try:
                tasks.append({
                    "task_id": str(getattr(alloc, "task_id", task_id)),
                    "image_id": str(getattr(alloc, "image_id", "")),
                    "host_id": str(getattr(alloc, "host_id", "")),
                    "allocated_at": float(getattr(alloc, "allocated_at", 0.0) or 0.0),
                })
            except Exception:
                continue
        tasks.sort(key=lambda x: float(x.get("allocated_at", 0.0) or 0.0))
        return tasks

    def get_pending_tasks(self) -> list[dict]:
        pending: list[dict] = []
        for t in list(self.pending_tasks or deque()):
            if not isinstance(t, TaskRequest):
                continue
            try:
                pending.append({
                    "task_id": str(getattr(t, "task_id", "")),
                    "priority": str(getattr(t, "priority", "")),
                    "requirements": getattr(t, "requirements", None),
                    "created_at": float(getattr(t, "submitted_at", 0.0) or 0.0),
                })
            except Exception:
                continue
        return pending

    def cancel_pending_task(self, task_id: str) -> bool:
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        try:
            for idx, t in enumerate(self.pending_tasks):
                if isinstance(t, TaskRequest) and str(getattr(t, "task_id", "")) == task_id:
                    del self.pending_tasks[idx]
                    self._append_task_log(task_id, "[Scheduler] pending task cancelled", level="INFO")
                    return True
        except Exception:
            pass
        try:
            before = len(self.pending_tasks)
            self.pending_tasks = deque([
                t for t in list(self.pending_tasks)
                if not (isinstance(t, TaskRequest) and str(getattr(t, "task_id", "")) == task_id)
            ])
            if len(self.pending_tasks) != before:
                self._append_task_log(task_id, "[Scheduler] pending task cancelled", level="INFO")
                return True
        except Exception:
            return False
        return False

    # ── Release ─────────────────────────────────────────────────────────

    def release(self, task_id: str) -> bool:
        allocation = self.allocations.pop(str(task_id), None)
        if allocation is None:
            return False
        try:
            if self.host_to_task.get(str(allocation.host_id)) == str(task_id):
                self.host_to_task.pop(str(allocation.host_id), None)
        except Exception:
            pass
        try:
            if self.image_to_task.get(str(allocation.image_id)) == str(task_id):
                self.image_to_task.pop(str(allocation.image_id), None)
        except Exception:
            pass
        self.mark_host_free(allocation.host_id)
        self.mark_image_free(allocation.image_id)
        logger.info("[Scheduler] released: task_id=%s", task_id)
        # 释放后立即尝试调度等待中的任务
        self._try_dispatch_next()
        return True

    def bind_manual_allocation(
        self, host_id: str, image_id: str, *, task_id: str | None = None,
    ) -> Allocation:
        """为手工迁移成功后的开发环境补登记 allocation/索引/忙闲状态。"""
        host_id = str(host_id or "").strip()
        image_id = str(image_id or "").strip()
        if not host_id:
            raise ValueError("host_id 不能为空")
        if not image_id:
            raise ValueError("image_id 不能为空")
        if host_id not in self.hosts:
            raise KeyError(f"未注册主机：{host_id}")
        if image_id not in self.images:
            raise KeyError(f"未注册镜像：{image_id}")

        # 释放旧关联
        related_task_ids: list[str] = []
        for tid in (self.host_to_task.get(host_id), self.image_to_task.get(image_id)):
            tid_s = str(tid or "").strip()
            if tid_s and tid_s not in related_task_ids:
                related_task_ids.append(tid_s)
        for old_task_id in related_task_ids:
            old = self.allocations.pop(str(old_task_id), None)
            if old is not None:
                try:
                    if self.host_to_task.get(str(old.host_id)) == str(old_task_id):
                        self.host_to_task.pop(str(old.host_id), None)
                except Exception:
                    pass
                try:
                    if self.image_to_task.get(str(old.image_id)) == str(old_task_id):
                        self.image_to_task.pop(str(old.image_id), None)
                except Exception:
                    pass
                self.mark_host_free(str(old.host_id))
                self.mark_image_free(str(old.image_id))
            else:
                if self.host_to_task.get(host_id) == str(old_task_id):
                    self.host_to_task.pop(host_id, None)
                if self.image_to_task.get(image_id) == str(old_task_id):
                    self.image_to_task.pop(image_id, None)

        task_id_s = str(task_id or "").strip()
        if not task_id_s:
            task_id_s = f"manual-{time.strftime('%Y%m%d-%H%M%S')}-{image_id}-{host_id}"

        allocation = Allocation(
            task_id=task_id_s, host_id=host_id, image_id=image_id,
            allocated_at=time.time(),
        )
        self.allocations[task_id_s] = allocation
        self.host_to_task[host_id] = task_id_s
        self.image_to_task[image_id] = task_id_s
        self.mark_host_busy(host_id)
        self.mark_image_busy(image_id)
        self._append_task_log(task_id_s, f"[Scheduler] manual allocation bound: task_id={task_id_s} host_id={host_id} image_id={image_id}")
        logger.info("[Scheduler] manual allocation bound: task_id=%s host_id=%s image_id=%s", task_id_s, host_id, image_id)
        return allocation

    # ── Core allocation ─────────────────────────────────────────────────

    def _try_schedule_next(self) -> Optional[Allocation]:
        """从等待队列中挑选一个可执行任务并分配资源。"""
        if not self.pending_tasks:
            return None

        def prio_value(p: str) -> int:
            p = str(p or "").strip().upper()
            if p == "A":
                return 0
            if p == "B":
                return 10
            return 20

        tasks = [t for t in list(self.pending_tasks) if isinstance(t, TaskRequest)]
        tasks.sort(key=lambda t: (
            prio_value(getattr(t, "priority", "C")),
            float(getattr(t, "submitted_at", 0.0) or 0.0),
        ))

        allocated: Optional[Allocation] = None
        remaining: Deque[TaskRequest] = deque()
        for task in tasks:
            if allocated is None:
                allocation = self._allocate_task(task)
                if allocation is not None:
                    allocated = allocation
                    continue
            remaining.append(task)
        self.pending_tasks = remaining
        return allocated

    def _allocate_task(self, task: TaskRequest) -> Optional[Allocation]:
        if task.task_id in self.allocations:
            return self.allocations[task.task_id]

        image_id = self._select_image_for_task(task)
        if image_id is None:
            logger.debug("[Scheduler] no matching image: task_id=%s", task.task_id)
            return None
        logger.info("[Scheduler] Found matching image: task_id=%s image_id=%s", task.task_id, image_id)

        final_reqs = self._build_final_hardware_requirements(task, image_id)
        host_id = self._select_host_for_hardware_requirements(task, final_reqs)
        if host_id is None:
            logger.debug("[Scheduler] no matching host: task_id=%s image_id=%s", task.task_id, image_id)
            return None
        logger.info("[Scheduler] Found matching host: task_id=%s host_id=%s", task.task_id, host_id)

        allocation = Allocation(
            task_id=str(task.task_id),
            host_id=str(host_id),
            image_id=str(image_id),
            allocated_at=time.time(),
        )
        self.allocations[str(task.task_id)] = allocation
        self.host_to_task[str(allocation.host_id)] = str(allocation.task_id)
        self.image_to_task[str(allocation.image_id)] = str(allocation.task_id)
        self.mark_host_busy(host_id)
        self.mark_image_busy(image_id)
        logger.info(
            "[Scheduler] allocated: task_id=%s prio=%s host_id=%s image_id=%s",
            allocation.task_id, str(getattr(task, "priority", "")),
            allocation.host_id, allocation.image_id,
        )
        return allocation

    def _select_image_for_task(self, task: TaskRequest) -> Optional[str]:
        target = (task.target_image_id or "").strip() if isinstance(task.target_image_id, str) else task.target_image_id
        if target:
            image_id = str(target)
            if image_id not in self.images:
                return None
            if image_id not in self._free_images_set:
                return None
            prof = self.image_profiles.get(str(image_id))
            if prof is None or not self._check_image_match(task, prof):
                return None
            return image_id
        for free_image_id in list(self.free_images):
            prof = self.image_profiles.get(str(free_image_id))
            if prof is None:
                continue
            if not self._check_image_match(task, prof):
                continue
            return str(free_image_id)
        return None

    def _check_image_match(self, task: TaskRequest, image_profile: ImageProfile) -> bool:
        req = task.requirements
        required_labels = set(req.required_image_labels or set())
        if required_labels and not required_labels.issubset(image_profile.labels):
            return False
        for k, v in (req.required_software or {}).items():
            if k not in (image_profile.software or {}):
                return False
            if image_profile.software.get(k) != v:
                return False
        return True

    def _build_final_hardware_requirements(self, task: TaskRequest, image_id: str) -> Dict[str, Any]:
        image_profile = self.image_profiles.get(str(image_id))
        image_reqs = dict(image_profile.hardware_requirements) if image_profile else {}
        task_reqs = dict(task.requirements.required_capabilities or {})
        return {**image_reqs, **task_reqs}

    def _select_host_for_hardware_requirements(self, task: TaskRequest, final_reqs: Dict[str, Any]) -> Optional[str]:
        req = task.requirements
        required_host_kinds = set(req.required_host_kinds) if req.required_host_kinds else None
        required_host_labels = set(req.required_host_labels or set())
        for host_id in list(self.free_hosts):
            prof = self.host_profiles.get(str(host_id))
            if prof is None:
                continue
            if required_host_kinds is not None and prof.kind not in required_host_kinds:
                continue
            if required_host_labels and not required_host_labels.issubset(prof.labels):
                continue
            if not self._check_capabilities_match(prof.capabilities, final_reqs):
                continue
            return str(host_id)
        return None

    @staticmethod
    def _check_capabilities_match(host_caps: Dict[str, Any], req_caps: Dict[str, Any]) -> bool:
        for k, req_val in (req_caps or {}).items():
            if isinstance(req_val, bool):
                if req_val is False:
                    continue
                if k not in (host_caps or {}):
                    return False
                if host_caps.get(k) is not True:
                    return False
                continue
            if k not in (host_caps or {}):
                return False
            host_val = host_caps.get(k)
            if isinstance(req_val, (int, float)) and isinstance(host_val, (int, float)):
                if float(host_val) < float(req_val):
                    return False
                continue
            if isinstance(req_val, str) and isinstance(host_val, str):
                if host_val != req_val:
                    return False
                continue
            if host_val != req_val:
                return False
        return True

    # ── Task logger ─────────────────────────────────────────────────────

    @staticmethod
    def _append_task_log(task_id: str, message: str, *, level: str = "INFO") -> None:
        try:
            base_dir = Path(__file__).resolve().parent.parent
            logs_dir = base_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{task_id}.log"
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [{level}] {message}\n")
        except Exception:
            pass

    def _setup_task_logger(self, task_id: str) -> tuple[logging.Logger, list[logging.Handler]]:
        base_dir = Path(__file__).resolve().parent.parent
        logs_dir = base_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        logger_name = f"pipeline.{task_id}"
        task_logger = logging.getLogger(logger_name)
        task_logger.setLevel(logging.INFO)
        for h in list(task_logger.handlers):
            task_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        handler = logging.FileHandler(str(logs_dir / f"{task_id}.log"))
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        task_logger.addHandler(handler)
        task_logger.propagate = False
        return task_logger, [handler]

    # ── Internal dispatch ───────────────────────────────────────────────

    def _try_dispatch_next(self) -> None:
        """尝试调度并派发队首任务（线程安全）。"""
        with self._dispatch_lock:
            allocation = self._try_schedule_next()
            if allocation is None:
                return
        self._executor.submit(self._execute_allocation, allocation)

    def _is_vm_on_target(self, vmid: str, target_host_id: str) -> bool:
        if self._dc is None:
            return False
        try:
            target_host = self._dc.find_host(str(target_host_id))
            if target_host is None:
                return False
            target_node = str(getattr(target_host, "pve_node_name", "") or "").strip()
            if not target_node:
                return False
            current_node = self._dc.find_vm_current_node(int(vmid))
            return bool(current_node and str(current_node) == target_node)
        except Exception:
            return False

    def _execute_allocation(self, allocation: Allocation) -> None:
        from core.pve_pipeline import DeploymentPipeline, PipelineError

        success = False
        task_logger: logging.Logger | None = None
        task_handlers: list[logging.Handler] = []

        task_id = str(getattr(allocation, "task_id", ""))
        vmid = str(getattr(allocation, "image_id", ""))
        target_host_id = str(getattr(allocation, "host_id", ""))
        host_label = target_host_id or "?"
        if self._dc is not None:
            try:
                target_host = (getattr(self._dc, "hosts", {}) or {}).get(target_host_id)
                host_label = str(getattr(target_host, "hostname", "") or target_host_id or "?")
            except Exception:
                pass

        print(f"\n🚀 [Task {task_id}] 启动部署: {vmid} -> {host_label}", flush=True)
        try:
            task_logger, task_handlers = self._setup_task_logger(str(allocation.task_id))

            # Suppress print() in background dispatch
            prev_quiet: dict[str, bool] = {}
            if self._dc is not None:
                for hid, host in (getattr(self._dc, "hosts", {}) or {}).items():
                    try:
                        prev_quiet[str(hid)] = bool(getattr(host, "quiet", False))
                        setattr(host, "quiet", True)
                    except Exception:
                        continue

            try:
                pipeline = DeploymentPipeline(
                    self._dc.master_server,
                    hosts=getattr(self._dc, "hosts", {}) if self._dc is not None else {},
                    logger=task_logger,
                    task_id=str(allocation.task_id),
                    config=getattr(self._dc, "config", None) if self._dc is not None else None,
                )
                pipeline.require_cable_confirmation = bool(self._require_cable_confirmation)

                if self._is_vm_on_target(vmid, target_host_id):
                    task_logger.info(
                        "[Scheduler] VM already on target; skip: vmid=%s target=%s", vmid, target_host_id
                    )
                    success = True
                else:
                    success = bool(pipeline.execute_pipeline(vmid=vmid, target_host_id=target_host_id))
            finally:
                for hid, host in (getattr(self._dc, "hosts", {}) or {}).items():
                    if str(hid) not in prev_quiet:
                        continue
                    try:
                        setattr(host, "quiet", prev_quiet[str(hid)])
                    except Exception:
                        continue
        except PipelineError as e:
            if task_logger is not None:
                task_logger.error("[Scheduler] PipelineError: step=%s detail=%s", getattr(e, "step", "?"), getattr(e, "detail", str(e)))
            if getattr(e, "cause", None) is not None and task_logger is not None:
                task_logger.exception("[Scheduler] cause", exc_info=e.cause)
        except Exception:
            if task_logger is not None:
                task_logger.exception("[Scheduler] Unexpected error: task_id=%s", allocation.task_id)
        finally:
            if success:
                print(f"✅ [Task {task_id}] 部署成功", flush=True)
                if task_logger is not None:
                    task_logger.info("[Scheduler] Deployment success. Resources BUSY for development. Use CLI to release.")
            else:
                print(f"💥 [Task {task_id}] 部署失败", flush=True)
                try:
                    self.release(allocation.task_id)
                except Exception:
                    if task_logger is not None:
                        task_logger.warning("[Scheduler] release failed: task_id=%s", allocation.task_id)
                if task_logger is not None:
                    task_logger.info("[Scheduler] Task finished with failure: task_id=%s", allocation.task_id)

            cb = self._on_task_done
            if cb is not None:
                try:
                    cb(str(allocation.task_id), bool(success))
                except Exception:
                    logger.exception("[Scheduler] on_task_done callback failed: task_id=%s", allocation.task_id)

            for h in list(task_handlers or []):
                try:
                    h.close()
                except Exception:
                    pass
                try:
                    if task_logger is not None:
                        task_logger.removeHandler(h)
                except Exception:
                    pass
