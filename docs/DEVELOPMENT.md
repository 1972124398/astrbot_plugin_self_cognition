# 开发说明

## 组成

| 文件 | 责任 |
| --- | --- |
| `main.py` | AstrBot 事件、后台任务、命令、主动联系和认证面板 API |
| `cognition.py` | 身份分区、证据校验、模型协议、中文与英文检索 |
| `store.py` | SQLite 表、历史修订、好感账本、持久化任务与删除语义 |
| `interop.py` | 可选插件快照及 companion v1 空闲租约 |
| `_conf_schema.json` | AstrBot 原生配置页及可视化面板的共同配置定义 |
| `views/cognition/` | 无外部前端依赖的管理视图 |

## 数据流

1. 真人聊天 `on_llm_request`：识别 UMO、人格与发送者，读取相关记忆，注入 `mark_as_temp()` 临时内容；不调用整理模型。
2. `on_llm_response` / `on_agent_done`：保存最终回复文本，忽略尚有工具调用的中间响应。
3. `after_message_sent`：持久化本轮片段与整理任务；同一事件只入队一次。AstrBot 4.29 流式发送路径不会经过这个钩子，因此流式模式在 `on_agent_done` 的完整响应边界入队。两者都不是对方已读回执，也无法提供所有适配器的物理送达保证。
4. 单个后台 worker：直接 `context.llm_generate()`，不执行工具、不再次触发聊天钩子。严格解析 JSON、核验非空原文摘录，再以单事务写入记忆、关系、好感与任务完成标记。
5. 定时器每分钟维护原始片段保留期、读取新活动快照，检查可选主动提问。发言受白名单、时区、静默时段、间隔、未回复状态、个人许可和共享租约约束。

所有 SQLite 操作在同一事件循环中同步完成，不跨 `await` 持有事务。数据量较大时可进一步迁移到专门数据库线程；当前检索的候选记录有明确上限。

## 事实与关系

记忆以 `(scope, subject, slot)` 作为可更新事实；新内容使旧版本失效，历史保留供核对。证据匹配只是来源检查，不构成语义真实性证明。角色关系按有方向的实体对更新，不自动推断反向边。

真人 ID 来自传输层，昵称只是显示名。模型只能把 `$speaker` 指向当前真人；其他被提及的人物使用独立 named 身份，并要求原名出现在输入里。当前不提供跨会话自动合并身份。

单条事实删除会删除该事实的全部修订、当前分区的原始片段和待整理任务；关系与好感仍保留。整个分区清空会删除所有上述数据。两种删除都会提高 generation，正在等待模型的旧任务无法再写回。联动快照另保存不含内容的 SHA-256 收据；删除后不重新导入相同快照，避免外部插件的旧快照复活记忆。

好感每个事务只记一次，范围校验后受 UTC 每日额度限制。负好感增量归零；信任单次变化限制在 -0.1..0.1，总范围0..1。不存在时间衰减作业。

## 运行测试

将仓库放到其自身命名的目录中，进入父目录运行：

```powershell
python -m pip install "astrbot>=4.29.4,<5" ruff -r astrbot_plugin_self_cognition/requirements.txt
python -m unittest discover -s astrbot_plugin_self_cognition/tests -v
python -m ruff check astrbot_plugin_self_cognition
node --check astrbot_plugin_self_cognition/views/cognition/app.js
```

测试使用真实 AstrBot SDK 类型和 Web 请求上下文，模型和平台发送通过测试替身隔离，**不会调用真实模型或联系真实用户**。覆盖重复处理、每日额度、重启持久化、删除与后台竞态、分区隔离、JSON 校验、任务报告排除、主动提问许可、发送失败和 API 认证等。

安装到真实 AstrBot 后，建议人工验证一次：普通对话 → 等待整理队列归零 → 新消息召回；打开认知面板查看证据、保存配置；启用自己希望使用的可选联动。实际模型质量和平台适配器投递行为需要部署环境验证。

## 依据

- [AstrBot 插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [AstrBot 插件可视化视图与 bridge](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)
- [AstrBot Context 源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/context.py)

本次兼容性验证使用 PyPI AstrBot 4.29.4 的实际 SDK；上游 master 文档用于核对接口，不作为已运行完整 AstrBot 服务的声明。
