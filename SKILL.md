---
name: feishu-wiki-backup
description: 将飞书 Wiki/Docx 或妙记完整逐字稿只读备份为不可覆盖的本地 Markdown。Wiki 保留目录，图片按 SHA-256 去重保存到共享 _assets 并使用短相对链接；妙记按会议年月/日期归档，校验原文哈希并支持恢复去重。不回写飞书。
---

# 飞书 Wiki Markdown 备份

复用飞书官方 `@larksuite/cli` 完成认证、Wiki 读取、Docx 转 Markdown 和媒体下载；不要另写 OpenAPI 客户端，也不要把文档正文交给 LLM 做搬运。

## 运行原则

- 只读飞书，不提供创建、覆盖、删除或同步回写能力。
- 每条命令显式使用 `--profile codex-bot`；个人知识空间默认 `--as user`。只有用户明确指定机器人身份时才用 `--as bot`，禁止静默切换身份。
- 先运行 `plan` 清点节点、快捷方式、唯一正文与不支持类型。批量执行前向用户报告数量和影响范围。
- `export` 只创建新的快照目录；目标已存在即停止，绝不覆盖旧 raw。
- 同一 `(obj_type, obj_token)` 的正文只抓取一次；shortcut 只保存路径关系和指向规范正文的本地链接。
- 默认使用 `--image-mode dedup`：普通图片和白板预览按原始字节计算 SHA-256，统一保存为快照根目录 `_assets/<完整哈希>.<ext>`。同一图片跨文档、跨 token 只写一份；Markdown 在原位置只保留到该文件的短相对链接。不得把 Base64 Data URI 当作默认交付格式。
- `_assets` 是快照内部的低干扰附件目录，不依赖 Windows 隐藏属性；复制、压缩或同步时必须和 Markdown 一起移动。真正的附件和 HTML5 交互资源仍按原目录保存。
- `--image-mode inline` 仅用于兼容旧快照，不推荐继续生产。旧版内嵌 Markdown 需要迁移时，使用 `scripts/externalize_markdown_images.py` 生成新的不可覆盖派生快照；不得原地改写 raw。`scripts/render_obsidian_portable.py` 只保留为旧版单文件 Data URI 的兼容工具，不是默认路线。
- 任何正文、媒体或 HTML5 侧车失败都必须进入 manifest 和验收报告；不得把部分成功称为完整备份。
- Wiki 路径只完整导出 `docx`。Sheet、Bitable、Slides、Mindnote 等对象保留在清单并标为 unsupported。妙记走下方独立入口，不把 Minutes token 传给 Wiki 导出器。

## 妙记逐字稿归档入口

用户要求会议逐字稿时，使用 `scripts/export_feishu_minutes.py`，不要以 AI 总结、会议纪要或 Note 摘要代替。支持 Minutes Transcript，以及由 `note +detail.verbatim_doc_token` 明确关联的 Docx 完整逐字稿；两者都属于原始发言记录，和 AI 智能纪要不同。

**权限分开判断**：`/minutes/`、逐字稿 `/docx/`、智能纪要 `/docx/` 是不同资源。Minutes 返回无权限时，继续沿用 user 身份读取同场 `note_id` 的 `verbatim_doc_token`；有完整逐字稿权限就继续归档和卡片交付，不必等待 Minutes 授权。仅当所有明确关联的逐字稿入口均不可读时才进入等待。不要把“Minutes API 无权限”表述成“用户无权读取这场会议逐字稿”。

Docx 请求沿用下方字段（minute_token 保留会议关联的妙记标识），额外设置 `source_type: feishu_docx_transcript`、`note_id`、`doc_token`，`source_url` 改为真实逐字稿 Docx 链接。脚本实时核验 `verbatim_doc_token`，拒绝以 `note_doc_token` 的智能纪要代替。完整源 JSON、revision 和哈希存入 audit；唯一序列化转换是把用户 cite 标签转为其 `user-name` 显示文本，避免姓名在 Obsidian 中消失，所有发言原文保持不变。卡片可保留用户约定的“基于妙记”标签，但必须简短标注实际来源为关联逐字稿文档，不声称读到了无权限的 Minutes 对象。

上游按 `lark-meeting` 技能发现会议、验证用户实际参会、取得妙记链接和真实会议时间。只因可见或拥有妙记，不能认定用户参会。先写 UTF-8 request JSON，字段为：

```json
{
  "meeting_id": "已核验的长会议ID",
  "minute_token": "已核验的妙记token",
  "title": "会议标题",
  "meeting_time": "2026-09-20T09:00:00+08:00",
  "participants": ["已核验的参会人"],
  "source_url": "https://tenant.feishu.cn/minutes/已核验的妙记token",
  "participation_verified": true
}
```

执行顺序：先 `plan` 预检一场会议的路径，再用相同参数执行 `export`。

```powershell
python scripts/export_feishu_minutes.py plan `
  --request "request.json" `
  --output-root "E:\Obsidian\模版仓库V5\raw\24-中恒电气\01-会议逐字稿" `
  --audit-root "C:\Users\Administrator\.codex\automation-state\feishu-meeting-transcripts\receipts" `
  --profile codex-bot --identity user
```

`export` 复用本技能 `LarkCLI`，调用官方 `minutes +detail --transcript`。CLI 只写独立 staging，程序逐字节保留 UTF-8 原文正文（仅移除编码 BOM），加元数据后保存到 `YYYY-MM/YYYY-MM-DD/会议标题逐字稿.md`，日期转换为北京时间。同名不同会议追加稳定短哈希。正文不经模型搬运，不改写发言人、时间戳、换行或内容。

