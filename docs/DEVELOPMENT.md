# 开发说明

## 组成

| 文件 | 责任 |
| --- | --- |
| `main.py` | AstrBot 事件、后台任务、命令、主动联系和认证面板 API |
| `cognition.py` | 身份分区、证据校验、模型协议、中文与英文检索 |
| `life.py` | 完整经历、持续自我、挂念闭环、别称与关系视角的持久化 |
| `store.py` | SQLite 表、历史修订、好感账本、持久化任务与删除语义 |
| `memory.py` | 前台缓存、后台数据库调度与预先构建的检索词 |
| `interop.py` | 可选插件快照及 companion v1 空闲租约 |
| `_conf_schema.json` | AstrBot 原生配置页及可视化面板的共同配置定义 |
| `views/cognition/` | 无外部前端依赖的管理视图 |

## 数据流

1. 真人聊天 `on_llm_request`：从内存读取会话人格、相处印象和相关回忆，构成简短自然语言背景。把本轮信息投递到保存队列，不在此钩子读写数据库或调用整理模型。
2. `on_llm_response` / `on_agent_done`：记录完整回复。`after_message_sent` 投递完整经历；AstrBot 4.29 流式路径在 `on_agent_done` 入队。只使用已有 SDK 完成边界，不代表对方已读。
3. 保存任务将片段写入持久化队列，并刷新缓存。`Memory.call` 将所有运行期 SQLite 操作交给线程，串行执行；数据库慢时不会阻塞聊天事件循环。
4. 后台 worker 默认停聊45秒后，或累积6轮、最长等待10分钟时，合并同一会话人格和同一模型的最多12轮场景消息，调用一次 `context.llm_generate()`。`turns` 保留每轮 actor/name/source_kind，`speakers` 列出参与者，`people` 分别更新各人的相处印象，避免把多人经历拼到同一人身上。
5. 整理结果包含相处印象、共同经历、相处方式、未完话题和自我认识，以及值得保留的具体记忆。文字印象允许综合多轮表达；具体事实和关系仍保存来源摘录供核对，`experience` 保留完整经过，`character` 延续自己的价值、喜好、习惯、兴趣和近来想法。
6. 真人开始新一轮聊天且共用模型时，暂停后台生成请求，本轮经历仍留在持久队列里；后台明确使用不同 Provider 时允许继续。缓存可立即用于回复。客户端取消请求不能保证远端服务立即停止推理，独立后台 Provider 可进一步减少资源竞争。
7. 每15秒更新可选插件快照、检查主动联系。主动联系使用当前角色与记忆组织新措辞，也可返回 WAIT。自己的主动消息会保存在相处背景，后续能接住用户回复。intentions 支持 care/promise/share/play/explore/ask，按 pending→sent→offered→done/dropped 延续；没有意图时仍兼容旧问题。

前台缓存只在后台成功保存后刷新，首次安装尚无回忆时不等待。更新插件时在初始化阶段预热已有记忆。关闭插件前等待当前保存队列完成，尚未整理的经历重启后继续。

## 事实与关系

记忆以 `(scope, subject, slot)` 作为可更新事实；新内容使旧版本失效，历史保留供核对。证据匹配只是来源检查，不构成语义真实性证明。

关系按 `(scope, source, target, label)` 独立保存，任意人物之间可叠加自由标签。模型返回 `labels` 增加或确认标签，`ended_labels` 只结束指定标签，其余保留；仍兼容旧的单个 `label`。每个标签带描述、来源发言者和状态，`relation_history` 保存变化，`relation_opinions` 分别保留不同发言者的看法。旧版单关系表自动迁移且保留现有边。

群聊的 @ 组件提供确定的成员账号对应；模型可使用已知人物 ID 连接实际成员，普通人名继续保留独立文字人物身份。前台按当前提到的人物优先召回相关连接，读取缓存，不新增前台模型或数据库调用。

真人 ID 来自传输层，昵称只是显示名。模型把 `$speaker` 指向场景首位发言者，多人时使用真实 speakers ID；其他被提及的人物使用独立 named 身份。明确对应的人物别称可保存为 alias，歧义别称不会自动合并身份。当前不提供跨会话自动合并身份。

单条事实删除会删除该事实的全部修订、当前分区的原始片段和待整理任务，同时清除可能包含该事实的文字印象；关系与好感仍保留，同时清除可能含该事实的经历、自我摘要和挂念。整个分区清空会删除所有上述数据。两种删除都会提高 generation，正在等待模型的旧任务无法再写回。联动快照另保存不含内容的 SHA-256 收据；删除后不重新导入相同快照，避免外部插件的旧快照复活记忆。

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

## 可选 companion 联动兼容层

`CompanionLink.connect()` 每15秒检查已安装且双方启用联动的插件。对于当前已验证的 v1 `context_for` 接口，其函数全局 `NAMES` 映射会暂时增加 self_cognition 项；复用协议自己的 peer、snapshot、publish 与会话白名单检查。不替换函数，不修改磁盘文件。只撤销由本实例添加的条目，支持重载、晚安装、单项关闭和卸载。未识别的协议降级到已有快照逻辑。

当前验证对象是本地四个配套插件的 v1 实现；CI 使用随测试保存的同协议样本作为无外部仓库依赖的验证。模型与消息发送仍使用替身，不代表已经在用户实际 AstrBot 服务上运行过完整聊天。

## 范围

此版实现完整经历、文字认识的逐渐沉淀、持续自我、挂念闭环与可选双向联动。跨会话身份合并、全局共享自我、向量召回和图片/语音经历提炼仍未实现。原始片段按保留期清理；提炼经历和好感不会按时间删除。共同经历、文字认识与事实是并行层次，不进行破坏性的自动压缩。
