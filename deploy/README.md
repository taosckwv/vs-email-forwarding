# deploy/

部署脚本与交付链的落点：服务器侧构建、基础镜像 tar 的导出与载入、交付前归档自检、数据库迁移与服务启动。

**当前为空**——新链路（在 CentOS 8 生产服务器上本地构建 + 基础镜像以 tar 带入）的脚本尚未实现，形态是未定项（见 `docs/open-questions.md` D2）。

不要在此放应用代码；判断标准、服务器侧硬约束与所有已否决方案见 `docs/research/01-delivery-and-deploy.md`。
