#!/bin/zsh
# 双击运行星球游戏：首次先导入模型（Godot 运行前必须导入资源），之后直接进游戏。日志写在本目录 play.log / import.log。
cd "${0:A:h}"
G=/Applications/Godot.app/Contents/MacOS/Godot
if [ ! -d .godot/imported ] || [ assets/oskar_atlas.glb -nt .godot/imported ]; then   # 模型更新过也重新导入
  echo "首次运行：正在导入模型（约 10–60 秒）…"
  "$G" --headless --path . --import > import.log 2>&1
  echo "导入完成（日志 import.log）"
  touch .godot/imported
fi
echo "启动游戏…（日志 play.log）"
"$G" --path . 2>&1 | tee play.log
