#!/bin/zsh
# 凌晨运行基准试点集：等队列空闲 → 停 agent → 跑 → 无论如何都恢复 agent。由 launchd（com.huatuo.bench-nightly）在 0:00 触发，也可手动运行。
# 结果在 data/bench/results/nightly-<日期>.jsonl，汇总在同名 .summary.txt。日志在 data/bench/nightly.log。
set -u
REPO=${0:A:h:h:h}; cd $REPO
LABEL=nightly-$(date +%F)
LOG=data/bench/nightly.log; mkdir -p data/bench/results
log() { echo "$(date '+%F %T') $*" >> $LOG; }
DEADLINE=$(date -v+7H +%s)      # 最多跑 7 小时（约 7:00 前结束），超时就中止，已跑完的用例保留，下次可续跑
IDLE_WAIT_UNTIL=$(date -v+3H +%s)  # 最多等 3 小时让队列空闲，仍有任务则放弃，留待下一晚
export PATH=/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
set -a; [ -f .env ] && source .env; set +a

busy() { [ "$(sqlite3 data/state.db "select count(*) from jobs where status in ('running','pending')")" != "0" ]; }
while busy; do
  [ $(date +%s) -gt $IDLE_WAIT_UNTIL ] && { log "队列一直不空闲，放弃本次"; exit 0; }
  sleep 60
done

restore() {
  for i in 1 2 3 4 5; do
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.huatuo.agent.plist 2>/dev/null && break
    sleep 8
  done
  launchctl list | grep -q com.huatuo.agent && log "agent 已恢复" || log "警告：agent 未能恢复，请手动 launchctl bootstrap"
}
trap restore EXIT

log "停止 agent，开始基准 $LABEL"
launchctl bootout gui/$(id -u)/com.huatuo.agent 2>/dev/null; sleep 8
zsh deploy/start-model.sh >> $LOG 2>&1 || { log "模型未就绪，放弃"; exit 1; }

caffeinate -i uv run python eval/bench/run.py $LABEL >> data/bench/$LABEL.out 2>&1 &
PID=$!
while kill -0 $PID 2>/dev/null; do
  [ $(date +%s) -gt $DEADLINE ] && { log "超时，中止基准"; pkill -P $PID; kill $PID; break; }
  sleep 30
done
wait $PID 2>/dev/null
uv run python eval/bench/summary.py $LABEL > data/bench/results/$LABEL.summary.txt 2>&1
log "基准结束：$(grep '^ALL' data/bench/results/$LABEL.summary.txt)"
# 只跑一次：成功跑完（没有超时）就注销这个定时任务
if [ -f data/bench/results/$LABEL.jsonl ] && [ "$(wc -l < data/bench/results/$LABEL.jsonl)" -ge "$(ls -d data/bench/*/meta.json | wc -l)" ]; then
  launchctl bootout gui/$(id -u)/com.huatuo.bench-nightly 2>/dev/null; rm -f ~/Library/LaunchAgents/com.huatuo.bench-nightly.plist; log "已注销定时任务"
fi
