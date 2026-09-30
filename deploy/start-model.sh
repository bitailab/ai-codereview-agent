#!/bin/zsh
# 启动 LM Studio 服务并以 40K 上下文、单并发加载审查模型。由 launchd（com.aicr.model）在登录时执行，也可手动运行。
# 必须显式指定上下文长度，否则会使用默认的小上下文，内容会被静默截断。
set -u
LMS=~/.lmstudio/bin/lms
# GGUF + llama.cpp 引擎。MLX 引擎在 macOS 15 上多次触发内核 panic（IOGPUMemory "prepare count underflow"）
MODEL_KEY=qwen3.6-35b-a3b-switch@q4_k_m   # lms ls 中的模型名
MODEL=qwen3.6-35b-a3b-gguf-switch         # API 中的模型 id，与 .env 的 LLM_MODEL 一致
# 48GB 机器上模型权重约占 21G；上下文过大或多并发槽会让 KV/缓存把 GPU 内存耗尽，导致花屏死机
CTX=40960
PARALLEL=1   # ai-cr 串行调用模型，多余的并发槽只会多占内存
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
sys.exit(0 if any(m['id']=='$MODEL' and m.get('state')=='loaded' for m in ms) else 1)" 2>/dev/null &&
  $LMS ps --json 2>/dev/null | python3 -c "
import json,sys
ms = json.load(sys.stdin)
sys.exit(0 if any(m.get('identifier')=='$MODEL' and m.get('contextLength')==$CTX and m.get('parallel')==$PARALLEL for m in ms) else 1)" 2>/dev/null
}

if ready; then log "模型已就绪"; exit 0; fi

# 后台启动 LM Studio（不抢焦点），等待 CLI 可用
open -gja "LM Studio"
for i in {1..60}; do $LMS ps >/dev/null 2>&1 && break; sleep 5; done

log "启动本地服务"
$LMS server start

# 若以错误的上下文或并发数加载过（例如被 JIT 自动加载），先卸载
if $LMS ps 2>/dev/null | grep -q "$MODEL" && ! ready; then
  log "模型上下文或并发数不正确，重新加载"
  $LMS unload "$MODEL"
fi

if ! ready; then
  log "加载模型 $MODEL（上下文 $CTX，并发 $PARALLEL）"
  $LMS load "$MODEL_KEY" -c $CTX --parallel $PARALLEL --gpu max --identifier "$MODEL" -y 2>&1 | tail -2
fi

if ready; then log "模型已就绪"; exit 0; fi
log "模型加载失败"; exit 1
