# 实际交付验收记录

日期：2026-10-27（Asia/Shanghai）。当前版本为可运行的工程交付版本，**尚未达到可宣称生产上线验收通过的状态**。

| 项目 | 实际状态 | 证据 / 限制 |
|---|---|---|
| 源码、依赖版本锁、v1数据库迁移 | 已交付 | requirements.lock、harness/migrations |
| API、控制台、独立worker本机运行 | 已执行 | 127.0.0.1:8787；真实进程及HTTP请求 |
| 四页联通、认证、任务导入/取消/重新排队 | 已操作验证 | evidence/browser-qa.json、截图 |
| 工程测试 | 42通过、1跳过（最终报告以tests.xml为准） | Windows符号链接创建权限不足，Linux需补测 |
| 实际进程死亡后的写入核对与恢复 | 已通过 | 子进程os._exit(91)，tests/test_reliability.py与fault报告 |
| 七类固定故障注入 | 7/7通过，n=7固定工程场景 | evidence/fault-injection.json；不是模型效果评测 |
| 完整Agent循环、补丁、干净目录验证、等待审核 | 确定性夹具通过 | evidence/engineering-fixture；real_model=false，容器替身不代表Docker验收 |
| Lite公开数据下载与固定清单 | 已执行 | revision b0dde1093fe417d83b7184254edf8199c1f0dff5；dev23/test300；冻结test30/dev3 |
| 真实公开issue任务尝试 | 实际导入，配置预检阻塞 | marshmallow-code__marshmallow-1343；模型调用0；evidence/runtime |
| 30条baseline/enhanced实验 | 已入队60任务，模型均未运行 | 缺少模型与Linux Docker；比例null，不声称模型成绩 |
| 官方SWE-bench验证 | 仅运行入口预检，未执行评测 | evidence/official-preflight.json；无Docker、无模型补丁、无评测环境 |
| 真实模型服务 | 适配器实现与HTTP替身测试通过；真实调用未验收 | 无API凭证和付费授权，不能使用当前聊天充当平台API |
| Linux Docker资源/安全边界 | 启动参数测试通过；实际容器未验收 | 本机Docker/WSL不存在；需真实Linux执行验证 |
| 部署配置、备份工具、五分钟演示与交接 | 已交付 | deploy、docs；Linux部署/灾备演练待执行 |
| 许可证 | 来源与已获取文件记录；仍需审核 | 数据卡许可未声明，部分上游许可需人工核对；不宣称全材料MIT |
| GitHub私有仓库 | 已创建；最终同步以远端提交核对为准 | https://github.com/lminghao233-tech/engineering-agent-harness |
| 团队试点/生产使用/收益 | 未执行 | 不填写生产使用量、改进比例或企业背书 |

## 上线前仍需完成

1. 配置真实模型endpoint/key/模型和有限预算，在专用Linux Docker主机跑至少1条dev真实任务，保留成功或失败全部证据。
2. 为选中仓库准备并固定依赖镜像digest；真实验证网络关闭、只读挂载、权限、CPU、内存、PID、磁盘、超时、取消与遗留容器清理。当前测试只能证明命令构造，不能证明内核约束已生效。
3. 安装固定官方评测器及Linux传递依赖锁，对真实模型补丁执行官方评测；执行冻结30条双组，或继续准确保留未运行状态。
4. 在Linux执行符号链接越界测试、实际容器中断恢复测试、备份恢复演练和端到端资源测量。
5. 设置var独立磁盘配额、保留策略、TLS、团队令牌管理、监控与告警。应用层已加入累计磁盘准入检查；文件系统硬配额、监控和历史归档自动保留策略仍需部署补齐。
6. 完成数据和第三方许可审核、代码安全审查和团队试点，再批准部署。当前为单团队共享令牌，无个人强身份审计。

这些未完成项是资源和实现验收边界，不是可以用夹具成功或当前聊天模型自行评分替代的证据。
