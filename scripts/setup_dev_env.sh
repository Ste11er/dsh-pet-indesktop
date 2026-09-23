#!/usr/bin/env bash
# 一键建立/更新本仓库的开发环境（Linux / macOS）：由 uv 管理 .venv。
#
#   scripts/setup_dev_env.sh            # 建 .venv 并按 requirements.lock 同步
#   scripts/setup_dev_env.sh --relock   # 改过 requirements.txt 后重新解析锁文件
#
# 设计取舍：
# - **Python 版本对齐 CI**：读 .python-version（当前 3.11，与
#   .github/workflows/pr-test.yml 的 matrix 一致）。本机与 CI 等价，避免
#   「本机红 CI 绿」的版本专属假红（见 docs/LINUX-DEV-ENVIRONMENT-2026-09-22.md）。
# - **uv 的缓存/解释器目录默认落在仓库内**（.uv-cache / .uv-python，均已
#   gitignore）：uv 默认写 ~/.cache/uv 与 ~/.local/share/uv，在只读 HOME、
#   容器或文件沙箱里会直接 EROFS。需要共用全局缓存时用环境变量覆盖即可。
# - **依赖锁**：requirements.txt 仍是唯一权威清单（CI 与打包脚本都读它），
#   requirements.lock 是它的 --universal 解析产物，仅用于本地可复现安装。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$ROOT/.uv-python}"

PYTHON_VERSION="$(cat .python-version 2>/dev/null || echo 3.11)"
LINUX_APT_LIBS="libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libfontconfig1 libdbus-1-3 fonts-noto-cjk"

case "${1:-}" in
  --relock) RELOCK=1 ;;
  "" )      RELOCK=0 ;;
  -h|--help)
    # 打印文件头的注释块（shebang 之后连续的 # 行），去掉前导 "# "
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    exit 0 ;;
  *)
    echo "未知参数：$1（可用：--relock / --help）" >&2
    exit 2 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "缺少 uv。安装：" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  echo "或 pipx install uv / pip install uv" >&2
  exit 1
fi

echo "==> 解释器：CPython $PYTHON_VERSION（与 CI pr-test.yml 一致）"
uv python install --no-bin "$PYTHON_VERSION"

if [ "$RELOCK" = 1 ]; then
  echo "==> 重新解析依赖锁：requirements.txt → requirements.lock（--universal）"
  uv pip compile requirements.txt -o requirements.lock --universal
fi

if [ ! -f requirements.lock ]; then
  echo "==> 缺少 requirements.lock，先生成一次"
  uv pip compile requirements.txt -o requirements.lock --universal
fi

echo "==> 建立 .venv 并按锁文件同步依赖（会移除多余包）"
uv venv --allow-existing .venv
uv pip sync --python .venv/bin/python requirements.lock

echo "==> 自检：PySide6 能否在无显示器环境导入"
if ! QT_QPA_PLATFORM=offscreen .venv/bin/python -c "import PySide6" 2>"/tmp/dspet-pyside-err.$$"; then
  echo "PySide6 导入失败。Linux 上通常是缺系统库，可执行：" >&2
  echo "  sudo apt-get install -y $LINUX_APT_LIBS" >&2
  echo "--- 原始报错 ---" >&2
  cat "/tmp/dspet-pyside-err.$$" >&2
  rm -f "/tmp/dspet-pyside-err.$$"
  exit 1
fi
rm -f "/tmp/dspet-pyside-err.$$"

cat <<EOF

完成。下一步：
  QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q     # 全量测试（无显示器）
  .venv/bin/python -m pet                                     # 启动桌宠（需要图形会话）

改过 requirements.txt 之后：scripts/setup_dev_env.sh --relock
EOF
