# 容器地址租约回收仲裁庭

离线模拟共享云原生集群中地址池、Pod 地址租约和网关撤路由后的安全回收。使用 Python 3.11+ 标准库与 SQLite；不会配置真实 CNI 或网络接口。

三个业务入口 `pool`、`lease`、`reclaim` 分别维护网段权益、唯一持有人与回收见证。地址池份额到期仅阻止新分配；旧 Pod 必须先被高代围栏，再收齐注册网关的当前代际回执及连接排空证据，才能释放地址。迟到连接或孤儿占用会重开回收案。

```sh
python -m ipleasecourt demo --db demo.sqlite
python -m ipleasecourt replay --db demo.sqlite
python -m unittest discover -s tests -v
```

单入口命令如 `python -m ipleasecourt pool --db state.sqlite --fixture fixtures/pool_normal.json`。三个正常样例在同一数据库按 `pool`、`lease`、`reclaim` 顺序运行，随后可运行相应失败样例。所有命令留下原始事件与决定；`replay` 重新执行事件并逐表比较状态。
