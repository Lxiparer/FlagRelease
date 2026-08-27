#!/bin/bash
# 多机免密 SSH 配置工具（经堡垒机，流程外独立运行，一次性配置）
#
# 场景：master 经 JumpServer 堡垒机访问各内网子节点。本脚本在这条堡垒机路径上：
#   1. 确保 master 拥有密钥对（无则自动生成）；
#   2. 把带来源备注的 master 公钥，经堡垒机分发到各子节点的 authorized_keys；
#   3. 经堡垒机验证公钥已安装、子节点连通且 docker 可用；
#   4. 打印可直接粘贴进 deploy_config.yaml 的 ssh/nodes 片段。
#
# 子节点登录用户：每个节点独立按 root → secure 顺序尝试，各节点最终用户可不一致。
#
# 关于“免密”的说明：连接堡垒机本身始终需要堡垒机密码，本脚本无法免掉这一步。
#   分发到子节点的公钥，作用是堡垒机路由到子节点后的第二跳认证 / 将来的直连场景。
#
# 公钥备注（comment）：仅替换公钥行的第三段（认证只用 type+base64，comment 被 SSH 忽略），
#   因此【不影响认证，也不改动本地 id_rsa.pub】，只写入一份临时副本用于分发。
#   格式：flagos-multinode-deploy master=<user>@<对外IP> added=<YYYY-MM-DDTHH:MM>
#   便于日后在子节点 ~/.ssh/authorized_keys 中用前缀 grep 识别与清理。
#
# 与流程的关系：本脚本【不】属于自动化流水线，需在跑任务前手动执行一次。
#
# 用法:
#   bash setup_passwordless_ssh.sh --nodes "<host:gpus>,..." \
#     --jump-user <堡垒机用户> --password <堡垒机密码> --master-ip <master对外IP> [选项]
#
# 节点列表格式与 run_pipeline.sh 的 --nodes 一致（host:gpus，第一个为 master）；
# gpus 字段仅为对齐格式，本脚本忽略。host 为子节点内网 IP（rank 0 也用于 --master-addr）。
#
# 必填:
#   --nodes <list>        节点列表（host:gpus,...）
#   --jump-user <user>    堡垒机登录用户
#   --password <pass>     堡垒机密码（需已安装 sshpass）
#   --master-ip <ip>      master 对外 IP，写入公钥 comment 标识来源（机器多 IP，必须显式指定）
# 可选:
#   --jump-host <ip>      堡垒机地址，默认 120.92.211.161
#   --jump-port <port>    堡垒机 SSH 端口，默认 22
#   --ssh-key <path>      master 私钥路径，默认 ~/.ssh/id_rsa；不存在则自动生成
#   -h, --help            显示帮助
#
# 示例:
#   bash setup_passwordless_ssh.sh --nodes "10.212.14.18:8,10.212.14.19:8" \
#     --jump-user '<堡垒机用户>' --password '<堡垒机密码>' --master-ip 10.212.14.18 --jump-port 2224
#
# ========================================
# 使用方式
# ========================================
#
# 1. 前置依赖
#    - 已安装 sshpass: apt-get install -y sshpass 或 yum install -y sshpass
#    - 堡垒机已授权该用户访问目标子节点内网 IP
#    - 子节点存在 root 或 secure 账号且允许登录
#
# 2. 执行脚本（在 master 节点上）
#    bash skills/flagos-container-preparation/tools/setup_passwordless_ssh.sh \
#      --nodes "172.21.16.6:8,172.21.16.14:8" \
#      --jump-user ckxu --password '<堡垒机密码>' \
#      --master-ip 172.21.16.6 --jump-port 2224
#
#    节点列表第一个为 master(本机),后续为 worker。
#
# 3. 脚本输出四步
#    [1/4] master 密钥对准备（无则自动生成 ~/.ssh/id_rsa）
#    [2/4] 经堡垒机分发公钥到各子节点（每节点独立试 root→secure,幂等）
#          输出：STATE_ADDED（新增）| STATE_UPDATED（补备注）| STATE_SKIP（已就绪,跳过）
#    [3/4] 经堡垒机验证公钥在位、子节点连通、docker 可用性
#    [4/4] 打印 deploy_config.yaml 的 ssh/nodes 片段（复制到你的 config 中）
#
# 4. 幂等性
#    - 可安全重复执行：已有备注的公钥不会重复写入或更新时间戳（STATE_SKIP）
#    - 密钥体已存在但无本工具备注时，会更新该行 comment（STATE_UPDATED）
#    - 本地 ~/.ssh/id_rsa.pub 始终不变（分发用临时拼接行）
#
# 5. 查看节点上的公钥配置
#    经堡垒机查看子节点的 ~/.ssh/authorized_keys:
#      sshpass -p '<堡垒机密码>' ssh -p 2224 \
#        'ckxu@root@172.21.16.14@120.92.211.161' \
#        "cat ~/.ssh/authorized_keys"
#    找出本工具分发的那行（含 flagos-multinode-deploy 备注）:
#      sshpass -p '<堡垒机密码>' ssh -p 2224 \
#        'ckxu@root@172.21.16.14@120.92.211.161' \
#        "grep flagos-multinode-deploy ~/.ssh/authorized_keys"
#
# 6. 清理公钥（将来不再需要时）
#    经堡垒机删除本工具分发的公钥（所有含 flagos-multinode-deploy 的行）:
#      sshpass -p '<堡垒机密码>' ssh -p 2224 \
#        'ckxu@root@172.21.16.14@120.92.211.161' \
#        "grep -v 'flagos-multinode-deploy' ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp && \
#         mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys"
#
# 7. 后续使用
#    将脚本输出的 ssh/nodes 片段粘贴进 deploy_config.yaml 后，即可用 run_pipeline.sh
#    或 deploy_vllm.py 执行多机任务。注意：连接堡垒机仍需在 config 的 ssh.password
#    填堡垒机密码（本脚本无法免掉堡垒机这一跳的密码）。
#
# ========================================

