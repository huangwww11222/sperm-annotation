# 医院离线服务器环境

制作离线更新包、判断 GPU/CUDA 兼容或估算磁盘空间时读。返回 [DOCKER_DEPLOYMENT.md](../DOCKER_DEPLOYMENT.md#医院内网可复用的离线升级流程)。

仓库公开，本文只记录与部署兼容相关的软硬件信息；主机名、IP、账号、网络拓扑和部署密钥不写入仓库。

## 当前记录

数据来自医院管理员在服务器上执行的命令截图（`nvidia-smi` 显示的采集时间为 2026-09-28）。服务器升级驱动、Docker 或更换硬件后重新采集并直接改写本表。

| 项目 | 记录值 | 来源命令 |
| --- | --- | --- |
| 操作系统 | Ubuntu 24.04.4 | 管理员提供 |
| 架构 | x86_64（linux/amd64） | `lscpu` |
| CPU | 48 逻辑核 | `lscpu` |
| 内存 | 125 GiB，无 swap | `free -h` |
| 根分区 | LVM 3.5 TB，已用 565 GB，可用 2.8 TB | `df -h` |
| GPU | 1 张 NVIDIA L20，显存 46068 MiB（标称 48 GB） | `nvidia-smi` |
| NVIDIA 驱动 / CUDA | 595.84 / 13.2 | `nvidia-smi` |
| Docker / Compose | 29.4.3 / v5.1.3 | `docker --version`、`docker compose version` |
| NVIDIA Container Toolkit | 1.19.0 | `nvidia-ctk --version` |
| 网络 | 医院内网，不能访问 GitHub、镜像仓库、PyPI 或 npm | 管理员提供 |
| 文件传入 | 人工离线传输；内网没有单文件大小限制 | 管理员提供 |

## 对发布的约束

- 更新包必须是 GPU 模式、linux/amd64。驱动 595.84 支持 CUDA 13.2，满足当前 `cu132` PyTorch；Compose v5.1.3 满足部署要求的 2.20+。
- 服务器不能联网，所有镜像、脚本和清单都由维护者在联网电脑取得后人工传入，再按 [离线升级流程](../DOCKER_DEPLOYMENT.md#医院内网可复用的离线升级流程) 执行。即使包发布在 GitHub Releases，也只是方便联网电脑下载，医院服务器仍按离线包安装。
- 根分区可用空间远大于一个 GPU 更新包加完整数据备份；升级器仍会按实际文件系统再次检查空间。

## 尚未记录

以下信息影响升级但尚未采集；需要时在服务器执行后补入上表；可记录部署路径，不记录密钥、密码和账号凭据：

- 原部署目录、当前运行模式和已安装版本：`bash /新包目录/update-offline.sh /原部署目录 --status`。
- 执行升级的账号是否就是原部署管理员账号。
