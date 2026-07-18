# FINECYCLE: A Full-Cycle Management Paradigm for Robotic Applications

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![ROS 2](https://img.shields.io/badge/ROS_2-Humble-blue.svg)](https://docs.ros.org/en/humble/)
[![Paper](https://img.shields.io/badge/Paper-ICRA_2026-green.svg)](#)

**FINECYCLE** is a hardware-virtualization-based management paradigm designed to streamline the deployment and development of robotic applications. By encapsulating the operating system, development environments, dependencies, and applications into unified **Robotic Images**, FINECYCLE achieves a zero-configuration, closed-loop workflow: *Deploy → Develop → Restore → Redeploy*.

---

## 1. Cluster Setup

To operationalize the FINECYCLE paradigm, you need to set up a centralized storage server and configure the target robotic hosts. We utilize **KVM** and **Proxmox Virtual Environment (PVE)** as the underlying hypervisor management layer.

### 1.1 Central Server

The central server acts as a unified repository for robotic images and provides high-performance computational resources.

- **Installation:** Install [Proxmox VE (PVE)](https://www.proxmox.com/) on the server hardware.
- **Storage Configuration:** Configure a shared storage pool (e.g., NFS, Ceph, or ZFS) within PVE to host the images.
- **ISO Repository Setup:** Upload the open-source Clonezilla ISO to the `local` ISO storage of the PVE node.
- **Baseline VM Initialization:** Following the procedure in [Standard Image Template](#3-standard-image-template), instantiate at least one baseline Virtual Machine.

### 1.2 Virtualized Robotic Hosts

Virtualized hosts are robotic computing units (e.g., Mini PCs) equipped with a hypervisor layer.

- **Installation:** Install Proxmox VE on the host and join it to the central server's PVE Cluster.
  ![](materials/cluster.png)
- **Network Preparation:** Since mobile robots typically rely on Wi-Fi, an additional USB wireless network card is required to provide network connectivity for the VMs. Configure the wireless interface based on `WIRELESS_TEMPLATE` in `configs/netconf_templates.py`.

### 1.3 Bare-Metal Robotic Hosts

Bare-metal hosts are conventional robotic platforms without a virtualization layer.

- **Setup:** No hypervisor installation is required. Ensure the host architecture is compatible (e.g., x86_64) and prepare a [Clonezilla](https://clonezilla.org/) live USB for disk cloning.

---

## 2. Image Deployment, Development & Restoration

FINECYCLE supports a bidirectional image lifecycle: deploying images from server to robot, developing directly on the deployed environment, and restoring finalized images back to the server. The system supports two deployment pipelines — baremetal pipeline for bare-metal hosts, and pve pipeline for virtualized hosts.

> **A temporary physical Ethernet connection to the robotic host is required during deployment and restoration.**

For detailed instructions on deployment commands, development workflows, and automated scripts, please refer to the guides in the `document/` folder:

| Guide | Language |
|-------|----------|
| `document/interactive_runner_guide.en.md` | English |
| `document/interactive_runner_guide.zh-CN.md` | 简体中文 |

---

## 3. Standard Image Template

A standardized robotic platform that encapsulates the operating system, development environment, and dependencies. Clone the pre-built image to rapidly deploy a ready-to-use environment.

### Features

- **Pre-configured environment:** Includes OS, drivers, middleware, and libraries.
- **Rapid deployment:** Clone the standardized disk image directly without manual installation.
- **Cross-host support:** Works on both bare-metal and virtualized hosts.

### Getting Started

1. Boot your host from the Clonezilla live environment (ISO mount for VM, live USB for bare-metal).
2. Enter the WebDAV URL: [FineCycle WebDAV](http://admin:admin@fines-robot.sjtu.edu.cn/webdav/)
3. Choose device-image mode: `FineStdImg-VM` for virtualized hosts or `FineStdImg-Disk` for bare-metal hosts.
4. Follow the [Clonezilla Tutorial](https://clonezilla.org/fine-print-live-doc.php?path=./clonezilla-live/doc/01_Save_disk_image/00-boot-clonezilla-live-cd.doc#00-boot-clonezilla-live-cd.doc) to clone the image from WebDAV to your local disk.
5. Boot into the system and start developing.

---

## 🎬 Demo
[![Bilibili](https://img.shields.io/badge/Bilibili-00A1D6?style=flat-square&logo=bilibili&logoColor=white)](https://www.bilibili.com/video/BV1BXEL6oEuP/?share_source=copy_web&vd_source=99a85ba4e6c211edba7b6d86b3677aff)
[![YouTube](https://img.shields.io/badge/YouTube-FF0000?style=flat-square&logo=youtube&logoColor=white)](https://youtu.be/qALrNK9dYgc)
---

## License

This project is released under the MIT License.

## Citation

If you find this work helpful in your research, please cite our paper:

```bibtex
@inproceedings{wang2026finecycle,
  title={FINECYCLE: Towards a Full-Cycle Management Paradigm for Robotic Deployment and Development},
  author={Wang, Haolin and Xi, Wang and Zhu, Zhiyuan and Fang, Chongrong and He, Jianping},
  booktitle={IEEE International Conference on Robotics and Automation (ICRA)},
  year={2026}
}
