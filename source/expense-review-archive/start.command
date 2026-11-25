#!/bin/zsh
set -eu
cd "${0:A:h}"
base="$PWD"
mode="${1:-mac}"
case "$mode" in
  mac|cloud|archive) ;;
  *) print -u2 '用法：./start.command [mac|cloud|archive]'; exit 2 ;;
esac
if [[ -x .venv/bin/python ]]; then
  python_bin="$base/.venv/bin/python"
else
  python_bin="$(command -v python3)"
fi
if [[ ! -f "${EXPENSE_CONFIG:-config.local.json}" ]]; then
  print -u2 '请先复制 config.example.json 为 config.local.json，并填入本地审核规则。'
  exit 1
fi
export EXPENSE_CONFIG="${EXPENSE_CONFIG:-config.local.json}"
export EXPENSE_CONFIG="${EXPENSE_CONFIG:A}"
mkdir -p 输入_汇总凭证PDF 输入_系统截图Word
pdfs=(输入_汇总凭证PDF/*.pdf(N))
docs=(输入_系统截图Word/*.docx(N))
if (( ${#pdfs} == 0 )); then
  print -u2 '请先将交易汇总 PDF 放入 输入_汇总凭证PDF。'
  exit 1
fi
if [[ "$mode" != archive ]] && (( ${#docs} == 0 )); then
  print -u2 '请先将含有费用截图的 Word 文件放入 输入_系统截图Word。'
  exit 1
fi
if [[ "$mode" = cloud && -z "${DASHSCOPE_API_KEY:-}" ]]; then
  print -u2 '云端模式需要环境变量 DASHSCOPE_API_KEY。'
  exit 1
fi
mkdir -p 结果
run_dir="$(mktemp -d "$base/结果/$(date +%Y%m%d-%H%M%S)-XXXXXX")"
if [[ "$mode" != archive ]]; then
  if [[ "$mode" = mac ]]; then
    mkdir -p .build
    if [[ ! -x .build/vision || src/vision.swift -nt .build/vision ]]; then
      swiftc src/vision.swift -o .build/vision
    fi
    entry=mac_review.py
  else
    entry=auto_check.py
  fi
  mkdir -p "$run_dir/审核"
  (cd "$run_dir/审核" && "$python_bin" -u "$base/src/$entry")
fi
mkdir -p "$run_dir/归档"
"$python_bin" -u src/pdf_employee_archive.py --pdf-dir 输入_汇总凭证PDF --output-dir "$run_dir/归档/员工凭证" --excel-output "$run_dir/归档/非标发票汇总.xlsx"
print "运行结果：$run_dir"
if [[ -t 1 ]]; then open "$run_dir"; fi
