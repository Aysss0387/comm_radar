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
