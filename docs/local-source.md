# 本地协议演练

`python3 scenario.py` 启动仅监听回环的业务来源，输出临时端口和可用路由。进程在前台运行，以 Ctrl-C 关闭。

`python3 scenario.py --export` 输出演练响应定义，可按 `docs/source-contract.md` 在独立副本增加乱序、截断、失败或来源更新情形。响应数组顺次消耗并保持最后状态；不会调用真实第三方系统。

请求者可以在测试中用 `from scenario import scenario` 和 `ReplaySource(scenario())` 获得同样来源并检查 requests，避免固定端口冲突。现有 scenario 测试核对来源实际返回值，不表示业务语义采集已实现。

标准演练顺序是先准备依赖，再运行 `python3 -m unittest discover -s tests -v`、构建命令和 `python3 app.py demo`。新增能力演示须调用实际新增入口，不以现有结构检查输出代替；演练应选择有限输入，并在九十秒内完成。
