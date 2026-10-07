#!/bin/zsh
# 凌晨运行基准试点集：等队列空闲 → 停 agent → 跑 → 无论如何都恢复 agent。由 launchd（com.huatuo.bench-nightly）在 0:00 触发，也可手动运行。
# 结果在 data/bench/results/nightly-<日期>.jsonl，汇总在同名 .summary.txt。日志在 data/bench/nightly.log。
set -u
REPO=${0:A:h:h:h}; cd $REPO
# 可选环境变量：BENCH_LABEL（结果文件前缀，默认 nightly）、BENCH_CASES（空格分隔的用例 id，默认跑全部）、
# BENCH_JOBS（空格分隔的多个评测，按顺序跑；每项是前缀，加 :fwd 表示负例模式，例如 "new-full neg:fwd"，设了就忽略 BENCH_LABEL）
LABEL=${BENCH_LABEL:-nightly}-$(date +%F)
LOG=data/bench/nightly.log; mkdir -p data/bench/results
log() { echo "$(date '+%F %T') $*" >> $LOG; }
DEADLINE=$(date -v+7H +%s)      # 最多跑 7 小时（约 7:00 前结束），超时就中止，已跑完的用例保留，下次可续跑
IDLE_WAIT_UNTIL=$(date -v+3H +%s)  # 最多等 3 小时让队列空闲，仍有任务则放弃，留待下一晚
export PATH=/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
set -a; [ -f .env ] && source .env; set +a

QUEUE_DB=${HUATUO_QUEUE_DB:-data/state.db}   # 线上队列的状态库；开发/生产分离后在生产目录里，由 plist 的 HUATUO_QUEUE_DB 指定
busy() { [ "$(sqlite3 $QUEUE_DB "select count(*) from jobs where status in ('running','pending')")" != "0" ]; }
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
  # 全部跑完后注销定时任务。必须放在恢复 agent 之后的最后一步：bootout 会连本脚本一起杀掉，之前放在前面导致 agent 一直没被恢复
  if [ -n "${ALL_DONE:-}" ]; then
    log "已注销定时任务"
    rm -f ~/Library/LaunchAgents/com.huatuo.bench-nightly.plist
    launchctl bootout gui/$(id -u)/com.huatuo.bench-nightly 2>/dev/null
  fi
}
trap restore EXIT

log "停止 agent，开始基准 $LABEL"
launchctl bootout gui/$(id -u)/com.huatuo.agent 2>/dev/null; sleep 8
zsh deploy/start-model.sh >> $LOG 2>&1 || { log "模型未就绪，放弃"; exit 1; }

ALL_DONE=1
for job in ${=BENCH_JOBS:-${BENCH_LABEL:-nightly}}; do
  name=${job%%:*}; mode=${job#*:}; [ "$mode" = "$job" ] && mode=""
  LABEL=$name-$(date +%F); FLAGS=(); [ "$mode" = fwd ] && FLAGS=(--forward)
  log "开始 $LABEL ${FLAGS[*]:-}"
  caffeinate -i uv run python eval/bench/run.py $LABEL ${FLAGS[@]} ${=BENCH_CASES:-} >> data/bench/$LABEL.out 2>&1 &
  PID=$!
  while kill -0 $PID 2>/dev/null; do
    [ $(date +%s) -gt $DEADLINE ] && { log "超时，中止基准"; pkill -P $PID; kill $PID; break; }
    sleep 30
  done
  wait $PID 2>/dev/null
  uv run python eval/bench/summary.py $LABEL > data/bench/results/$LABEL.summary.txt 2>&1
  log "基准结束 $LABEL：$(grep -m1 -E '^(ALL|负例)' data/bench/results/$LABEL.summary.txt)"
  # 只跑一次：全部评测都成功跑完（没有超时）才标记，由 EXIT 时的 restore 在恢复 agent 之后注销这个定时任务
  if [ -n "${BENCH_CASES:-}" ]; then WANT=${#${=BENCH_CASES}}; else WANT=$(ls -d data/bench/*/meta.json | wc -l); fi
  if [ ! -f data/bench/results/$LABEL.jsonl ] || [ "$(wc -l < data/bench/results/$LABEL.jsonl)" -lt "$WANT" ]; then ALL_DONE=""; fi
done
