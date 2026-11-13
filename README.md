# weather-grid

天气网格 GRIB2 消息的来源检查、原件留存与港区格点实际采集工具。

## 能力

- `source.py` 查看来源的段结构，不把检查结果冒充完整业务采集。
- `collector.py` 在本地限定范围内做实际数据采集：组合一条消息中的网格（3.0）、产品（4.0）、位图（第 6 段）与 simple packing 数据（5.0/第 7 段），恢复位图缺测位置，按扫描方向建立与扫描方向无关的稳定格点索引。打包值个数不会被误当作网格总点数。
- `storage.py` 将原始字节、来源标识、摘要、解析场与拒绝记录一起保存在本地 SQLite；同一有效时刻的不同起报批次按各自 `identity_key` 独立保存，重复原件仍保留同一收件编号。
- `sampledata.py` 按规范字节布局构造可重复的本地虚构数据，覆盖反向扫描、跨零经线、复用位图、多起报批次与坏场隔离；`transport.py` 提供独立的回环 HTTP 演练源与请求记录。

解释范围、位图复用边界与来源变体见 `docs/source-contract.md`。结构检查测试与业务采集测试分开保留（`tests/test_source.py`、`tests/test_scenario.py` 与 `tests/test_collector.py`）。

## 环境

Python 3.11 或以上，仅标准库，无需凭据和外部数据。使用容器时可执行 `docker build -t weather-grid .`，依赖及镜像须在正式运行前准备。
运行入口、测试与演示均不安装包，不访问公网。数据库使用临时路径，不向代码目录写入业务文件。

## 测试与构建

构建工具版本固定在 `requirements-build.txt`，在环境准备阶段执行 `python3 -m pip install -r requirements-build.txt`；容器构建已包含此步骤。应用运行仍仅使用标准库，测试及演示不安装依赖。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py source.py collector.py sampledata.py storage.py transport.py scenario.py
python3 -m build --wheel --no-isolation
```

## 快速演示

```sh
python3 app.py demo
```

演示先执行原有的回环来源结构检查与留存，再执行端到端业务采集：通过回环 HTTP 逐份领取夹具、采集场、入库，并演示跨零经线港区查询、反向扫描稳定索引、同有效时刻多起报批次、位图复用与坏场隔离。正常在数秒内完成，不以预录日志代替执行。

## 单独检查、收件与采集

```sh
python3 app.py fixtures /tmp/weather-grid-fixtures
python3 app.py inspect /tmp/weather-grid-fixtures/sample.grib2
python3 app.py capture /tmp/weather-grid-fixtures/sample.grib2 --database /tmp/weather-grid.sqlite3 --source local-partner
python3 app.py list --database /tmp/weather-grid.sqlite3

# 实际数据采集入库（场与拒绝记录分别持久化）
python3 app.py harvest /tmp/weather-grid-fixtures/harbor-batch-a.grib2 \
    --database /tmp/weather-grid.sqlite3 --source local-partner
# 列出已保存场，可按有效时刻过滤
python3 app.py fields --database /tmp/weather-grid.sqlite3 --valid-time 2026-11-12T06:00:00
# 按港区经纬度框取值与缺测原因（west>east 表示跨零环绕区间）
python3 app.py harbor --database /tmp/weather-grid.sqlite3 \
    --west -1 --east 1 --south 29.5 --north 31.5 --valid-time 2026-11-12T06:00:00
```

生成夹具不会覆盖已有文件。运行错误以 JSON 输出到标准错误并返回退出码 2；原始输入无效时不生成成功收件。单个场不被支持或缺先前位图时只拒绝该场，保留后续消息可检查，不会用前一场的网格解释它。内部原件与任何对外展示副本应分开管理，不写入真实个人数据。
