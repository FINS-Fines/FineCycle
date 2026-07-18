# interactive_runner 操作指南

## 1. 工具说明

`interactive_runner` 是 FineCycle 在 `version1.0` 分支中的交互式 CLI 入口，用于完成以下操作：

- 连接 PVE Master 节点
- 扫描集群节点并注册可调度主机
- 注册 VM 镜像
- 扫描并注册 Clonezilla ISO 镜像
- 通过统一入口 `migrate <vmid> <host_id>` 执行部署
- 通过 `auto` 提交自动调度任务

默认启动命令：

```bash
python -m interactive_runner
```

## 2. 使用前准备

### 2.1 Python 依赖

`interactive_runner` 的基础依赖来自项目根目录的 `requirement.txt`：

- `paramiko`
- `proxmoxer`
- `pyyaml`

建议先安装：

```bash
pip install -r requirement.txt
```

终端增强依赖不是必需，但推荐安装：

- `rich`
- `prompt_toolkit`

```bash
pip install rich prompt_toolkit
```

如果不安装这两个包，CLI 会自动回退到纯文本模式，只是显示效果和命令补全能力较弱。

### 2.2 配置文件

工具默认读取项目根目录的 `config.yaml`。首次使用前建议至少确认以下字段：

- `system.demo_mode`
- `system.log_level`
- `system.require_cable_confirmation`
- `network_defaults.gateway`
- `network_defaults.prefixlen`
- `network_defaults.wired_interface`
- `network_defaults.wifi_interface`

其中 `network_defaults` 会在自动推断 PVE 节点网络配置失败时作为兜底参数使用。

### 2.3 集群连接信息

首次启动会进入初始化向导，因此需要提前准备：

- 一个可访问的 PVE Master 节点 IP 或主机名
- PVE API 用户名和密码
- SSH 用户名、端口和密码

向导中的默认值如下：

- PVE API 用户名：`root@pam`
- PVE API 端口：`8006`
- PVE API `verify_ssl`：默认关闭
- SSH 用户名：`root`
- SSH 端口：`22`

注意：

- 工具依赖 SSH 和 PVE API 都可访问
- 初始化向导默认假设集群节点可以共用同一套 SSH 凭据
- 如果节点地址无法自动解析，CLI 会要求你手动输入该节点的 SSH 地址

### 2.4 Bare-Metal 流程的额外准备

裸机部署基于 **Clonezilla ISO 镜像注册与绑定** 的方式完成。因此在使用裸机流程前，需要额外准备：

- 将 Clonezilla ISO 上传到 **Master 节点的 `local` ISO 存储**
- ISO 文件名需要满足 `clonezilla*.iso`，这样 `interactive_runner` 才能扫描到
- 如果文件名采用 `clonezilla-<IPv4>.iso` 格式，例如 `clonezilla-192.168.8.210.iso`，工具会自动解析默认 IP
- 如果文件名不包含 IP，注册时需要手动输入该 ISO 对应的 IP
- 至少准备两个可用的 Clonezilla ISO

原因是：

- 一个 Clonezilla ISO 会绑定到目标裸机
- 裸机部署执行时，系统还会再选择一个空闲的 Clonezilla ISO 作为源端发送镜像
- 源端与目标端不能使用同一个 Clonezilla ISO

如果你需要理解这些 Clonezilla ISO 是如何制作和部署的，可以参考 [scripts/DDC/readme.md](D:\Code\Finecycle\scripts\DDC\readme.md)。

## 3. 快速上手

### 3.1 启动工具

在项目根目录执行：

```bash
python -m interactive_runner
```

启动进入初始化向导。

### 3.2 初始化向导流程

启动后按顺序完成以下输入：

1. `Master Server IP/Hostname`
2. `PVE API 用户名`
3. `PVE API 密码`
4. 如有需要，配置高级参数：
   - `PVE API 端口`
   - `PVE API verify_ssl`
5. `SSH 用户名`
6. `SSH 端口`
7. `SSH 密码`

连接成功后，工具会自动执行这些动作：

