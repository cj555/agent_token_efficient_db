# git hooks

一次性启用：

```bash
git config core.hooksPath scripts/hooks
```

`pre-commit` 会拦截：
- 任何 parquet/duckdb/npy 等数据文件（`tests/fixtures/` 白名单除外）
- 超过 5MB 的文件
- `.env` 凭据文件

这是 `.gitignore` 之外的第二道防线 —— 尤其是 private 实验仓库里误 `git add -f` 的时候。
