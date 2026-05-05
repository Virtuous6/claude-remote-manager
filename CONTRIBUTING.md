# Contributing to cortextOS

Thanks for wanting to contribute! Here's how to do it.

## Quick Start (using gh CLI)

```bash
# 1. Fork and clone
gh repo fork grandamenium/claude-remote-manager --clone
cd claude-remote-manager

# 2. Create a branch
git checkout -b my-feature

# 3. Make your changes, then commit
git add . && git commit -m "describe your change"

# 4. Push to your fork
git push origin my-feature

# 5. Open a PR
gh pr create --repo grandamenium/claude-remote-manager
```

## Using Claude Code?

Claude Code uses `gh` under the hood. Just make sure you fork first:

```bash
gh repo fork grandamenium/claude-remote-manager --clone
```

Then work normally. When Claude creates a PR, it will target the upstream repo automatically.

## Guidelines

- Keep PRs small and focused — one change per PR
- Use descriptive commit messages
- Test your changes before submitting
- Bug fixes and new features are both welcome
- Open an issue first if you want to discuss a larger change

## What happens next

PRs are reviewed by maintainers. We'll review, give feedback if needed, and merge when ready. You cannot push directly to the main repo — all changes go through PRs.