验收以此入口 JSON 回执为准（不套用 Wiki 的节点 manifest）：`status=archived` 或 `already_archived`、`verification=full_body_equal`、路径、源文本与文件 SHA-256。无权限、没有逐字稿文件、空文件或错误页均不产生 raw 占位文档。程序复用已验收回执，并能恢复“原文已发布、回执未写完”的中断；已有 raw 不覆盖、不删除，原文变更需单独审核。

上游自动化维护 waiting_permission / waiting_transcript / archived / sent 独立状态；本脚本不管理调度、不生成总结、不发送消息。权限等待无过期淘汰。应用关闭、关机、断网后的首次可执行轮次，先恢复断点、再补查停机时间段，不能将恢复时间直接写成成功检查水位。只有原文归档验收成功才能发送会后卡片，发送成功必须保存 message_id；已归档未发送的只补发送。不要声称每十分钟轮询等于开机瞬时触发。

## 命令

脚本位于 `scripts/export_feishu_wiki.py`，仅依赖 Python 3.9+ 标准库与官方 `lark-cli`。若 `lark-cli` 不在 PATH，会调用 `npx.cmd -y @larksuite/cli`（非 Windows 使用 `npx`）。飞书 Profile 固定为隔离账号 `codex-bot`，脚本会拒绝其他 Profile。

先清点：

```powershell
python scripts/export_feishu_wiki.py plan `
  --url "https://my.feishu.cn/wiki/<node_token>" `
  --profile codex-bot `
  --identity user
```

小样导出：

```powershell
python scripts/export_feishu_wiki.py export `
  --url "https://my.feishu.cn/wiki/<node_token>" `
  --output-dir "E:\Obsidian\模版仓库V5\output\04-其他\feishu-wiki-poc" `
  --profile codex-bot `
  --identity user `
  --image-mode dedup `
  --limit 3
```

小样验收后，正式导出到 raw 并移除 `--limit`：

```powershell
python scripts/export_feishu_wiki.py export `
  --url "https://my.feishu.cn/wiki/<node_token>" `
  --output-dir "E:\Obsidian\模版仓库V5\raw" `
  --profile codex-bot `
  --identity user `
  --image-mode dedup
```

可用 `--snapshot-id` 指定可读版本名；省略时使用当前时间。`--max-depth 0` 只处理根节点，`-1` 表示整棵子树。`--image-mode files` 保留按飞书 token 分目录的旧外部文件布局；`--image-mode inline` 仅用于复现旧版 Data URI 快照。

把已有内嵌版迁移成哈希附件版（源目录和输出目录不能互相包含，输出已存在时拒绝覆盖）：

```powershell
python scripts/externalize_markdown_images.py `
  --source-dir "E:\Obsidian\模版仓库V5\raw\原快照" `
  --output-dir "E:\Obsidian\模版仓库V5\raw\新附件版快照"
```

迁移器整批在临时目录中转换并校验，保留原快照中的 PDF、HTML5、附件和其他非 Markdown 侧车文件，全部通过后才发布；输出包含 `migration-report.json`，记录源文件哈希、输出哈希、图片引用数、唯一图片数、去重节省副本数和侧车文件哈希。

内嵌白板的离线预览需要当前用户已授权 `board:whiteboard:node:read`；缺少该 scope 时脚本必须保持失败状态，不能把占位符当作已备份。

## 验收

完成后必须读取快照中的 `verify-report.md` 和 `manifest.jsonl`，至少核对：

- 清点节点数、唯一正文数、shortcut 数与飞书目录一致；
- `failed = 0`、`unsupported = 0` 才能称为完整备份；
- Markdown 中没有未处理的 `<img>`、`<source>`、`<whiteboard>` 或飞书 file URL；
- 飞书 `<callout>`、`<grid>/<column>`、`<title>`、嵌入块和同步引用必须转换为可移植 Markdown；正文区残留未支持的飞书布局标签时快照必须失败，不能把阅读器中的错版称为完成；
- `dedup` 模式下正文不得出现 `data:image/`；普通图片和白板预览必须指向 `_assets/<64位 SHA-256>.<ext>`，所有相对链接均可解析；
- 图片按字节签名审核 MIME，当前只接受 PNG、JPEG、GIF 和 WebP；扩展名与签名不符或不支持的图片必须使快照失败，禁止静默降级成外链；
- `_assets` 中每个文件名哈希必须等于实际内容哈希；相同哈希只能有一个文件，manifest 的 `path`、`sha256`、MIME、原始字节数和出现次数必须一致；
- manifest 含 node/obj token、revision、源路径、抓取时间与 SHA-256；
- 已生成的快照没有被后续运行覆盖。

`--limit` 生成的是 `sample` 快照，只用于小样验收，不能作为完整知识库备份回执。

## 边界

- 遇到 `permission_denied`、无效 token 或缺 scope：停止对应节点，不要换身份碰运气。
- 遇到限流：保留 `.partial-*` 目录和错误清单，稍后重跑；不要高并发轰炸接口。
- Markdown 与 `_assets` 构成一个完整快照；转发时应发送整个目录或压缩包，不能只发送 `.md` 文件。
- 若必须交付真正的单文件，另行导出 HTML/PDF；不要再用超长 Base64 图片牺牲 Obsidian 的编辑和预览稳定性。
- 真正的 PDF、压缩包等附件仍保存为 `assets/attachments/` 文件；HTML5 交互块仍保存侧车。只有不含这两类资源的文档，才能称为完全单 `.md` 交付。
- 脚本不保存 App ID、App Secret、access token 或浏览器 Cookie。
- raw 快照只是后续 ingest 的来源层；不得在本流程中自动创建或修改 Wiki 知识页。
