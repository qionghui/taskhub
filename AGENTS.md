# AGENTS.md

家里局域网用的小网站。没有账号。外壳固定：电脑左侧是模块，手机底部是模块。
模块从清单来，页面只提供中间内容。

人看 `README.md`；代理看本文件。细则在 `.agents/rules/`，按需加载。
规则只写工程约定，不写某个模块做什么。

## Always apply

- @.agents/rules/core-standards.md
- @.agents/rules/communication.md
- @.agents/rules/testing.md

## Scoped rules

编辑相关路径时再读对应规则：

| 规则 | 适用路径 |
|------|----------|
| @.agents/rules/server.md | `serve.py`、`start.bat` |
| @.agents/rules/station.md | `station/**` |
| @.agents/rules/styles.md | `web/assets/**` |

## 关键约定速览

细则为准；此处只作索引：

- 启动方式、目录边界、清单驱动导航、提交说明 — [core-standards.md](./.agents/rules/core-standards.md)
- 文档与代码冲突时先问；外壳用词保持一致 — [communication.md](./.agents/rules/communication.md)
- 没有测试框架；改行为要实际请求或打开页面 — [testing.md](./.agents/rules/testing.md)
- 路由、转义、路径校验只放在 `serve.py` — [server.md](./.agents/rules/server.md)
- 模块只通过清单注册；页面文件只写中间片段 — [station.md](./.agents/rules/station.md)
- 外观只改 `web/assets/site.css`；宽屏和窄屏的正文都不能被裁切或挡住 — [styles.md](./.agents/rules/styles.md)