set -uo pipefail

# ========== 参数解析 ==========
NODE_LIST=""
PASSWORD=""
MASTER_IP=""
SSH_KEY="$HOME/.ssh/id_rsa"
JUMP_HOST="120.92.211.161"
JUMP_USER=""
JUMP_PORT=22

usage() {
    sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --nodes|--node-list) NODE_LIST="$2"; shift 2 ;;
        --jump-user) JUMP_USER="$2"; shift 2 ;;
        --password)  PASSWORD="$2"; shift 2 ;;
        --master-ip) MASTER_IP="$2"; shift 2 ;;
        --jump-host) JUMP_HOST="$2"; shift 2 ;;
        --jump-port) JUMP_PORT="$2"; shift 2 ;;
        --ssh-key)   SSH_KEY="$2"; shift 2 ;;
        -h|--help)   usage 0 ;;
        *) echo "错误: 未知参数 '$1'"; usage 1 ;;
    esac
done

# ========== 必填校验 ==========
missing=()
[ -z "$NODE_LIST" ] && missing+=("--nodes")
[ -z "$JUMP_USER" ] && missing+=("--jump-user")
[ -z "$PASSWORD" ]  && missing+=("--password（堡垒机密码）")
[ -z "$MASTER_IP" ] && missing+=("--master-ip（master对外IP，写入公钥备注）")
if [ ${#missing[@]} -gt 0 ]; then
    echo "错误: 缺少必填参数: ${missing[*]}"
    echo ""
    usage 1
fi
if ! command -v sshpass >/dev/null 2>&1; then
    echo "错误: 经堡垒机用密码认证，需要 sshpass 但未安装。"
    echo "  安装: apt-get install -y sshpass  或  yum install -y sshpass"
    exit 1
fi

# ========== 解析节点列表 ==========
IFS=',' read -ra NODE_SPECS <<< "$NODE_LIST"
HOSTS=()
for spec in "${NODE_SPECS[@]}"; do
    host="${spec%%:*}"                 # 取 host，丢弃 :gpus
    host="$(echo "$host" | xargs)"     # 去除首尾空白
    [ -n "$host" ] && HOSTS+=("$host")
done
if [ ${#HOSTS[@]} -lt 1 ]; then
    echo "错误: 节点列表为空"
    exit 1
fi
MASTER_HOST="${HOSTS[0]}"

# 子节点登录用户候选：每节点独立按此顺序尝试
TARGET_USER_CANDIDATES=("root" "secure")

# ========== 确保 master 拥有密钥对 ==========
PUB_KEY="${SSH_KEY}.pub"
if [ ! -f "$SSH_KEY" ]; then
    echo "[1/4] master 未找到私钥 ${SSH_KEY}，自动生成密钥对..."
    mkdir -p "$(dirname "$SSH_KEY")"
    chmod 700 "$(dirname "$SSH_KEY")"
    ssh-keygen -t rsa -b 4096 -N "" -f "$SSH_KEY" -q
    echo "  ✓ 已生成: ${SSH_KEY}"
else
    echo "[1/4] master 已存在私钥: ${SSH_KEY}"
    if [ ! -f "$PUB_KEY" ]; then
        echo "  公钥缺失，从私钥重建 ${PUB_KEY}..."
        ssh-keygen -y -f "$SSH_KEY" > "$PUB_KEY"
    fi
fi
chmod 600 "$SSH_KEY"
chmod 644 "$PUB_KEY"

# ========== 构造带备注的分发公钥（不改动本地 PUB_KEY）==========
KEY_COMMENT="flagos-multinode-deploy master=${JUMP_USER}@${MASTER_IP} added=$(date '+%Y-%m-%dT%H:%M')"
read -r KEY_TYPE KEY_BODY _ < "$PUB_KEY"     # 取原公钥的 type 与 base64 两段
PUB_LINE="${KEY_TYPE} ${KEY_BODY} ${KEY_COMMENT}"
echo "  分发公钥备注: ${KEY_COMMENT}"
echo "  （备注仅用于子节点识别来源，不参与认证，本地 ${PUB_KEY} 不变）"
echo ""

# 经堡垒机执行远程命令，带一次重试（堡垒机多跳链路偶发抖动/限流，单次失败不代表用户不可用）。
# 用法: jump_ssh <route> <remote_cmd>；stdout 为远端输出，返回码 0 表示成功拿到输出。
jump_ssh() {
    local route="$1" cmd="$2" out
    local attempt
    for attempt in 1 2; do
        out=$(sshpass -p "$PASSWORD" ssh \
            -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
            -p "$JUMP_PORT" "${route}@${JUMP_HOST}" "$cmd" 2>/dev/null)
        if [ -n "$out" ]; then
            printf '%s' "$out"
            return 0
        fi
        [ "$attempt" -eq 1 ] && sleep 2      # 抖动/限流，隔 2 秒重试一次
    done
    return 1
}

# ========== banner ==========
echo "============================================================"
echo "  多机免密 SSH 配置 · 经堡垒机分发公钥"
echo "============================================================"
echo "  节点总数: ${#HOSTS[@]}    节点: ${HOSTS[*]}"
echo "  堡垒机:   ${JUMP_USER}@${JUMP_HOST}:${JUMP_PORT}"
echo "  子节点用户: ${TARGET_USER_CANDIDATES[*]}（每节点独立按序尝试，可不一致）"
echo "  master IP: ${MASTER_IP}    私钥: ${SSH_KEY}"
echo "============================================================"
echo ""

# ========== 经堡垒机分发公钥 ==========
echo "[2/4] 经堡垒机把 master 公钥分发到各子节点..."
FAILED=()
RESOLVED_USERS=()     # 与 HOSTS 平行，记录每节点实际连通的用户
RANK=0
for host in "${HOSTS[@]}"; do
    resolved=""
    for tuser in "${TARGET_USER_CANDIDATES[@]}"; do
        # ssh_user 三段式：堡垒机用户@子节点用户@子节点IP，交给 JumpServer 路由
        route="${JUMP_USER}@${tuser}@${host}"
        # 幂等分发（三态）：
        #   - 密钥体不存在        → 追加带备注的行        (STATE_ADDED)
        #   - 存在且已含本工具备注 → 真跳过，不重复改写    (STATE_SKIP)
        #   - 存在但无本工具备注  → 删旧行、补带备注的新行 (STATE_UPDATED)
        remote_cmd="umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; \
if grep -qF '${KEY_BODY}' ~/.ssh/authorized_keys; then \
  if grep -F '${KEY_BODY}' ~/.ssh/authorized_keys | grep -q 'flagos-multinode-deploy'; then \
    echo STATE_SKIP; \
  else \
    grep -vF '${KEY_BODY}' ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp && mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys; \
    printf '%s\n' '${PUB_LINE}' >> ~/.ssh/authorized_keys; \
    echo STATE_UPDATED; \
  fi; \
else \
  printf '%s\n' '${PUB_LINE}' >> ~/.ssh/authorized_keys; \
  echo STATE_ADDED; \
fi"
        out=$(jump_ssh "$route" "$remote_cmd")
        if [ $? -eq 0 ] && echo "$out" | grep -qE 'STATE_ADDED|STATE_UPDATED|STATE_SKIP'; then
            resolved="$tuser"
            case "$out" in
                *STATE_ADDED*)   echo "  [rank ${RANK}] ${host} (用户 ${tuser}) — ✓ 公钥已分发（新增）" ;;
                *STATE_UPDATED*) echo "  [rank ${RANK}] ${host} (用户 ${tuser}) — ✓ 公钥已在位，已补来源备注" ;;
                *STATE_SKIP*)    echo "  [rank ${RANK}] ${host} (用户 ${tuser}) — ✓ 公钥已在位且备注就绪，跳过" ;;
            esac
            break
        else
            if [ "$tuser" != "${TARGET_USER_CANDIDATES[-1]}" ]; then
                echo "  [rank ${RANK}] ${host} (用户 ${tuser}) — ✗ 失败，尝试下一候选用户"
            else
                echo "  [rank ${RANK}] ${host} (用户 ${tuser}) — ✗ 失败"
            fi
        fi
    done
    if [ -n "$resolved" ]; then
        RESOLVED_USERS+=("$resolved")
    else
        RESOLVED_USERS+=("${TARGET_USER_CANDIDATES[0]}")   # 占位，便于生成 config
        FAILED+=("$host")
    fi
    RANK=$((RANK + 1))
done
echo ""

# ========== 经堡垒机验证 ==========
echo "[3/4] 经堡垒机验证公钥已安装、子节点连通..."
VERIFY_FAILED=()
RANK=0
for host in "${HOSTS[@]}"; do
    if printf '%s\n' "${FAILED[@]:-}" | grep -qx "$host"; then
        echo "  [rank ${RANK}] ${host} — 跳过（分发失败）"
        RANK=$((RANK + 1)); continue
    fi
    tuser="${RESOLVED_USERS[$RANK]}"
    route="${JUMP_USER}@${tuser}@${host}"
    check_cmd="grep -qF '${KEY_BODY}' ~/.ssh/authorized_keys && echo KEY_OK; \
hostname; docker info >/dev/null 2>&1 && echo DOCKER_OK || echo DOCKER_MISSING"
    out=$(jump_ssh "$route" "$check_cmd")
    if [ $? -eq 0 ] && echo "$out" | grep -q KEY_OK; then
        rhost=$(echo "$out" | grep -v -E 'KEY_OK|DOCKER_OK|DOCKER_MISSING' | head -1)
        if echo "$out" | grep -q DOCKER_OK; then
            echo "  [rank ${RANK}] ${host} (用户 ${tuser}) — ✓ 公钥在位（${rhost}，docker 可用）"
        else
            echo "  [rank ${RANK}] ${host} (用户 ${tuser}) — ✓ 公钥在位（${rhost}），⚠ docker 不可用"
        fi
    else
        echo "  [rank ${RANK}] ${host} (用户 ${tuser}) — ✗ 验证失败（公钥未确认）"
        VERIFY_FAILED+=("$host")
    fi
    RANK=$((RANK + 1))
done
echo ""

# ========== 生成 config 片段 ==========
echo "[4/4] 生成 deploy_config.yaml 的 nodes 片段（供参考）..."
echo "  ----------------------------------------------------------"
echo "  nodes:"
RANK=0
for host in "${HOSTS[@]}"; do
    echo "    - name: \"node-${RANK}\""
    echo "      host: \"${host}\"    # 子节点内网 IP（rank 0 也用于 --master-addr）"
    echo "      ssh_user: \"${RESOLVED_USERS[$RANK]}\"    # 本脚本解析出的实际连通用户"
    [ "$RANK" -eq 0 ] && echo "      local: true"
    RANK=$((RANK + 1))
done
echo "  ----------------------------------------------------------"
echo ""

# ========== 汇总 ==========
echo "============================================================"
if [ ${#FAILED[@]} -eq 0 ] && [ ${#VERIFY_FAILED[@]} -eq 0 ]; then
    echo "  ✓ 公钥分发完成，所有子节点连通、公钥在位"
    echo "============================================================"
    exit 0
else
    echo "  ✗ 配置未完全成功"
    [ ${#FAILED[@]} -gt 0 ]        && echo "    公钥分发失败: ${FAILED[*]}"
    [ ${#VERIFY_FAILED[@]} -gt 0 ] && echo "    验证失败:     ${VERIFY_FAILED[*]}"
    echo "============================================================"
    echo "  排查建议："
    echo "    - 确认堡垒机地址/端口/用户/密码正确"
    echo "    - 确认堡垒机已授权该用户访问这些子节点内网 IP"
    echo "    - 确认子节点存在 root 或 secure 账号且允许该账号登录"
    echo "    - 手动重试: sshpass -p '<密码>' ssh -p ${JUMP_PORT} ${JUMP_USER}@<root或secure>@<子节点IP>@${JUMP_HOST}"
    exit 1
fi
