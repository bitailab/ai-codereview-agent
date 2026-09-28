#!/bin/zsh
# 安装两个开机自启服务：
#   com.aicr.model —— 登录时启动 LM Studio 并以 64K 上下文加载模型
#   com.aicr.agent —— 常驻轮询 GitLab 并做 CR（模型未就绪时会等待）
# 模型启动脚本复制到 ~/.local/share/ai-cr：launchd 下的 zsh 没有读取 ~/Documents 的权限。
set -e
cd "$(dirname "$0")"
REPO=$(cd .. && pwd)
UV=$(command -v uv)
# 服务进程的 PATH：uv、go、golangci-lint 所在目录 + 系统默认目录
SVC_PATH=$(for t in uv go golangci-lint; do p=$(command -v $t) && dirname $p; done | awk '!s[$0]++' | paste -sd: -):/usr/bin:/bin:/usr/sbin:/sbin
DEST=~/.local/share/ai-cr
AGENTS=~/Library/LaunchAgents
mkdir -p $DEST $AGENTS ../data
cp start-model.sh $DEST/ && chmod +x $DEST/start-model.sh
for name in com.aicr.model com.aicr.agent; do
  launchctl bootout gui/$(id -u)/$name 2>/dev/null || true
  for i in {1..20}; do launchctl print gui/$(id -u)/$name >/dev/null 2>&1 || break; sleep 0.5; done
  # plist 为模板：替换为本机的仓库路径、uv 路径、数据目录和 PATH
  sed -e "s#__REPO__#$REPO#g" -e "s#__SHARE__#$DEST#g" -e "s#__UV__#$UV#g" -e "s#__PATH__#$SVC_PATH#g" $name.plist > $AGENTS/$name.plist
  launchctl bootstrap gui/$(id -u) $AGENTS/$name.plist
  launchctl kickstart gui/$(id -u)/$name
  echo "已安装并启动 $name"
done
