# FINECYCLE: A Full-Cycle Management Paradigm for Robotic Applications

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![ROS 2](https://img.shields.io/badge/ROS_2-Humble-blue.svg)](https://docs.ros.org/en/humble/)
[![Paper](https://img.shields.io/badge/Paper-ICRA_2026-green.svg)](#)

**FINECYCLE** is a hardware-virtualization-based management paradigm designed to streamline the deployment and development of robotic applications. By encapsulating the operating system, development environments, dependencies, and applications into unified **Robotic Images**, FINECYCLE achieves a zero-configuration, closed-loop workflow: *Deploy → Develop → Restore → Redeploy*.

This repository provides the guidelines and tools to build the FINECYCLE infrastructure, facilitating both local and high-performance collaborative development across heterogeneous robotic hosts.

---

## 📖 Table of Contents
1. [Cluster Setup](#1-cluster-setup)
2. [Robotic Image Deployment & Restoration](#2-robotic-image-deployment--restoration)
3. [Robotic Application Development](#3-robotic-application-development)
4. [Standard Image Template](#4-standard-image-template)

---

## 1. Cluster Setup
To operationalize the FINECYCLE paradigm, you need to set up a centralized storage server and configure the target robotic hosts. We utilize **KVM** and **Proxmox Virtual Environment (PVE)** as the underlying hypervisor management layer.

### 1.1 Central Server
The central server acts as a unified repository for robotic images and provides high-performance computational resources.
* **Installation:** Install [Proxmox VE (PVE)](https://www.proxmox.com/) on the server hardware.
* **Storage Configuration:** Configure a shared storage pool (e.g., NFS, Ceph, or ZFS) within PVE to host the images within the VMs.
* **ISO Repository Setup:** Upload the open-source Clonezilla ISO (or the customized versions mentioned in DDC Prerequisites) to the `local` ISO storage of the PVE node.
* **Baseline VM Initialization:** Following the procedure in [Standard Image Template](#4-standard-image-template), instantiate at least one baseline Virtual Machine with Standard Image Template.
### 1.2 Virtualized Robotic Hosts
Virtualized hosts are robotic computing units (e.g., Mini PCs) equipped with a hypervisor layer to seamlessly join the server's cluster.
* **Installation:** Install Proxmox VE on the host and join it to the central server's PVE Datacenter/Cluster.
  ![](figures/cluster.png)
* **Network Preparation:**  Since mobile robots typically rely on Wi-Fi and the wireless network card of the PVE host does not support cascading to the VM, an additional USB wireless network card is required to provide network connectivity for the VMs in the virtualization host.
* **Network Configuration (Wireless Setup):** Configure the wireless interface to ensure PVE cluster connectivity. Modify the host's `/etc/network/interfaces` file as `configs/interface.wireless`.
### 1.3 Bare-Metal Robotic Hosts
Bare-metal hosts represent conventional robotic platforms without a virtualization layer. They execute the robotic image natively.
* **Setup:** No hypervisor installation is required. Ensure the host architecture is compatible with the image (e.g., x86_64) and prepare a [Clonezilla](https://clonezilla.org/) live USB for disk cloning.

---

## 2. Robotic Image Deployment & Restoration
FINECYCLE supports a bidirectional image relocation loop, ensuring environmental consistency across platforms without manual reconfiguration. **A temporary physical Ethernet connection to the robotic host is required during both deployment and restoration; the connection can be terminated once the process is finalized.**

### 2.1 Forward Deployment (Server → Host)
Deploying a functional robotic image from the central server to the target host:
* **For Virtualized Hosts:**
  * **Cluster Live Migration (CLM):** Transfers only the volatile VM state for sub-second relocation (requires cluster shared storage).
    ![](figures/migration.png)
    * 💡 You can also utilize `scripts/CLM/deploy_clm.py` for automated CLM deployment. Please refer to `scripts/CLM/readme.md` for implementation details.
  * **Cluster Disk Cloning (CDC):**
    1. **Network Interface Switch:** Temporarily switch the host to a wired connection by modifying `/etc/network/interfaces`as `configs/interface.wired`. After modification, **reboot** the host to apply the changes.

    2. **Execute Disk Cloning:**  Clones the entire robotic image from the shared storage to the host's local disk via the PVE cluster network.
       ![](figures/CDC.png)
    3. **Network Interface Switch:**  After cloning, switch the network configuration back to `configs/interface.wireless`, connect the USB wireless network card directly to the VM where the image is located, and **reboot** the host.
  
    * 💡 Tip: Save your configurations as `interfaces.wired` and `interfaces.wireless` in the `/etc/network/` directory. You can then quickly toggle them using `cp interfaces.wired interfaces` (followed by `reboot`) before and after the deployment. You can also utilize `scripts/CDC/deploy_cdc.py` for automated CDC deployment. Please refer to `scripts/CDC/readme.md` for implementation details.
    
* **For Bare-Metal Hosts:**
  * **Direct Disk Cloning (DDC):**
    1. **Server-Side Preparation:** Mount a Clonezilla ISO to the specific VM containing your target robotic image on the central server.
    2. **Host-Side Preparation:** Insert a bootable Clonezilla live USB drive into the target bare-metal host.
    3. **Boot Environment:** Configure and boot *both* the VM and the bare-metal host into the Clonezilla live environment.
    4. **Cloning Process:** Once both systems are running Clonezilla, follow the [Clonezilla Tutorial](https://clonezilla.org/fine-print-live-doc.php?path=./clonezilla-live/doc/01_Save_disk_image/00-boot-clonezilla-live-cd.doc#00-boot-clonezilla-live-cd.doc) (typically utilizing the network clone feature) to transfer the raw robotic image block-by-block directly onto the bare-metal host's physical storage.
    * 💡 You can also utilize `scripts/DDC/deploy_ddc.py` for automated DDC deployment. Please refer to `scripts/DDC/readme.md` for implementation details.
### 2.2 Backward Restoration (Host → Server)
Once modifications or parameter tunings are validated on the robot, the finalized image must be archived back to the server:
* **From Virtualized Hosts:** Use PVE's native migration/cloning to push the updated VM disk back to the central storage repository.  Please refer to `scripts/CDC/readme.md` for implementation details.
* **From Bare-Metal Hosts:** Use Clonezilla (DDC) to capture the entire physical disk state and push the image file back to the server.  Please refer to `scripts/DDC/readme.md` for implementation details.

---

## 3. Robotic Application Development
FineCycle supports two primary development workflows, depending on the computational requirements of your tasks.

### 3.1 Local Development
For standard debugging and algorithmic refinement:
1. Deploy the robotic image directly to the **Robotic Host** (via CDC or DDC).
2. Developers access the host to modify code, compile, and validate the application within the actual physical constraints of the robot.
3. Upon completion, restore the image to the server.

### 3.2 Collaborative Development (via Mobile Disk Media)
For resource-intensive tasks (e.g., compiling large ROS 2 workspaces, training neural networks) that exceed the onboard computer's capabilities, FINECYCLE leverages **Mobile Disk Media** (e.g., Portable NVMe SSDs) to bridge the server and the host:
1. **DDC to Media:** follow the [Clonezilla Tutorial](https://clonezilla.org/fine-print-live-doc.php?path=./clonezilla-live/doc/01_Save_disk_image/00-boot-clonezilla-live-cd.doc#00-boot-clonezilla-live-cd.doc) (typically utilizing the disk-to-disk feature) to clone the robotic image onto the mobile disk media.
2. **Server-Side Computation:** Plug the media into the Central Server. Mount the disk into a VM via hardware pass-through and boot VM in the disk. The steps are as follows.
   ```bash
   ##Identify Disk ID and map Disk to VM(PVE Host Shell):
   ls -l /dev/disk/by-id/ | grep "usb"
   # Copy the complete ID (e.g., usb-Samsung_SSD_...) without "-part1"
   qm set <VM_ID> -scsi1 /dev/disk/by-id/<disk_ID>
   
   ##Configure VM (PVE Web UI):
   ##    Hardware: Select the original virtual disk and click Detach (Mandatory to avoid UUID conflicts).
   ##    Options: Set `scsi1` as the 1st priority in Boot Order.
   ```
3. **Host-Side Testing:** Unplug the media and connect it to the physical Robotic Host. Boot the robot directly from the mobile disk to immediately test the newly compiled algorithms.

*Note: This approach streamlines the develop-and-test iteration loop by eliminating the wait times associated with transferring massive disk images over the network.*

## 4. Standard Image Template
A standardized robotic platform that encapsulates the operating system, development environment, and dependencies required for robotic application development.
This platform enables developers to rapidly deploy a ready-to-use environment by cloning a pre-built system image, avoiding repetitive setup and configuration.

### 4.1 Features

Pre-configured environment: Includes OS, drivers, middleware, and libraries for robotic applications.

Rapid deployment: Clone a standardized disk image directly to your host without manual installation.

Development-ready: Applications can be developed and executed immediately, with no pre-configuration required.

Cross-host support: Works on both bare-metal and virtualized hosts.

### 4.2 Getting Started

1. Configure your VM (by mounting the ISO) or bare-metal host (using a live USB) to boot from the Clonezilla live environment.

2. Enter the WebDAV URL provided:[FineCycle WebDAV](http://admin:admin@fines-robot.sjtu.edu.cn/webdav/)

3. Choose device-image mode：FineStdImg-VM for virtualized hosts or FineStdImg-Disk for bare-metal hosts.

4. Follow the [Clonezilla Tutorial](https://clonezilla.org/fine-print-live-doc.php?path=./clonezilla-live/doc/01_Save_disk_image/00-boot-clonezilla-live-cd.doc#00-boot-clonezilla-live-cd.doc) to clone the image from the WebDAV server to your local disk.
   Once complete, the system will be bootable and contain the full robotic platform environment.

5. Boot into the system and directly develop your robotic applications.

## Start your robotic application development

No additional configuration or dependency installation is required.

📊 Use Cases

Robotic system prototyping

Classroom/laboratory teaching

Multi-host deployment with consistent environments

Benchmarking and performance evaluation

📜 License

This project is licensed under the [Mozilla Public License 2.0](LICENSE).
