# 工作台

家里局域网用的一个小网站。没有账号，家人和自己打开就能用。

页面外壳是固定的：电脑左侧是模块，手机底部是模块。首页按清单列出每一块。工作计划可以记事项。小孩学习点开主题，里面是思维导图和卡片。攒钱记下任务和存下的钱，平板也可以单独打开。任务调度用来把活派给各电脑上的 AI 员工。简易 ERP 还没做。

## 目录

```
serve.py                 启动
start.bat                双击启动
station/modules.json     模块清单。加功能先改这里
station/pages/<id>.html  某个模块中间的内容，可选
station/work.py          工作计划
station/work.json        工作计划的数据，第一次记事时出现
station/learn.py         小孩学习
station/learn.json       小孩学习的内容，导入导出用这一份
station/taskhub.py       任务调度
station/taskhub.db       任务调度的数据，第一次启动时出现
station/pages/taskhub.html  任务调度的页面
station/piggy.py         攒钱的读写
station/piggy.json       攒钱的记录，只留在这台电脑上
station/piggy.html       攒钱的整页，没有侧栏
station/pages/piggy.html 攒钱套在外壳里的中间内容
docs/taskhub.md          任务调度的设计，接口和行为以它为准
docs/Rules_1.md          员工的调度协议，换项目只改身份三行
web/assets/site.css      外观
```

## 启动

双击 `start.bat`，或在这个目录执行：

```
py -3 serve.py
```

- 本机：`http://127.0.0.1:8787`
- 手机：窗口里打印的 `http://192.168.x.x:8787`

手机要连和电脑同一个 Wi-Fi。按 Ctrl+C 停止。

改清单、页面、`station/learn.json` 或 `station/piggy.html` 后，刷新浏览器即可。改 `serve.py`、`station/work.py`、`station/learn.py`、`station/taskhub.py` 或 `station/piggy.py` 后需要重新启动。

如果手机打不开，多半是 Windows 防火墙拦住了。在家里的网络上放行 Python，或允许 TCP 8787。

## 加一个模块

在 `station/modules.json` 的 `modules` 里加一项，保存后刷新：

```json
{
  "id": "notes",
  "name": "备忘",
  "group": "日常",
  "summary": "临时记下一件事",
  "status": "准备中"
}
```

`id` 用小写英文、数字或短横线。侧栏、底部导航和首页会一起出现这项。

要放真正的页面时，新建 `station/pages/notes.html`，只写中间内容，不要写整页。不做这一步也可以，会显示一块「准备中」。功能做完后，把 `status` 删掉，首页的「准备中」标记就没了。

## 任务调度

打开「任务调度」。先有项目，再派任务。各电脑上的员工按 `docs/Rules_1.md` 来领：地址就是这个网站，例如 `http://127.0.0.1:8787`，调用 `/api/v1`，只传员工编号和项目编码，没有口令。

计划只在页面上看见，不会出现在给员工的响应里。员工领走之后要按约定心跳；超时没续上，任务会回到可领取，并记一次失败。

原来 `D:\Project\TaskHub` 里的项目、员工和任务已经放进 `station/taskhub.db`。

## 攒钱

打开「攒钱」，或把平板直接开到 `/piggy/app/`（例如 `http://127.0.0.1:8787/piggy/app/`）。从首页和侧栏进去会带着工作台的导航；单独这个地址没有侧栏。

两边看的是同一份记录，在 `station/piggy.json`。这份文件只留在这台电脑上。清空、重置和补记仍要页面里的管理员密码。

## 小孩学习的内容

`station/learn.json` 是一份可以整份换上的内容。页面上的「导出」下载的就是这个文件。「导入」用另一份换上，换上前会先检查；有问题会指出坏在哪一张，原来的还在。「管理」里可以改一张、加一张、删掉一张。改这份文件不用重启。

`version` 现在是 `1`。`cards` 一层套一层：主题里面是题目，题目里面是步骤。

| 字段 | 作用 |
|------|------|
| `name` | 卡片标题，必填，40 字以内 |
| `text` | 标题下面的一行，可空，200 字以内 |
| `note` | 点开后、导图上面的一句，可空，200 字以内 |
| `id` | 点开用的编号。主题和还会再点开的卡片要有；最里面的步骤可以不写。小写英文、数字或短横线，整份里不重复 |
| `cards` | 里面的卡片，可空 |

两张或四张时排成导图，其他数量就从上往下连成一串。
