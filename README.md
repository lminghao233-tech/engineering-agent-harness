# Engineering Agent Harness

研发 Agent 可靠执行与评测平台。提供任务控制台、持久化执行、可核对恢复、受约束工具、独立验证与人工审核。

**交付状态：可运行的单团队工程版本；尚未通过生产上线验收。** 本机实际运行了 API、控制台与独立 worker。真实公开开发任务可以导入并保存运行记录；由于没有模型 API 凭证及 Linux Docker，真实模型修复与官方 SWE-bench 评测保持未验收。详见 [验收清单](docs/ACCEPTANCE.md)。确定性夹具结果独立存放，不冒充真实基准成绩。

## 快速启动

要求 Python 3.12、Git。Windows 可运行控制台和工程测试；执行公开仓库代码必须使用 Linux Docker worker。

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
python scripts/prepare_data.py
python scripts/start.py
```

打开 **http://127.0.0.1:8787**。访问令牌自动生成在 `var/access-token`，在控制台“连接工作区”中输入。也可以预先设置 `HARNESS_API_TOKEN`。Ctrl+C 停止 API 与 worker；未完成任务会在租约过期后进入可核对的阻塞状态。

本次开发环境使用 Codex 附带的 Python 3.12，依赖安装在工作区 `work/python312`，不要求用户修改系统 Python。`python scripts/start.py` 是其他机器上的标准启动方式。

## 开始真实任务执行

1. 在专用 Linux 主机部署可信 worker；按照 [运行手册](docs/RUNBOOK.md) 准备依赖镜像并固定 digest。
2. 复制 `.env.example` 为 `.env`，配置模型 HTTPS endpoint、模型标识、API key、访问令牌和镜像；明确授权模型调用预算后设置 `MODEL_CALLS_AUTHORIZED=true`。凭证不进入任务容器。
3. 下载固定 revision 的数据。优先从 Lite dev 集导入任务，确认可信测试路径和依赖镜像与仓库基线兼容。
4. 创建任务，观察时间线、预算、失败原因和检查点。Agent 完成后进入干净基线的独立验证；通过才进入 `AWAITING_REVIEW`。
5. 下载补丁和验证报告，记录本地人工审核。`APPROVED` 不会自动合并或发布。
6. 官方评测在独立可信 Linux 评测环境运行 `python scripts/official_eval.py TASK_ID --execute`，输出与补丁哈希绑定的唯一 run ID。

没有凭证、镜像或容器时，worker 会保存 `BLOCKED` 原因，模型调用数保持为 0，不执行仓库代码。默认不开启任何付费调用。

## 已实现

- FastAPI + SQLite WAL 模块化单体；独立 worker，无队列中间件；全局并发上限 1。
- 明确状态机、执行尝试、事件序号、租约及 fencing token、取消和总预算累计。
- 文件哈希前置条件、操作 ID、写入意图日志、原子替换、写后未确认操作核对。
- 两份最近完整工作区快照与历史检查点元数据；恢复时校验基线、快照和操作记录。
- 文件读取、字面检索、受控文件变更、可信测试、差异与补丁导出。
- Docker 无网络、非 root、只读源目录与根文件系统、受限 tmpfs、CPU/内存/PID/时长/输出限制；宿主仅处理文件字节，不执行仓库脚本。
- 真实供应商兼容的 Chat Completions HTTP 适配器、分类错误、有界重试、供应商 token 用量与保守预留分别记录；未配置价格时费用为不可用。
- 基线原始测试保护、干净目录独立验证、补丁哈希与产物完整性校验。
- 四个中文控制台页面，认证、筛选、事件轮询/SSE API、取消、恢复、下载、人工审核、双组实验与 JSON 导出。
- 固定数据 revision、30 条分层样本、3 条 dev 任务、来源/哈希/许可清单、agent-visible/evaluator-only 分离。
- 七类固定位置故障测试、完整执行循环夹具、CI、部署配置、备份工具和交接文档。

## 验证与证据

```bash
python -m pytest -q --junitxml=evidence/tests.xml
node --check harness/static/app.js
python scripts/fault_campaign.py
python scripts/export_evidence.py
curl http://127.0.0.1:8787/health
```

本机工程测试、故障注入和页面验证的实测状态见 [evidence](evidence/) 与 [验收清单](docs/ACCEPTANCE.md)。Windows 上符号链接权限不足时该测试显式跳过，Linux CI 应执行它。CI 配置已提供，远端运行结果需以 GitHub Actions 的实际记录为准。

## 数据与实验

固定清单：[data/manifest.json](data/manifest.json)。来源为 [SWE-bench Lite](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite)，本次下载 test 300、dev 23。30 条 test 样本按仓库排序、仓库内固定种子洗牌、轮询分层选择，种子 `20261027`；筛选与成功无关，不替换失败任务。3 条 dev 与 test 按实例和 issue 身份分离。hints 不启用。Verified 本次仅核验数据卡显示 500 条，未下载或运行。

评测中心创建 baseline/enhanced 两组，模型、初始提示词、工具、基线、隔离和预算相同；增强组启用恢复、分类重试与上下文管理。小样本、故障注入与官方判定分别报告；没有官方结果时使用 null，不填 0% 或成功日志。所有预选任务构成端到端比例的分母。公共基准可能已存在于模型预训练中，无法排除污染。

## 文档

- [架构、状态机与数据模型](docs/ARCHITECTURE.md)
- [API](docs/API.md)
- [部署、备份、恢复与排障](docs/RUNBOOK.md)
- [实验协议](docs/EXPERIMENTS.md)
- [五分钟演示](docs/DEMO.md)
- [技术总结与项目介绍](docs/PROJECT.md)
- [上线验收](docs/ACCEPTANCE.md)
- [安全边界](SECURITY.md)
- [第三方来源与许可](THIRD_PARTY_NOTICES.md)

本项目不代表任何消费电子企业的官方系统、合作关系或生产使用成果。第三方组件和公开基准保持独立归属，未宣称团队试点或效果提升。
