# 来源、归属与许可

平台执行协议和应用代码在本仓库实现。FastAPI、Starlette、Uvicorn、HTTPX、Pydantic、Pytest、PyArrow等为第三方组件，版本见锁文件，部署分发时保留各安装包LICENSE/NOTICE及其传递依赖许可。没有把第三方框架或官方评测器包装为自研。

| 材料 | 一手来源 | 本次使用与许可边界 |
|---|---|---|
| SWE-bench代码 | https://github.com/SWE-bench/SWE-bench | 官方评测器，requirements-evaluator.txt固定包版本；仓库MIT许可仅适用于其覆盖内容 |
| Lite数据 | https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite | 实际下载固定revision，来源、哈希和字段见manifest；本次数据卡未明确声明license，不从代码MIT推断全部数据许可 |
| Verified数据 | https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified | 只核验数据卡500条，未用于执行或成绩 |
| 官方评测指南 | https://www.swebench.com/SWE-bench/guides/evaluation/ | 容器、预测格式、缓存、结果口径参考 |
| 官方harness指南 | https://www.swebench.com/SWE-bench/reference/harness/ | 执行环境与资源容量参考 |
| mini-swe-agent | https://github.com/SWE-agent/mini-swe-agent | 单Agent参考设计，MIT；未复制其代码、未运行其基线成绩 |
| 任务上游仓库 | manifest各repo/base_commit | 分别下载代表性选中基线的许可证文件；不能从某仓库许可推断所有issue文本或基准材料许可 |

`data/licenses`保存成功获取的上游许可证原文。未找到许可的位置在manifest显式标为manual_license_review_required，需发布/商业再分发前处理。下载和本地研究使用不构成对数据再许可的声明。原始数据、agent-visible问题全文和evaluator-only答案默认不入Git；交付包通过来源脚本重新获取，不将全部第三方材料重新打包发布。

引用：Carlos E. Jimenez et al. **SWE-bench: Can Language Models Resolve Real-World GitHub Issues?** ICLR 2024. https://arxiv.org/abs/2310.06770 。
