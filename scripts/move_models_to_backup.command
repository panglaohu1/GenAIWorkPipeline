#!/bin/zsh
# 双击：把不再使用的模型（Bonsai、Sage-A1.5、Gemma 4 31B）移到外接盘 /Volumes/ORICO1T/modelbackup。
# 先复制，再逐个文件校验（大小 + 内容校验和），全部一致后才删除项目里的原目录；任何一步失败都不删除。
ROOT="${0:A:h:h}"
cd "$ROOT"
mkdir -p logs
exec > >(tee -a logs/move_models.log) 2>&1
echo "=== move_models $(date) ==="
DEST=/Volumes/ORICO1T/modelbackup
if [ ! -d /Volumes/ORICO1T ]; then echo "外接盘 /Volumes/ORICO1T 没有挂载，停止。"; exit 1; fi
mkdir -p "$DEST" || { echo "无法创建 $DEST，停止。"; exit 1; }
for M in bonsai2-27b-mlx sage-a1.5-gguf gemma-4-31b-it-4bit; do
  SRC="models/llm/$M"
  if [ ! -d "$SRC" ]; then echo "[$M] 项目里已没有，跳过"; continue; fi
  echo "[$M] 复制到 $DEST/$M（$(du -sh "$SRC" | cut -f1)）"
  rsync -a "$SRC" "$DEST/" || { echo "[$M] 复制失败，保留原目录"; continue; }
  echo "[$M] 校验（逐文件比对校验和）"
  DIFF=$(rsync -anc --out-format='%n' "$SRC/" "$DEST/$M/" | grep -v '/$')
  if [ -n "$DIFF" ]; then echo "[$M] 校验不一致，保留原目录：$DIFF"; continue; fi
  # 外接盘（exFAT）上 macOS 会自动生成 ._* / .DS_Store，不计入
  N1=$(find "$SRC" -type f ! -name '._*' ! -name '.DS_Store' | wc -l); N2=$(find "$DEST/$M" -type f ! -name '._*' ! -name '.DS_Store' | wc -l)
  if [ "$N1" != "$N2" ]; then echo "[$M] 文件数不一致（$N1 / $N2），保留原目录"; continue; fi
  rm -rf "$SRC" && echo "[$M] 校验一致（$N1 个文件），已从项目删除"
done
echo "项目目录现在：$(du -sh "$ROOT" | cut -f1)；models/llm：$(du -sh models/llm | cut -f1)"
echo "外接盘备份：$(du -sh "$DEST" | cut -f1)"
echo "完成。可以关闭这个窗口。"
