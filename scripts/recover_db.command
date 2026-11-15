#!/bin/zsh
# 双击：修复运行记录数据库 data/pipeline.db（"database disk image is malformed"）。
# 先停控制台 → 原库（含 -wal/-shm）整体备份到 data/_backup_db_<时间>/ → 用 sqlite3 .recover 导出到新库并做完整性检查
# → 新库通过才替换（原文件只移动、不删除）→ 后台重新启动控制台。输出写到 logs/recover_db.log。
set -u
ROOT="${0:A:h:h}"
cd "$ROOT"
mkdir -p logs
exec > >(tee -a logs/recover_db.log) 2>&1
TS=$(date +%Y%m%d-%H%M%S)
BK="data/_backup_db_$TS"
echo "=== recover_db $TS ==="
PORT=$(awk -F= '/^\[console\]/{c=1} c&&/^port/{gsub(/ /,"",$2);print $2;exit}' config/pipeline.toml)
PIDS=$(lsof -ti tcp:${PORT:-8701} -sTCP:LISTEN)
if [ -n "$PIDS" ]; then echo "停止控制台 $PIDS"; kill ${=PIDS}; sleep 3; kill -9 $(lsof -ti tcp:${PORT:-8701} -sTCP:LISTEN) 2>/dev/null; fi
mkdir -p "$BK"
cp -p data/pipeline.db* "$BK"/
echo "已备份到 $BK"
CHECK=$(sqlite3 data/pipeline.db "PRAGMA integrity_check;" 2>&1 | head -3)
echo "原库完整性：$CHECK"
if [ "$CHECK" = "ok" ]; then
  echo "原库完整，只做 WAL 检查点"
  sqlite3 data/pipeline.db "PRAGMA wal_checkpoint(TRUNCATE);"
else
  rm -f data/pipeline_recovered.db
  sqlite3 data/pipeline.db ".recover" | sqlite3 data/pipeline_recovered.db
  NEW=$(sqlite3 data/pipeline_recovered.db "PRAGMA integrity_check;" 2>&1 | head -1)
  echo "恢复库完整性：$NEW；运行数 $(sqlite3 data/pipeline_recovered.db 'select count(*) from runs;' 2>&1)，事件数 $(sqlite3 data/pipeline_recovered.db 'select count(*) from events;' 2>&1)"
  if [ "$NEW" = "ok" ]; then
    mkdir -p "$BK/replaced"
    mv data/pipeline.db data/pipeline.db-wal data/pipeline.db-shm "$BK/replaced/" 2>/dev/null
    mv data/pipeline_recovered.db data/pipeline.db
    echo "已换成恢复后的数据库（原文件在 $BK/replaced/）"
  else
    echo "恢复库仍不完整，保持原状，请把 logs/recover_db.log 发给 Claude"
  fi
fi
echo "后台启动控制台…"
nohup "$ROOT/scripts/start.sh" >> logs/console_restart.log 2>&1 &
echo "完成。可以关闭这个窗口。"
