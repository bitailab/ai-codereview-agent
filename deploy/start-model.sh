#!/bin/zsh
# 启动 LM Studio 服务并以 64K 上下文加载审查模型。由 launchd（com.aicr.model）在登录时执行，也可手动运行。
# 必须显式指定上下文长度，否则会使用默认的小上下文，内容会被静默截断。
set -u
LMS=~/.lmstudio/bin/lms
MODEL=qwen3-coder-30b-a3b-instruct
CTX=65536
log() { echo "$(date '+%F %T') $*"; }

# 同一时间只允许一个实例运行，避免另一个实例把正在加载中的模型误判为配置错误而卸载
zmodload zsh/system
LOCK=${TMPDIR:-/tmp}/aicr-start-model.lock
: >> $LOCK
if ! zsystem flock -t 900 $LOCK; then log "等待锁超时"; exit 1; fi

ready() {
  curl -s -m 5 http://127.0.0.1:1234/api/v0/models 2>/dev/null | python3 -c "
import json,sys
ms = json.load(sys.stdin)['data']
sys.exit(0 if any(m['id']=='$MODEL' and m.get('state')=='loaded' and (m.get('loaded_context_length') or 0) >= $CTX for m in ms) else 1)" 2>/dev/null
}

if ready; then log "模型已就绪"; exit 0; fi

# 后台启动 LM Studio（不抢焦点），等待 CLI 可用
open -gja "LM Studio"
for i in {1..60}; do $LMS ps >/dev/null 2>&1 && break; sleep 5; done

log "启动本地服务"
$LMS server start

# 若以错误的上下文加载过（例如被 JIT 自动加载），先卸载
if $LMS ps 2>/dev/null | grep -q "$MODEL" && ! ready; then
  log "模型上下文不正确，重新加载"
  $LMS unload "$MODEL"
fi

if ! ready; then
  log "加载模型 $MODEL（上下文 $CTX）"
  $LMS load "$MODEL" -c $CTX --gpu max --identifier "$MODEL" -y 2>&1 | tail -2
fi

if ready; then log "模型已就绪"; exit 0; fi
log "模型加载失败"; exit 1
