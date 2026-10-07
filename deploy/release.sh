#!/bin/zsh
# 在生产目录发布指定版本（git tag）：等队列空闲 → 切到该 tag → 同步依赖 → 重启 agent 和 ui；起不来就回滚到上一个版本。
# 用法（在生产目录里执行）：deploy/release.sh <tag> [--force]
#   --force  不等队列空闲，直接重启（会打断正在跑的任务，任务会重新排队）
# 开发在另一个目录里改代码、提交、打 tag 并 push；生产目录只拉取，不手改。
set -eu
TAG=${1:?用法: deploy/release.sh <tag> [--force]}
FORCE=${2:-}
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd $REPO
UID_=$(id -u)
LOG=data/release.log; mkdir -p data
log() { echo "$(date '+%F %T') $*" | tee -a $LOG; }
die() { log "失败: $*"; exit 1; }
export PATH=/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[ -z "$(git status --porcelain --untracked-files=no)" ] || die "生产目录有未提交的改动，先处理掉（生产目录不应手改）"
git fetch -q --tags origin || die "git fetch 失败"
git rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null || die "tag $TAG 不存在（开发目录里打 tag 并 git push --tags 了吗？）"
PREV=$(git describe --tags --always)
[ "$PREV" = "$TAG" ] && { log "已经是 $TAG，无需发布"; exit 0; }

busy() { [ "$(sqlite3 data/state.db "select count(*) from jobs where status in ('running','pending')")" != "0" ]; }
if [ "$FORCE" != "--force" ]; then
  DEADLINE=$(( $(date +%s) + 10800 ))   # 最多等 3 小时
  while busy; do
    [ $(date +%s) -gt $DEADLINE ] && die "队列 3 小时内没有空闲，未发布（可加 --force）"
    sleep 30
  done
fi

restart() {
  for s in agent ui; do launchctl kickstart -k gui/$UID_/com.huatuo.$s; done
  sleep 20
  for s in agent ui; do
    pid=$(launchctl list | awk -v l=com.huatuo.$s '$3==l{print $1}')
    [ -n "$pid" ] && [ "$pid" != "-" ] || return 1
  done
}

log "发布 $PREV -> $TAG"
git checkout -q $TAG && uv sync --frozen -q && uv run python -c "import huatuo.cli" || die "切换到 $TAG 失败，当前可能停在半途，请手动检查 git status"
if restart; then
  log "已发布 $TAG（上一个版本 $PREV）"
else
  log "服务没有起来，回滚到 $PREV"
  git checkout -q $PREV && uv sync --frozen -q && restart && die "$TAG 启动失败，已回滚到 $PREV" || die "回滚后服务仍未起来，请手动检查 data/agent.log"
fi
