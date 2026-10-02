#!/usr/bin/env bash
# 服务器自动更新：拉取 origin/$BRANCH，按需安装依赖，代码有变化时重启服务。
#
# 触发方式（两种都以 root 运行）：
#   - eve-skill-plan-update.timer（systemd 定时器，兜底轮询）
#   - GitHub Actions 经 SSH 执行（push 后即时；见 .github/workflows/deploy.yml）
#
# 说明：
#   - 只读拉取通过 $APP_DIR/.deploy_key（GitHub 只读 Deploy Key）完成；
#   - APP_USER 与运行服务的用户一致（默认 root，与 eve-skill-plan.service 对齐）；
#   - data/（skills.db / app.db / 日志）与 config.json 均被 .gitignore 忽略，
#     git reset --hard 不会触碰它们，技能索引、已保存计划与 OAuth 凭据不受影响。
set -euo pipefail

APP_DIR=/root/eve-skill-plan
APP_USER=root
BRANCH=main
DEPLOY_KEY="$APP_DIR/.deploy_key"

export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

cd "$APP_DIR"

# 以运行服务的用户执行 git / pip（root 直接跑，非 root 用 runuser 切换），
# 保证文件属主与服务用户一致
as_app() {
  if [ "$APP_USER" = "root" ]; then
    "$@"
  else
    runuser -u "$APP_USER" -- env GIT_SSH_COMMAND="$GIT_SSH_COMMAND" "$@"
  fi
}

before=$(as_app git rev-parse HEAD)
as_app git fetch --quiet origin "$BRANCH"
after=$(as_app git rev-parse "origin/$BRANCH")

if [ "$before" = "$after" ]; then
    echo "[auto-update] 已是最新（$before），无需更新"
    exit 0
fi

echo "[auto-update] 更新 $before -> $after"
as_app git reset --hard "origin/$BRANCH"

# 仅当依赖清单变化时才安装，减少无谓耗时
if as_app git diff --name-only "$before" "$after" | grep -q '^requirements.txt$'; then
    echo "[auto-update] requirements.txt 有变化，安装依赖"
    as_app python3 -m pip install -q -r requirements.txt
fi

systemctl restart eve-skill-plan
echo "[auto-update] 已重启 eve-skill-plan 服务"
