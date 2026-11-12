"""Porter —— 内部表单录入的 RPA 运行时。

模块划分：
    config     环境变量与默认值
    models     YAML 流程定义与运行记录的数据结构
    selectors  选择器表
    drivers    浏览器/HTTP 驱动，屏蔽 Playwright 与纯 HTTP 两种实现
    executor   顺序执行引擎
    store      PostgreSQL 运行记录
    api        FastAPI 服务
"""

__version__ = "0.9.3"
