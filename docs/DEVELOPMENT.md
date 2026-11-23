# 开发说明

## 组成

| 文件 | 责任 |
| --- | --- |
| `main.py` | AstrBot 事件、后台任务、命令、主动联系和认证面板 API |
| `cognition.py` | 身份分区、证据校验、模型协议、中文与英文检索 |
| `store.py` | SQLite 表、历史修订、好感账本、持久化任务与删除语义 |
| `memory.py` | 前台缓存、后台数据库调度与预先构建的检索词 |
| `interop.py` | 可选插件快照及 companion v1 空闲租约 |
| `_conf_schema.json` | AstrBot 原生配置页及可视化面板的共同配置定义 |
| `views/cognition/` | 无外部前端依赖的管理视图 |

## 数据流

1. 真人聊天 `on_llm_request`：从内存读取会话人格、相处印象和相关回忆，构成简短自然语言背景。把本轮信息投递到保存队列，不在此钩子读写数据库或调用整理模型。
2. `on_llm_response` / `on_agent_done`：记录完整回复。`after_message_sent` 投递完整经历；AstrBot 4.29 流式路径在 `on_agent_done` 入队。只使用已有 SDK 完成边界，不代表对方已读。
3. 保存任务将片段写入持久化队列，并刷新缓存。`Memory.call` 将所有运行期 SQLite 操作交给线程，串行执行；数据库慢时不会阻塞聊天事件循环。
4. 后台 worker 默认停聊45秒后，或累积6轮、最长等待10分钟时，合并同一会话人格、同一发言者和同一模型的最多12轮经历，调用一次 `context.llm_generate()`。按发言者分组是为了保留群聊归属；`turns` 保留每轮说话上下文。
5. 整理结果包含相处印象、共同经历、相处方式、未完话题和自我认识，以及值得保留的具体记忆。文字印象允许综合多轮表达；具体事实和关系仍保存来源摘录供核对。
6. 真人开始新一轮聊天时，暂停后台生成请求，本轮经历仍留在持久队列里。缓存可立即用于回复。客户端取消请求不能保证远端服务立即停止推理，独立后台 Provider 可进一步减少资源竞争。
7. 每15秒更新可选插件快照、检查主动联系。主动联系使用当前角色与记忆组织新措辞，也可返回 WAIT。自己的主动消息会保存在相处背景，后续能接住用户回复。

前台缓存只在后台成功保存后刷新，首次安装尚无回忆时不等待。更新插件时在初始化阶段预热已有记忆。关闭插件前等待当前保存队列完成，尚未整理的经历重启后继续。

## 事实与关系

记忆以 `(scope, subject, slot)` 作为可更新事实；新内容使旧版本失效，历史保留供核对。证据匹配只是来源检查，不构成语义真实性证明。

关系按 `(scope, source, target, label)` 独立保存，任意人物之间可叠加自由标签。模型返回 `labels` 增加或确认标签，`ended_labels` 只结束指定标签，其余保留；仍兼容旧的单个 `label`。每个标签带描述、来源发言者和状态，`relation_history` 保存变化。旧版单关系表自动迁移且保留现有边。

群聊的 @ 组件提供确定的成员账号对应；模型可使用已知人物 ID 连接实际成员，普通人名继续保留独立文字人物身份。前台按当前提到的人物优先召回相关连接，读取缓存，不新增前台模型或数据库调用。

真人 ID 来自传输层，昵称只是显示名。模型只能把 `$speaker` 指向当前真人；其他被提及的人物使用独立 named 身份，并要求原名出现在输入里。当前不提供跨会话自动合并身份。

单条事实删除会删除该事实的全部修订、当前分区的原始片段和待整理任务，同时清除可能包含该事实的文字印象；关系与好感仍保留。整个分区清空会删除所有上述数据。两种删除都会提高 generation，正在等待模型的旧任务无法再写回。联动快照另保存不含内容的 SHA-256 收据；删除后不重新导入相同快照，避免外部插件的旧快照复活记忆。

好感按一次回顾记录一次，取消每日配额，持续积累且没有时间衰减。信任仍可随经历变化；分数保留为后台相处状态，普通聊天提示使用文字印象。一次合并回顾完成时同时提交各片段的完成状态，重试不会重复积累。

## 运行测试

将仓库放到其自身命名的目录中，进入父目录运行：

```powershell
python -m pip install "astrbot>=4.29.4,<5" ruff -r astrbot_plugin_self_cognition/requirements.txt
python -m unittest discover -s astrbot_plugin_self_cognition/tests -v
python -m ruff check astrbot_plugin_self_cognition
node --check astrbot_plugin_self_cognition/views/cognition/app.js
```

测试使用真实 AstrBot SDK 类型和 Web 请求上下文，模型和平台发送通过测试替身隔离，**不会调用真实模型或联系真实用户**。覆盖多轮合并回顾、前台在慢数据库下响应、后台生成被新聊天打断、文字印象与旧数据迁移、重复处理、重启持久化、删除与后台竞态、分区隔离、JSON 校验、任务报告排除、主动提问许可、发送失败和 API 认证等。

安装到真实 AstrBot 后，建议人工验证一次：普通对话 → 等待整理队列归零 → 新消息召回；打开认知面板查看证据、保存配置；启用自己希望使用的可选联动。实际模型质量和平台适配器投递行为需要部署环境验证。

## 依据

- [AstrBot 插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [AstrBot 插件可视化视图与 bridge](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)
- [AstrBot Context 源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/context.py)

本次兼容性验证使用 PyPI AstrBot 4.29.4 的实际 SDK；上游 master 文档用于核对接口，不作为已运行完整 AstrBot 服务的声明。
