#!/usr/bin/env bash
set -euo pipefail

PLUGIN_NAME="tickflow-assist"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_DEFAULT_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python3"
if [ -n "${HERMES_PYTHON:-}" ]; then
  PYTHON_BIN="$HERMES_PYTHON"
elif [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON_BIN="$PYTHON_BIN"
elif [ -x "$HERMES_DEFAULT_PYTHON" ]; then
  PYTHON_BIN="$HERMES_DEFAULT_PYTHON"
else
  PYTHON_BIN="python3"
fi
DEFAULT_VENV_DIR="$ROOT_DIR/.venv"
FALLBACK_VENV_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/tickflow-assist-hermes/venv"
VENV_DIR="${TICKFLOW_ASSIST_VENV:-$DEFAULT_VENV_DIR}"
HERMES_PLUGIN_DIR="${HERMES_PLUGIN_DIR:-$HOME/.hermes/plugins}"
HERMES_SKILLS_DIR="${HERMES_SKILLS_DIR:-$HOME/.hermes/skills}"
PLUGIN_LINK="$HERMES_PLUGIN_DIR/$PLUGIN_NAME"
CONFIG_FILE="$ROOT_DIR/local.config.json"
VENV_MARKER_FILE="$ROOT_DIR/.tickflow-assist-venv"

info() {
  printf '%s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "未找到 Python：$PYTHON_BIN"

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit("ERROR: TickFlow Assist Hermes 需要 Python >= 3.10")
PY
PYTHON_VERSION="$("$PYTHON_BIN" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
info "==> 使用 Python：$PYTHON_BIN ($PYTHON_VERSION)"

info "==> 安装 Python 依赖"
if [ "$VENV_DIR" = "$DEFAULT_VENV_DIR" ]; then
  if [ -e "$DEFAULT_VENV_DIR" ] && [ ! -w "$DEFAULT_VENV_DIR" ]; then
    info "检测到 $DEFAULT_VENV_DIR 不可写，改用 $FALLBACK_VENV_DIR"
    VENV_DIR="$FALLBACK_VENV_DIR"
  elif [ ! -w "$ROOT_DIR" ]; then
    info "检测到项目目录不可写，改用 $FALLBACK_VENV_DIR"
    VENV_DIR="$FALLBACK_VENV_DIR"
  fi
fi

mkdir -p "$(dirname "$VENV_DIR")"
if [ -x "$VENV_DIR/bin/python" ]; then
  VENV_VERSION="$("$VENV_DIR/bin/python" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
  if [ "$VENV_VERSION" != "$PYTHON_VERSION" ]; then
    if [ -n "${TICKFLOW_ASSIST_VENV:-}" ]; then
      die "指定的虚拟环境 $VENV_DIR 使用 Python $VENV_VERSION，但 Hermes/安装 Python 是 $PYTHON_VERSION。请删除该虚拟环境后重试，或改用匹配 Hermes Python 的 TICKFLOW_ASSIST_VENV。"
    fi
    BACKUP_VENV_DIR="$VENV_DIR.py$VENV_VERSION.bak.$(date +%Y%m%d%H%M%S)"
    info "检测到虚拟环境 Python 版本不匹配：$VENV_VERSION != $PYTHON_VERSION"
    info "==> 将旧虚拟环境移动到：$BACKUP_VENV_DIR"
    mv "$VENV_DIR" "$BACKUP_VENV_DIR"
  fi
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
    die "创建虚拟环境失败：$VENV_DIR。若提示 ensurepip/venv 缺失，请先执行 sudo apt install python3-venv；若提示权限不足，请检查目录 owner 或设置 TICKFLOW_ASSIST_VENV=/path/to/writable/venv"
  fi
fi

VENV_DIR="$(cd "$VENV_DIR" && pwd)"
VENV_PYTHON="$VENV_DIR/bin/python"
info "==> 使用虚拟环境：$VENV_DIR"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e "$ROOT_DIR"
"$VENV_PYTHON" - <<'PY'
import importlib

for name in ("pandas", "numpy", "lancedb", "pyarrow", "requests", "yaml", "PIL"):
    importlib.import_module(name)
print("Python 依赖检查通过")
PY
if (umask 077; printf '%s\n' "$VENV_DIR" > "$VENV_MARKER_FILE"); then
  info "==> 已记录虚拟环境路径：$VENV_MARKER_FILE"
else
  info "==> 无法写入虚拟环境路径记录；如 Hermes 找不到依赖，请设置 TICKFLOW_ASSIST_VENV=$VENV_DIR"
fi

info "==> 配置 Hermes 插件目录"
mkdir -p "$HERMES_PLUGIN_DIR"
if [ -L "$PLUGIN_LINK" ]; then
  ln -sfn "$ROOT_DIR" "$PLUGIN_LINK"
elif [ -e "$PLUGIN_LINK" ]; then
  die "$PLUGIN_LINK 已存在且不是符号链接，请先手动处理该目录"
else
  ln -s "$ROOT_DIR" "$PLUGIN_LINK"
fi

info "==> 配置 Hermes Discord/Telegram 命令 skills"
mkdir -p "$HERMES_SKILLS_DIR"
LEGACY_TA_SKILL_LINK="$HERMES_SKILLS_DIR/ta"
if [ -L "$LEGACY_TA_SKILL_LINK" ]; then
  LEGACY_TA_TARGET="$(readlink "$LEGACY_TA_SKILL_LINK" || true)"
  if [ "$LEGACY_TA_TARGET" = "$ROOT_DIR/skills/ta" ] || [ "$LEGACY_TA_TARGET" = "$ROOT_DIR/skills/ta/" ] || [[ "$LEGACY_TA_TARGET" == *"tickflow-assist-hermes/skills/ta" ]]; then
    rm "$LEGACY_TA_SKILL_LINK"
    info "已移除旧 /ta 总入口 skill 链接：$LEGACY_TA_SKILL_LINK"
  fi
elif [ -e "$LEGACY_TA_SKILL_LINK" ]; then
  info "检测到 $LEGACY_TA_SKILL_LINK 已存在且不是符号链接；如不再需要 /ta 总入口，请手动处理"
fi
for legacy_skill_link in "$HERMES_SKILLS_DIR"/ta_*; do
  [ -e "$legacy_skill_link" ] || [ -L "$legacy_skill_link" ] || continue
  if [ -L "$legacy_skill_link" ]; then
    legacy_target="$(readlink "$legacy_skill_link" || true)"
    if [[ "$legacy_target" == "$ROOT_DIR"/skills/ta_* ]] || [[ "$legacy_target" == *"tickflow-assist-hermes/skills/ta_"* ]]; then
      rm "$legacy_skill_link"
      info "已移除旧 /ta_ skill 链接：$legacy_skill_link"
    fi
  else
    info "检测到 $legacy_skill_link 已存在且不是符号链接；如不再需要 /ta_ 旧入口，请手动处理"
  fi
done
for skill_dir in "$ROOT_DIR"/skills/ta-*; do
  [ -d "$skill_dir" ] || continue
  skill_name="$(basename "$skill_dir")"
  skill_link="$HERMES_SKILLS_DIR/$skill_name"
  if [ -L "$skill_link" ]; then
    ln -sfn "$skill_dir" "$skill_link"
  elif [ -e "$skill_link" ]; then
    info "跳过 $skill_link：已存在且不是符号链接，请手动处理后再运行安装脚本"
  else
    ln -s "$skill_dir" "$skill_link"
  fi
done
skill_count="$(find "$HERMES_SKILLS_DIR" -maxdepth 1 -type l -name 'ta-*' 2>/dev/null | wc -l | tr -d ' ')"
info "已安装 TickFlow slash command skills：$skill_count 个"

HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
HERMES_CONFIG_FILE="${HERMES_CONFIG_FILE:-$HOME/.hermes/config.yaml}"
if [ -f "$HERMES_ENV_FILE" ] && grep -Eq '^[[:space:]]*DISCORD_COMMAND_SYNC_POLICY=["'"'"']?off["'"'"']?[[:space:]]*$' "$HERMES_ENV_FILE"; then
  info "警告：$HERMES_ENV_FILE 中 DISCORD_COMMAND_SYNC_POLICY=off，Discord 原生命令不会同步"
fi
if [ -f "$HERMES_CONFIG_FILE" ] && grep -Eq '^[[:space:]]*slash_commands:[[:space:]]*false[[:space:]]*$' "$HERMES_CONFIG_FILE"; then
  info "警告：$HERMES_CONFIG_FILE 中 slash_commands: false，Discord 原生命令不会同步"
fi

info "==> 写入本地配置：$CONFIG_FILE"
"$VENV_PYTHON" "$ROOT_DIR/configure_tickflow.py" "$CONFIG_FILE" "$ROOT_DIR"

info ""
info "安装完成。请重启 Hermes gateway，然后在会话里运行 /plugins 确认 tickflow-assist 已加载，并等待 Discord/Telegram slash commands 同步。"
