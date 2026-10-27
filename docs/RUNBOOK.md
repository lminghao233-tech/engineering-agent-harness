# 运行手册

## 本机启动与停止

按 README 安装 Python3.12 与锁定依赖，执行 `python scripts/start.py`。该进程管理 API 与独立 worker，默认绑定127.0.0.1:8787。访问令牌在 `var/access-token`；不得提交 Git。Ctrl+C 停止。单独服务命令：

```bash
python -m uvicorn harness.api:app --host 127.0.0.1 --port 8787 --limit-concurrency 64
python -m harness.worker
python -m harness.worker --once
```

单独启动前要在 shell 中加载 `.env`，或用服务管理器的 EnvironmentFile；Python 不会自动加载 `.env`，只有 `scripts/start.py` 负责加载。`GET /health` 是进程/数据库检查；认证后的 `/api/system` 列出执行配置阻塞。预检 Docker daemon 和镜像发生在 worker 的任务准备阶段。

## Linux 部署

要求专用、补丁更新的 Linux Docker 主机，Python3.12、足够磁盘和内存。官方 SWE-bench 的一手文档建议 x86_64、约120GB空闲磁盘、16GB内存、8CPU；此数值是官方容量参考，不是本平台低配运行承诺。首轮并发固定1，先运行一条开发任务测量容量。ARM 官方支持状态需要按当前指南核对。

1. 将源码放置 `/srv/engineering-agent-harness`，建立受限制的 `harness` 系统账户，创建 `.venv` 并安装 requirements.lock。下载数据使用独立可信准备环境的 requirements-dev.lock。数据库必须在本地磁盘，禁止 NFS。
2. 创建 `.env`，权限600；设置长随机 HARNESS_API_TOKEN。服务目录与var仅可信服务账户可写，evaluator-only仅可信评测账户可读。单团队访问令牌是首版认证边界。
3. 操作者在可信准备阶段构建依赖镜像。`deploy/Dockerfile.sandbox` 仅提供 pytest 基础模板，真实仓库需要按基线锁文件安装其依赖；禁止把仓库未经审核的 setup/build 脚本在宿主执行。构建阶段也应使用隔离主机，镜像不得含任何密钥。
4. 发布到操作者有权限的镜像仓库，取得不可变 RepoDigest，填入 `HARNESS_IMAGE=registry/image@sha256:...`。运行时不会 pull/build 镜像。基础镜像也要在发布构建中通过 `PYTHON_IMAGE` 指定 digest；仓库模板中的 tag 是构建起点，不是已验证发布锁。
5. API 可以直接以 systemd 运行，或 `docker compose -f deploy/compose.yaml up -d --build`。先创建宿主var并让UID10001与worker账户通过适当本地组/ACL读写同一目录。API容器不持有Docker socket；trusted worker通过宿主systemd运行，见 `deploy/harness-worker.service`，按实际路径与UID调整。
6. 模型key只给worker。Compose的env_file会读到相同配置；更严格部署请把API环境文件拆为仅访问令牌和数据目录，worker持有模型配置。API系统检查在这种模式下会显示缺少模型配置，应以worker环境事件为准；这项就绪检查尚未汇聚跨进程健康。
7. 远程访问通过团队管理的TLS反向代理，保持Bearer认证，配置请求体256KB、连接/速率限制、访问审计。不要直接公开8787，不额外开放Docker daemon或数据库。
8. 按验收清单完成真实模型、容器、备份恢复和官方评测验证，再由操作者批准上线。

Docker group可控制宿主，trusted broker与服务代码必须按高信任级别管理。任务进程只获得源码只读挂载，不获得模型key、数据库、快照、评测答案或Docker socket。

## 资源与容量

每次容器CPU默认2、内存/交换总上限2GiB、PID64、nofile1024、单文件64MiB、/tmp64MiB、/work256MiB、输出128KB、工具默认60秒、全局容器并发1。模型每次输出最多2048 tokens；输入按保守字节上限控制，任务模型次数/累计预留token/运行秒/工具秒独立计数。事件单条截断，累计事件正文8MB后保留摘要。每个源码目录默认256MiB、文件数量40000；快照保留最近两份完整内容，历史元数据保留。

**存储边界：**256MiB为单源码目录及容器工作盘上限；平台在准备、写入、快照、验证和产物保存前按实际磁盘占用进行准入检查，默认每任务为源码上限的6倍、平台总计4096MiB，并保留64MiB空闲空间。超额进入DISK_BUDGET阻塞。该应用层检查不能替代文件系统硬配额，数据库增长与并发外部写入仍需部署配额及监控；历史归档没有自动删除策略。

## 取消与恢复

取消写入数据库，活动工具循环终止命名容器并保存已有证据。模型HTTP请求可能等待到60秒超时，预留预算保留。硬杀worker后，最多一个租约窗口（默认90秒）内由下一次claim将任务标为BLOCKED；操作者在页面恢复。恢复前worker会清理该任务label的遗留容器。尚未建成检查点的配置失败任务执行重新准备，不称为恢复进程内存。

剩余预算不足时不能通过resume续命；创建新实验run并明确重复策略。未知文件哈希冲突不自动重放，保留目录和操作日志，由操作者核对。基线实验组禁用恢复。

## 备份与恢复

停止worker，等待或核对遗留租约，再停止API写入。执行：

```bash
python scripts/backup.py /srv/backups/harness-20261027
```

脚本拒绝活动owner，用SQLite backup API生成一致数据库，并复制tasks/official。恢复时停止服务，把原var移至独立备份位置，将备份复制到新的var，调整权限，执行SQLite `PRAGMA integrity_check`，抽查产物哈希，再启动API与worker。路径必须人工核对，禁止覆盖尚未保存的现有数据。数据库和产物一起恢复，密钥通过独立秘密管理渠道恢复。本次未在Linux生产环境执行完整恢复演练。

## 排障

| 现象 | 处理 |
|---|---|
| 401 | 检查令牌长度、环境和标签页会话；不是模型密钥 |
| CONFIGURATION | 填写模型、付费授权和digest镜像，安装Linux Docker |
| ENVIRONMENT | 检查docker info、预拉取镜像、/opt/venv及依赖、只读权限和配额 |
| MODEL_RATE_LIMIT / TIMEOUT | 增强组最多额外重试2次；所有尝试消耗预算，检查供应商记录 |
| TEST_ASSERTION | 读取独立验证artifact；不能等同基础设施失败 |
| EMPTY_PATCH | 模型没有提交变更，保留为失败，不替换评测样本 |
| lease expired | 确认旧worker结束，恢复并核对操作哈希 |
| patch不完整/测试文件修改 | 审阅受保护路径与工作区，禁止绕过保护评分 |