- 连接 Master 节点
- 读取 Master 节点名
- 扫描 Master 节点 `local` 存储中的 `clonezilla*.iso`
- 询问你是否注册这些 Clonezilla ISO，并为它们记录 IP
- 扫描 PVE 集群节点
- 交互式注册节点
- 交互式注册各节点下的 VM 镜像

节点注册阶段的常见交互包括：

- 选择节点类型是 `Server` 还是 `Robot`
- 如果是 `Robot`，输入 CPU、RAM、GPU 能力和标签
- 选择是否把该节点上的 VM 注册为可调度镜像
- 为镜像填写标签、最小内存需求和是否需要 GPU

完成后进入命令提示符：

```text
platform>
```

## 4. 首次进入后建议操作

### 4.1 查看帮助

```text
help
```

### 4.2 查看已注册主机和 VM

```text
list
vms
```

这两条命令用于确认初始化向导中已纳管的主机和 VM 是否都正确显示。

### 4.3 查看 Clonezilla ISO 状态

```text
cz-list
```

这个命令会显示当前已注册的 Clonezilla ISO，包括：

- ISO 名称
- 所在节点
- 绑定 IP
- `FREE` / `BUSY` 状态

如果你在启动 CLI 后又向 Master 节点 `local` 存储上传了新的 Clonezilla ISO，可以执行：

```text
cz-register
```

它会重新扫描并追加注册新的 Clonezilla ISO。

### 4.4 查看主机和镜像详情

```text
info host <host_id>
info image <image_id>
```

用于确认：

- 主机标签与能力
- 镜像标签与硬件需求
- 当前调度状态
- 镜像来源节点
- 裸机是否已经绑定 Clonezilla ISO

## 5. 关键命令入口

### 5.1 自动调度

```text
auto
```

`auto` 是问答向导模式，会询问：

- 目标镜像标签
- 是否要求 GPU
- 最小 RAM
- 任务优先级

然后提交自动部署任务。

### 5.2 注册裸机

```text
register-baremetal [target_clonezilla_iso]
```

 `register-baremetal` ：

1. 输入裸机 ID
2. 输入目标磁盘名，例如 `sda` 或 `nvme0n1`
3. 选择一个空闲的目标 Clonezilla ISO
4. 将该 ISO 的 IP 绑定到这台裸机

因此，执行它之前应该先确保：

- 已经有可用的 Clonezilla ISO 被注册
- 目标 Clonezilla ISO 当前是 `FREE`

### 5.3 部署到 PVE 或裸机

```text
migrate <vmid> <host_id>
```

这是当前分支的统一部署入口：

- 如果 `host_id` 对应的是 PVE 主机，就走 PVE 部署流程
- 如果 `host_id` 对应的是 `BareMetalHost`，就走裸机部署流程

对于裸机部署，常见使用方式是：

```text
migrate <vmid> <baremetal_host_id>
```

执行裸机部署时，CLI 会：

- 检查目标裸机是否已经通过 `register-baremetal` 绑定目标 Clonezilla ISO
- 再从空闲 Clonezilla ISO 中选择一个作为源端
- 自动为源端 VM 配置 Clonezilla 启动环境
- 启动裸机克隆流水线

## 6. 最小可行流程

如果你只想跑通一次从 VM 到裸机的最小流程，可以按下面的顺序操作：

1. 安装依赖
2. 检查 `config.yaml`
3. 确保 Master 节点 `local` 存储中已经上传至少两个 `clonezilla*.iso`
4. 运行 `python -m interactive_runner`
5. 在初始化向导中注册 Clonezilla ISO、节点和 VM
6. 进入 `platform>` 后执行 `cz-list`，确认至少有一个目标 ISO 和一个额外空闲 ISO
7. 执行 `register-baremetal`
8. 执行 `migrate <vmid> <baremetal_host_id>`

如果只是部署到 PVE 目标主机，则不需要 `register-baremetal`，直接执行：

```text
migrate <vmid> <pve_host_id>
```

## 7. 日志与说明

- CLI 日志写入项目根目录下的 `logs/`
- 主日志文件为 `logs/interactive_runner.log`
- 任务型操作也会在 `logs/` 下生成对应日志文件
- `interactive_runner` 会在命令循环启动后后台监控新的标准命名 Clonezilla ISO，并自动发现部分新增镜像

