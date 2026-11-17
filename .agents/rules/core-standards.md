# 核心约定

## 环境与运行

- Python 3，只用标准库。不要加第三方依赖，除非用户明确要求
- 启动：双击 `start.bat`，或在仓库根目录执行 `py -3 serve.py`
- 监听 `0.0.0.0:8787`。本机 `http://127.0.0.1:8787`，同一局域网用启动时打印的地址
- 改 `station/modules.json`、`station/pages/`、`station/learn.json` 或 `station/piggy.html` 后刷新浏览器即可
- 改 `serve.py`、`station/work.py`、`station/learn.py`、`station/taskhub.py` 或 `station/piggy.py` 后必须重新启动

## 目录边界

| 路径 | 说明 |
|------|------|
| `serve.py` | 路由、整页外壳、读清单、静态资源。模块名单不写死在这里 |
| `start.bat` | 只负责切到仓库根目录并启动，不放业务逻辑 |
| `station/modules.json` | 模块清单，导航的唯一来源 |
| `station/pages/<id>.html` | 该模块的中间内容，可选；没有文件就显示占位 |
| `web/assets/` | 样式等静态文件，经 `/assets/` 提供 |
| `station/work.py` | 工作计划的事项、周期和中间内容 |
| `station/work.json` | 工作计划的数据。第一次记事时出现，不进清单 |
| `station/learn.py` | 小孩学习的读取、修改和中间内容 |
| `station/learn.json` | 小孩学习的内容。导入导出用这份文件，不进清单 |
| `station/taskhub.py` | 任务调度的状态、租约和 `/api/v1` |
| `station/taskhub.db` | 任务调度的数据。第一次启动时出现，不进清单 |
| `station/pages/taskhub.html` | 任务调度的中间内容 |
| `station/piggy.py` | 攒钱的读取和保存 |
| `station/piggy.json` | 攒钱的记录。只留在这台电脑上，不进清单 |
| `station/piggy.html` | 攒钱的整页。`/piggy/app/` 直接给出，没有侧栏 |
| `station/pages/piggy.html` | 攒钱套在外壳里的中间内容 |
| `docs/taskhub.md` | 任务调度的设计。接口和状态以这份为准 |
| `docs/Rules_1.md` | 员工侧调度协议。换项目只改身份三行 |
| `__pycache__/` | 解释器缓存，不提交、不当源码改 |

清单里不存放某个模块的数据。模块自己的内容留在它的页面文件，或以后该模块自己的文件里。

## 加模块

先改 `station/modules.json` 的 `modules`，保存后刷新。`id` 用小写英文、数字或短横线，1–32 位，且不重复。需要页面时再新建 `station/pages/<id>.html`，只写中间内容。功能可用后去掉该项的 `status`。侧栏、底栏和首页都从清单生成，不要另改导航。

## 代码风格

- 改动保持最小，命名和结构跟周围代码一致
- 能复用现有函数就复用，不要复制出第二份几乎相同的逻辑
- 同类需求第二次出现时再提取，不为「也许用得上」加抽象层
- 从清单进入页面的文字用 `html.escape`。页面文件是本地 HTML，按片段插入
- 用户可见文案直接写中文。本仓库没有翻译管线，不要自行加多语言目录

## Git 提交规范

采用 [Conventional Commits](https://www.conventionalcommits.org/)：`<type>: <中文简述>`

| type | 何时用 |
|------|--------|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 只改文档 / 规则 |
| `refactor` | 重构（行为不变） |
| `test` | 只加/改测试 |
| `chore` | 构建、依赖、工具杂项 |
| `style` | 格式（不影响逻辑） |

- 一句说清改了什么、为何改
- 英文半角冒号加空格（`fix: ...`）
- 同一提交只做一件事
- 好：`fix: 模块 id 重复时指出是哪一项`
- 差：`update`、`fix bug`、`杂项`
- 仅在用户明确要求时才 `git commit`；提交信息必须与本次 diff 一致
