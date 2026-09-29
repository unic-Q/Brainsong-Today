# Brainsong Today

面向消费级脑机／脑电、耳机和教育应用的每日资讯简报。程序从公开信源及智谱官方联网搜索获取候选，核对日期和业务相关性，生成附原文链接的短简报，并可发送至飞书群。

本仓库基于 [Omni Info Radar](https://github.com/datawhalechina/omni-info-radar) 定制；原项目版权与 MIT 许可见 [LICENSE](LICENSE)。桌面版 Brainsong Today 与本仓库的云端简报程序是两套不同的入口。

## 当前推送规则

- 正式简报每天最多 7 条；每方向最多 3 条，学术最多 2 条，同一企业最多 2 条。不足时少发，不用无关内容凑数。
- 正式候选只接受最近 3 个自然日内有可靠发布日期的内容。旧候选和无日期副本从候选库清除，已发送指纹保留用于跨天去重。
- 付费分析前先做关键词、业务场景和日期初筛；同一信源占基础分析名额超过 25% 后暂缓，优先检查其他信源。该限制是软上限：其他信源不足时仍可继续检查。
- 先按相关性和时效选候选，再按相关性与来源权威性排序。基础分析名额为 36 条；若还不足 7 条，就继续检查未处理的有日期候选，直到选满或可检查候选用尽。
- 政策仅关注中国国内相关政策。指定行业／资本信源中首次发现、正文也无日期的文章可标作「近期」；它不是文章发布日期。
- 没有合格内容时不发送空简报；失败和采集问题写入 `logs/`，不会伪装成发送成功。

搜索接口的时间档位是“近一周”，程序再用文章发布日期做近 3 天的严格筛选；搜索结果缺日期不会被当作当天新闻。

## 本地运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --frozen
uv run brainsong-today --offline   # 无网络、无模型费用的样例
uv run brainsong-today             # 真实采集并生成预览，不发飞书
uv run brainsong-today --send      # 明确发送到飞书
```

真实采集和摘要使用官方智谱接口，可能产生费用。设置环境变量 `ZHIPU_API_KEY`；发送时还需 `FEISHU_WEBHOOK`。密钥和 Webhook 不要写进代码、日志或提交到 Git。

主要配置位于 [`config/brainsong.yaml`](config/brainsong.yaml)、[`config/keywords.yaml`](config/keywords.yaml) 和 [`config/sources.yaml`](config/sources.yaml)。详细设置与运行方式见 [使用说明](使用说明-Brainsong%20Today.md)。

## GitHub Actions

工作流见 [`.github/workflows/daily.yml`](.github/workflows/daily.yml)。在仓库的 **Settings → Secrets and variables → Actions** 中设置：

| 类型 | 名称 | 用途 |
| :-- | :-- | :-- |
| Secret | `ZHIPU_API_KEY` | 智谱官方搜索与 AI 分析 |
| Secret | `FEISHU_WEBHOOK` | 飞书群自定义机器人地址 |
| Variable | `BRAINSONG_FORMAL_READY=true` | 开启正式定时任务 |

工作流在北京时间早上 6:00—12:00 的时段尝试运行；GitHub 实际启动时间可能延后。同一天成功发送一次后，后续补偿运行会跳过。可以在 Actions 页面手动运行并选择仅预览或发送。历史候选、已发送记录、报告与故障日志通过 Actions artifacts 保存；公开仓库的构建产物也可能被他人访问，不要在其中存放敏感资料。

需要立即清理已有云端候选时，手动运行工作流并只勾选 `prune_only`；它会保存清理后的状态，不会搜索或发送，也不会重置已发送历史。

## 目录

| 路径 | 内容 |
| :-- | :-- |
| `src/brainsong/` | 云端采集、候选筛选、排序、摘要和飞书推送 |
| `config/` | 权重、关键词及信源配置 |
| `tests/` | 离线回归测试 |
| `.github/workflows/daily.yml` | 定时任务与手动运行入口 |
| `src/repo_courier/` | 上游 Omni Info Radar 的模块及复用组件 |

修改请通过 Pull Request 提交；`main` 已设置分支保护。运行前可执行 `uv run pytest -q`。
