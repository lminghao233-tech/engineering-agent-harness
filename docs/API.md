# HTTP API

启动后 `/openapi.json` 提供实际 OpenAPI schema。除 `/health` 和静态控制台，所有 `/api/*` 请求需要 `Authorization: Bearer <HARNESS_API_TOKEN>`。任务 ID 为 UUID hex。422 表示输入格式错误，409 表示状态/完整性冲突，401 表示认证失败，404 表示记录不存在。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 存活与数据库探测，不表示 worker/模型已就绪 |
| GET | `/api/system` | 模型标识、配置阻塞项、镜像与边界，不返回模型密钥 |
| GET/POST | `/api/tasks` | 列表筛选 / 创建任务 |
| GET | `/api/tasks/{id}` | 任务、事件、尝试、检查点、产物与审核 |
| GET | `/api/tasks/{id}/events?after=N` | 增量事件 |
| GET | `/api/tasks/{id}/stream?after=N` | Bearer 认证的 SSE，120秒后重连 |
| POST | `/api/tasks/{id}/cancel` | 请求取消，工具轮询终止关联执行 |
| POST | `/api/tasks/{id}/resume` | 恢复/重新准备；不重置预算；基线实验组禁用 |
| POST | `/api/tasks/{id}/review` | 本地 APPROVED/REJECTED 人工审核 |
| GET | `/api/artifacts/{id}` | 校验哈希后下载补丁/报告/日志 |
| GET | `/api/catalog` | 下载后的 agent-visible 任务 |
| GET | `/api/manifest` | 固定来源、revision、样本和许可证清单 |
| GET/POST | `/api/experiments` | 比较组结果 / 原子创建两组任务 |
| GET | `/api/export` | 真实运行与实验 JSON 导出 |

任务输入：

```json
{
  "instance_id": "owner__repo-123",
  "repo": "owner/repo",
  "base_commit": "0123456789012345678901234567890123456789",
  "problem_statement": "Paste the actual issue description here.",
  "tests": ["tests/test_module.py"],
  "max_calls": 12,
  "max_tokens": 240000,
  "max_seconds": 1800,
  "max_tool_seconds": 600,
  "data_revision": "manual"
}
```

`max_tokens` 为跨全部尝试的保守预留上限；每次预留 context 上限+2048，实际供应商用量记录在 model_response 事件，不冒充计费结果。任何 gold/test patch、hints、评分脚本字段都会被拒绝。测试字段只接受相对路径与 selector，不接受 shell、绝对路径或 pytest 选项。此版本只支持 pytest 仓库，其他测试框架尚未接入。

审核输入：`{"decision":"APPROVED","reviewer":"engineer","note":"Reviewed diff and independent test evidence"}`。名称不是独立身份认证，令牌持有人均属于可信团队。

实验输入：`{"name":"frozen-30-v1","instance_ids":["..."],"max_calls":12,"max_tokens":240000}`。IDs 必须来自冻结 test 清单，不允许重复；默认生成 baseline 和 enhanced 各一份任务。创建实验不会扩大服务端付费授权。
