# pre-commit hook（lint + format 护栏）

`pre-commit` 在 `git commit` 时对**本次暂存的 backend `.py` 文件**跑 `ruff check --fix` + `ruff format --check`，与 CI 的 `l1-semantic` job 对齐。本地只拦风格（不跑 pyright / 测试——那在 CI）。

## 安装（每个开发者 clone 后跑一次）

```bash
cp backend/scripts/git-hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

> 本仓库 `core.hooksPath` 已显式指向 `.git/hooks`，所以直接拷到 `.git/hooks/pre-commit` 即生效。
> 应急跳过：`git commit --no-verify`。

## 依赖

- `ruff`（homebrew：`/opt/homebrew/bin/ruff`；hook 用绝对路径调用，不依赖 backend venv）。
