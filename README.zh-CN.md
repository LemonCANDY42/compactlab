# compactlab

用你自己的 agent 会话历史做回放分析,按模型、按角色(主会话 / 子 agent)找出自动压缩(auto-compact)的最优阈值。默认完全离线。

**状态:v0.1,早期版本。** 成本曲线来自对你的历史按价格表回放;召回率部分是**假设代理**,不是任何模型的实测曲线。详见 `docs/METHODOLOGY.md`。

```bash
uv tool install .
compactlab demo                       # 用合成数据生成 compactlab-demo/report.html
compactlab scan                       # 扫描会话
compactlab simulate --out result.json # 扫描阈值并给出推荐
compactlab report --result result.json --out report.html
compactlab apply --result result.json --claude-settings ~/.claude/settings.json  # 默认只做 dry-run
```

- 支持 Claude Code 与 Codex;其他 harness(dsh、opencode、pi)只预留了适配器接口。
- 报告是单个自包含 HTML:挡位切换(Eco / Balanced / Recall / 自定义)、相对历史的预估节省、帕累托图、历史上下文分布、单个会话的真实曲线与回放曲线叠加。
- `apply` 默认 dry-run;写入必须 `--write` 加授权(预授权文件,或显式 `--i-authorize`)。每次写入都有备份和清单,`revert` 只还原被改动的键。
- Claude Code 写 `modelSettings.<model>.autoCompactWindow`(按模型);Codex 写顶层 `model_auto_compact_token_limit`(全局,无法按模型或角色区分)。
- AI 评估(`eval`)默认关闭,需要显式开关、会先打印预估费用,并有硬预算上限。
- 推送前运行 `python scripts/secrets_scan.py --all`。

MIT 许可。
