---
name: feishu-wiki-backup
description: 将飞书 Wiki 或其子树只读批量备份为不可覆盖的本地 Markdown 快照，默认将图片和白板预览以 Base64 Data URI 内嵌到 Markdown，并保留目录、快捷方式、revision、manifest 和验收报告；适用于先落 raw 再选择性 ingest，不用于把本地内容回写飞书。
---

# 飞书 Wiki Markdown 备份

复用飞书官方 `@larksuite/cli` 完成认证、Wiki 读取、Docx 转 Markdown 和媒体下载；不要另写 OpenAPI 客户端，也不要把文档正文交给 LLM 做搬运。

## 运行原则

- 只读飞书，不提供创建、覆盖、删除或同步回写能力。
- 每条命令显式使用 `--profile codex-bot`；个人知识空间默认 `--as user`。只有用户明确指定机器人身份时才用 `--as bot`，禁止静默切换身份。
- 先运行 `plan` 清点节点、快捷方式、唯一正文与不支持类型。批量执行前向用户报告数量和影响范围。
- `export` 只创建新的快照目录；目标已存在即停止，绝不覆盖旧 raw。
- 同一 `(obj_type, obj_token)` 的正文只抓取一次；shortcut 只保存路径关系和指向规范正文的本地链接。
- 默认使用 `--image-mode inline`：普通图片和白板预览在原出现位置写成兼容 Markdown 的 HTML `<img src="data:image/...;base64,...">`，Base64 在 HTML 属性内部按短行换行。不得使用文末引用定义（Obsidian 会把超长定义误显为正文），也不得留下单行超长的 Markdown／HTML 图片地址（大图可能让阅读视图空白）。成功快照不再保留这些图片的外部文件。
- 任何正文、媒体或 HTML5 侧车失败都必须进入 manifest 和验收报告；不得把部分成功称为完整备份。
- 第一版只完整导出 `docx`。Sheet、Bitable、Slides、Mindnote 等对象保留在清单并标为 unsupported，除非用户另行要求扩展。

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
  --image-mode inline `
  --limit 3
```

小样验收后，正式导出到 raw 并移除 `--limit`：

```powershell
python scripts/export_feishu_wiki.py export `
  --url "https://my.feishu.cn/wiki/<node_token>" `
  --output-dir "E:\Obsidian\模版仓库V5\raw" `
  --profile codex-bot `
  --identity user `
  --image-mode inline
```

可用 `--snapshot-id` 指定可读版本名；省略时使用当前时间。`--max-depth 0` 只处理根节点，`-1` 表示整棵子树。`--image-mode files` 可恢复为外部图片文件，仅用于目标阅读器不支持 Data URI 或文档大到不适合内嵌时。

内嵌白板的离线预览需要当前用户已授权 `board:whiteboard:node:read`；缺少该 scope 时脚本必须保持失败状态，不能把占位符当作已备份。

## 验收

完成后必须读取快照中的 `verify-report.md` 和 `manifest.jsonl`，至少核对：

- 清点节点数、唯一正文数、shortcut 数与飞书目录一致；
- `failed = 0`、`unsupported = 0` 才能称为完整备份；
- Markdown 中没有未处理的 `<img>`、`<source>`、`<whiteboard>` 或飞书 file URL；
- 飞书 `<callout>`、`<grid>/<column>`、`<title>`、嵌入块和同步引用必须转换为可移植 Markdown；正文区残留未支持的飞书布局标签时快照必须失败，不能把阅读器中的错版称为完成；
- `inline` 模式下，普通图片和白板预览应在图片原位置写为带 `data-feishu-embed-id` 的 HTML `img` Data URI，Base64 应换成短行，文末不得出现 Base64 引用定义；manifest 中状态为 `embedded` 且包含 `embed_id`、出现次数、MIME、原始字节数和 SHA-256；
- 内嵌前按字节签名审核 MIME，当前只接受 PNG、JPEG、GIF 和 WebP；扩展名与签名不符或不支持的图片必须使快照失败，禁止静默降级成外链。
- `inline` 模式下不应再出现与图片 token 对应的 `assets/media` 或 `assets/whiteboard` 文件；
- manifest 含 node/obj token、revision、源路径、抓取时间与 SHA-256；
- 已生成的快照没有被后续运行覆盖。

`--limit` 生成的是 `sample` 快照，只用于小样验收，不能作为完整知识库备份回执。

## 边界

- 遇到 `permission_denied`、无效 token 或缺 scope：停止对应节点，不要换身份碰运气。
- 遇到限流：保留 `.partial-*` 目录和错误清单，稍后重跑；不要高并发轰炸接口。
- Base64 通常会让图片数据膨胀约三分之一，并使 Markdown 不适合人工 diff；这是“单文件可转发”的明确交换。
- Markdown 阅读器可能出于安全策略屏蔽 `data:` URL。向特定平台转发前必须用该平台抽检；若被屏蔽，单个 Markdown 无法同时保证兼容性与内嵌图片，应改为单文件 HTML/PDF。
- 真正的 PDF、压缩包等附件仍保存为 `assets/attachments/` 文件；HTML5 交互块仍保存侧车。只有不含这两类资源的文档，才能称为完全单 `.md` 交付。
- 脚本不保存 App ID、App Secret、access token 或浏览器 Cookie。
- raw 快照只是后续 ingest 的来源层；不得在本流程中自动创建或修改 Wiki 知识页。
