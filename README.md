# 传播学/计算传播论文雷达

轻量自动化系统，用公开 API 每周抓取最近四个月传播学、计算传播、反诈与人工智能传播相关论文元数据，输出 Markdown 周报与 CSV 累计论文库。

## 快速开始

```bash
cd comm-paper-radar
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export OPENALEX_API_KEY="your-openalex-key"
export CONTACT_EMAIL="you@example.com"
python -m comm_paper_radar run
```

产物：

- `reports/YYYY-WW.md`: 每周精选 10 篇，国内 2 篇、国外 8 篇。
- `data/papers.csv`: 累计论文库。
- `data/recommended.csv`: 历史推荐记录，自动避重。

## Zotero 日常科研工作台

论文雷达负责从公开来源发现候选论文；你确认后自行导入 Zotero 并放入一个明确的集合（例如 Scam、Framing）。Codex 通过 zotero-fulltext MCP 读取该集合的元数据与已索引全文，并把知识沉淀到本项目的 Markdown 文件中。Markdown 是唯一主版本；可选地把它渲染为同一 Zotero 条目下的 `Codex Research Card` 子 Note。

首次使用前：

1. 在 Zotero 的「设置 → 高级」开启允许本机应用访问，并保持 Zotero 运行。
2. 确认目标 PDF 已完成全文索引。
3. 在 Codex 中配置 MCP（只需一次）：

   ~~~bash
   codex mcp add zotero -- uvx zotero-fulltext
   ~~~

4. 重启 Codex 后，以一个命名集合为工作范围使用项目内技能：
   - zotero-paper-reader：精读论文，生成 research/cards/<citekey>.md。
   - zotero-research-atlas：从已验证的精读卡生成理论地图和方法银行。
   - zotero-idea-audit：基于集合证据做跨论文比较、citation finder 和候选 research gap / idea 审计。

### Better BibTeX 与 Zotero 子 Note

首次启用前，在 Zotero「设置 → Better BibTeX」把 **Active citation key formula** 设为：

~~~text
auth.lower + "-" + year + "-" + shorttitle(3,3)
~~~

这会让未来全库新增文献采用“作者-年份-短题”。已有 citekey 不会自动变化。`framing` 的已有标星论文必须先生成预览、人工检查后才可以迁移。citekey 迁移可能影响 LaTeX、Pandoc 或 Word 文稿中的已有引用键。

~~~bash
# 只读：生成标星论文、现有卡片与预估新 citekey 的对应表
.venv/bin/python -m comm_paper_radar.research_workspace citekey-preview \
  --collection "framing"

# 先检查 research/zotero-sync/framing-citekey-preview.json，确认后才执行。
# 它会调用 Better BibTeX 重新生成 citekey、迁移卡片文件名和重建地图。
.venv/bin/python -m comm_paper_radar.research_workspace migrate-citekeys \
  --preview research/zotero-sync/framing-citekey-preview.json --confirm
~~~

`planned_citekey` 是按上述公式的可读预估；Better BibTeX 会在实际迁移时处理转写和重名后缀，并以其返回的值为最终 citekey。

把卡片显示在 Zotero 条目下需要 **Zotero 10 或更高版本**。先完成升级、保持 Zotero 运行，并在第一次 `--apply` 时接受 Zotero 的本地 API 授权提示。默认命令只预览，不会写入任何 Zotero Note：

~~~bash
# 预览将创建/更新哪些子 Note
.venv/bin/python -m comm_paper_radar.research_workspace sync-zotero-notes \
  --collection "framing"

# 实际同步。只管理带 Codex 标记的子 Note，不改手写 Note。
.venv/bin/python -m comm_paper_radar.research_workspace sync-zotero-notes \
  --collection "framing" --apply
~~~

同步登记保存在 `research/zotero-sync/registry.json`，仅记录条目、Note、内容哈希和同步时间，不保存 API 密钥。Zotero 子 Note 是 Markdown 的显示副本，在 Zotero 中直接修改会在下次同步时被覆盖。

对应的本地命令可用于创建模板、校验卡片和生成地图：

~~~bash
# 为一篇论文创建结构化精读卡；已有 My Thoughts 会被保留
.venv/bin/python -m comm_paper_radar.research_workspace card-template \
  --citekey "smith-2024-PaperTitle" --title "Paper title" --collection "Framing" \
  --zotero-item-key "ABCD1234"

# 每张精读卡完成后校验；有问题会返回非零状态码
.venv/bin/python -m comm_paper_radar.research_workspace validate-card research/cards/Smith2024.md

# 从同一集合的已验证精读卡生成理论地图和方法银行
.venv/bin/python -m comm_paper_radar.research_workspace build-atlas --collection "Framing"
~~~

工作顺序：

~~~text
论文雷达发现候选
→ 你筛选并导入 Zotero 某集合
→ Codex 生成精读卡
→ 生成理论地图和方法银行
→ 需要综述、比较、选题或查引文时生成证据报告
~~~

所有地图与 idea audit 都只代表指定 Zotero 集合中已验证的证据；集合里没有检索到的研究只能称为“候选 gap”，不能断言为领域空白。

### 本地科研网页

网页以 Zotero 为主：从集合中选择论文及已索引附件，生成带全文证据编号的精读草稿；确认“采用”后才写入 Markdown 正式卡片。每条证据可展开对应全文快照。选择 2–4 张已采用卡片可做并排比较，并可查看理论 × 方法矩阵。个人想法应写在 Zotero 中独立的手写 Note，避免与生成的精读卡冲突。

启动：

~~~bash
.venv/bin/python -m comm_paper_radar.research_web --base-dir .
~~~

然后在浏览器打开 `http://127.0.0.1:8765/`。若该端口被占用，可以使用 `--port 8876`。网页的 AI 任务只发送本次选择的附件全文，不发送 Zotero 手写 Note；Key 只保留在当前后端进程内存中。采用正式卡片后可在网页点击“同步到 Zotero”。自动同步需要 Zotero 10+ 并在首次写入时接受授权；Zotero 9 会在界面中提示升级要求。

## 手动检索

```bash
python -m comm_paper_radar search --keywords "AI misinformation" --from 2026-02-01 --to 2026-06-17
python -m comm_paper_radar search --journal "New Media & Society" --limit 30
python -m comm_paper_radar search --profile anti_fraud --region domestic --report reports/manual/anti-fraud-cn.md
python -m comm_paper_radar search --profile ai_communication --keywords "deepfake trust" --top 20
```

手动检索默认写入 `data/papers.csv`，但不会写入 `data/recommended.csv`；需要标记为已推荐时加 `--mark-recommended`。如需包含历史已推荐论文，加 `--include-seen`。

## GitHub Actions

`.github/workflows/weekly-paper-radar.yml` 每周一北京时间上午自动运行，也支持手动触发专题报告。

需要配置 Secrets：

- `OPENALEX_API_KEY`: 必需。OpenAlex API key。
- `CONTACT_EMAIL`: 必需。用于 OpenAlex/Crossref polite pool，并作为周报收件地址。
- `SEMANTIC_SCHOLAR_API_KEY`: 可选。提高 Semantic Scholar 限额。
- `RESEND_API_KEY`: 必需。Resend 邮件发送 API key。
- `RESEND_FROM_EMAIL`: 必需。测试阶段可使用 `Paper Radar <onboarding@resend.dev>`。

在 Actions 页面手动运行 workflow 并选择 `email-test`，可以只发送测试邮件，
不抓取论文或改动报告文件。

## 合规边界

本项目只抓取公开元数据、摘要、DOI、期刊页和 OA 链接；不模拟登录、不下载 PDF、不绕过访问控制。
