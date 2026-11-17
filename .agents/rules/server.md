# 服务器约定

适用：`serve.py`、`start.bat`

## 职责

- `serve.py` 提供：读清单、套外壳、提供 `web/assets/`。工作计划另外由 `station/work.py` 读写 `station/work.json`。小孩学习由 `station/learn.py` 读写 `station/learn.json`；`GET /learn/?export=1` 原样下载这一份。任务调度由 `station/taskhub.py` 读写 `station/taskhub.db`，页面走 `/taskhub/`，接口走 `/api/v1/`。攒钱由 `station/piggy.py` 读写 `station/piggy.json`：`/piggy/` 套外壳，`/piggy/app/` 直接给出整页，`GET` 和 `POST /piggy/api/data` 读写这一份记录
- 模块名单来自每次请求重新读取的清单，不要在代码里写死链接
- 已在清单中的模块一律 200。没有页面文件时用统一占位，不要改成 404
- 未知路径用「没有这一页」，不要回退成首页

## 边界

- 写操作只有这些：`POST /work/` 只提交工作计划表单，只读写 `station/work.json`；`POST /learn/` 只提交小孩学习的管理（导入、删除、加上、保存），只读写 `station/learn.json`，导入必须先通过内容检查才整份换上；`/api/v1/agent/` 与 `/api/v1/admin/` 只读写 `station/taskhub.db`；`PATCH` 只接受任务调度里的编辑；`POST /piggy/api/data` 只整份保存攒钱记录，只读写 `station/piggy.json`。不要扩成任意文件写入，也不要加登录
- 监听所有网卡，且没有账号。不要增加会读出仓库里任意文件的路径
- 静态资源必须落在 `web/assets/` 内：校验字符、拒绝 `..`、解析后再确认仍在该目录下
- 清单字段进 HTML 前用 `html.escape`。`station/pages/` 里的文件是本地片段，按 HTML 插入，不要再包一层转义
- `id` 只允许小写英文、数字和短横线。不合法或重复时，错误信息要带上这个 id
- 外壳占位是 `{{key}}` 的整段替换。放进模板的值里不要再出现这种占位

## 启动脚本

`start.bat` 只做 `cd` 到自身所在目录并执行 `py -3 -u serve.py`。端口、路由和页面逻辑放在 `serve.py`。
